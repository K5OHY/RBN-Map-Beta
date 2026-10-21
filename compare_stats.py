"""Antenna / test comparison maths, shared by the RBN and WSPR views.

Everything here is plain pandas / numpy (no Streamlit), so it can be checked against data where the right
answer is known. Two groups of spots, A and B, go in; a dict of findings and a plain-English verdict come out.

How the answer is reached
-------------------------
* Reach: how many different receivers heard each side. Receivers that heard only one side decide it, and an
  exact sign test (McNemar) says whether the imbalance is more than chance.
* Strength: at the receivers that heard BOTH sides, the SNR difference (A minus B). Comparing the same receivers
  cancels out their different locations and noise floors. A bootstrap gives the 95 % range for the average.
* Distance and direction: the same two measures, split by how far away and which way the receiver is.
* Power: if the two tests ran at different power, B's SNR is shifted to A's power first (WSPR reports its
  power; for RBN you can tell the app).
"""
import functools
import math
from functools import lru_cache

import numpy as np
import pandas as pd
from geographiclib.geodesic import Geodesic

SECTOR_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
KM_PER_MILE = 1.609344

ALPHA = 0.05              # significance level for "a real difference"
EQUAL_DB = 1.0            # a difference inside +/- this many dB counts as practically equal
MIN_SHARED = 5            # fewer receivers than this heard both sides: not enough to judge strength
DIRECTION_EDGE_DB = 2.0   # a direction / distance band only counts as "stronger" past this many dB
BIN_MIN_SHARED = 4        # ...and only with at least this many receivers hearing both sides there
BIN_ALPHA = 0.01          # ...and a 99 % range that excludes zero (about 14 bins are checked at once)
MIN_DISCORDANT = 4        # fewer one-sided receivers than this: reach can't be told apart
SLOT_NOISE_DB = 0.85      # how far apart ONE pair of identical-antenna transmissions typically reads, from fading that
                          # hits every receiver at once (measured on real WSPR data: spread 0.84 dB over 43 pairs)
REACH_LEAN_MIN = 0.6      # reach is only described as "leaning" a way when it is at least this likely to be real
MIN_REACH_ROUNDS = 6      # reach is judged round by round; fewer decisive rounds than this can't be significant
DX_MARGIN = 0.25          # one extreme receiver is noisy, so the farthest must be this much farther...
DX_MIN_KM = 1000          # ...and at least this far (km) farther, before it is called a difference

DISTANCE_EDGES = {"mi": [0, 300, 1000, 2000, 4000, 6000], "km": [0, 500, 1500, 3000, 6000, 10000]}


# ------------------------------------------------------------------ locations and tables

def locate(spotter, locs):
    """(lat, lon) of a spotter, tolerating the '-#' suffix used for extra skimmers at one site."""
    return locs.get(spotter) or locs.get(spotter.split("-")[0])


@lru_cache(maxsize=50000)
def _inverse(lat1, lon1, lat2, lon2):
    """(distance km, bearing degrees) from point 1 to point 2; cached because the same paths come up repeatedly."""
    inv = Geodesic.WGS84.Inverse(lat1, lon1, lat2, lon2)
    return inv["s12"] / 1000, inv["azi1"] % 360


def receiver_table(spots, locs, home):
    """One row per located receiver: median SNR, spot count, distance (km) and bearing from `home`."""
    rows = []
    for spotter, g in spots.groupby("spotter"):
        loc = locate(spotter, locs)
        if loc is None:
            continue
        km, bearing = _inverse(float(home[0]), float(home[1]), float(loc[0]), float(loc[1]))
        rows.append((spotter, g["snr"].median(), len(g), km, bearing))
    return pd.DataFrame(rows, columns=["skimmer", "snr", "spots", "km", "bearing"]).set_index("skimmer")


# ------------------------------------------------------------------ paired comparison

def paired_shared(spots_a, spots_b, locs, home, offset=0.0):
    """The same-receiver comparison for tests where the antennas alternate: for every round (an A transmission and the
    B one right next to it) take each receiver that heard both, and the difference A minus B. A receiver's answer is
    the median of its differences across rounds. Neighbouring transmissions share nearly the same propagation, so
    drift mostly cancels, and a receiver that only heard a few rounds still counts. `offset` (dB) is added to B first
    to correct a transmit-power difference. Returns (table, round_means): the same columns as the unpaired table plus
    `pairs`, and the average A minus B in each round (how the pairs came out, one number per pair)."""
    a = spots_a.groupby(["spotter", "round"])["snr"].median().rename("snr_a")
    b = (spots_b.groupby(["spotter", "round"])["snr"].median() + offset).rename("snr_b")
    pairs = pd.concat([a, b], axis=1, join="inner").reset_index()
    cols = ["snr_a", "snr_b", "delta", "pairs", "km_a", "bearing_a", "km_b", "bearing_b"]
    if pairs.empty:
        return pd.DataFrame(columns=cols, index=pd.Index([], name="skimmer")), pd.Series(dtype=float)
    pairs["delta"] = pairs["snr_a"] - pairs["snr_b"]
    by_round = pairs.groupby("round")["delta"].agg(["mean", "size"])
    round_means = by_round.loc[by_round["size"] >= 3, "mean"]  # a round only counts if enough receivers heard both
    per = pairs.groupby("spotter").agg(snr_a=("snr_a", "median"), snr_b=("snr_b", "median"),
                                       delta=("delta", "median"), pairs=("delta", "size"))
    geo = {}
    for spotter in per.index:
        loc = locate(spotter, locs)
        if loc is not None:
            geo[spotter] = _inverse(float(home[0]), float(home[1]), float(loc[0]), float(loc[1]))
    per = per[per.index.isin(geo)]
    per["km_a"] = per["km_b"] = [geo[r][0] for r in per.index]
    per["bearing_a"] = per["bearing_b"] = [geo[r][1] for r in per.index]
    per.index.name = "skimmer"
    return per[cols], round_means


def _one_per_site(shared):
    """Skimmers at the same place (K9TRV and K9TRV-2, or one site's several antennas) see nearly the same sky, so they
    are not independent witnesses, and counting each one separately makes a result look more certain than it is. Rows at
    the same distance and bearing are averaged into one, labelled with how many were merged."""
    if len(shared) < 2:
        return shared
    site = pd.Series(pd.factorize(pd.Series(list(zip(shared["km_a"].round(1), shared["bearing_a"].round(1))),
                                            index=shared.index))[0], index=shared.index)
    if site.nunique() == len(shared):
        return shared
    parts = []
    for _, members in site.groupby(site):
        idx = list(members.index)
        row = shared.loc[idx].mean(numeric_only=True)
        row.name = idx[0] + (f" (+{len(idx) - 1})" if len(idx) > 1 else "")
        parts.append(row)
    out = pd.DataFrame(parts)
    out.index.name = shared.index.name
    return out


# ------------------------------------------------------------------ the power-average ("linear") check

WSPR_FLOOR_DB = -27       # WSPR decodes down to about -28 dB; reports at or below this are at the edge of what can be decoded
DALE_MIN_SPOTS = 20       # spots per antenna that a long time-averaged test aims for
DALE_SITE_MIN = 40        # the minimum Dale's published method keeps per antenna, per receiver


def station_gains(spots_a, spots_b, locs, home, method="snr", min_spots=1, offset=0.0, floor=None, min_km=0.0):
    """One row per located receiver that heard both sides: each side's average SNR there, how many reports that rests on, and
    where the receiver is. `method` is 'snr' (the middle of the dB reports) or 'linear' (the reports turned into power,
    averaged, and turned back into dB). `min_spots` keeps receivers with at least that many reports on EACH side, and
    `offset` (dB) is added to B first to correct a transmit-power difference. gap = A minus B, so a station's relative
    gain (B minus A) is minus the gap. `floor` drops reports at or below that SNR (WSPR's decoding edge) and `min_km` leaves
    out receivers closer than that (so a station reached by ground wave does not count)."""
    def per_receiver(spots, shift):
        if floor is not None:
            spots = spots[spots["snr"] > floor]
        snr = spots["snr"] + shift
        g = spots.assign(_snr=snr, _power=10 ** (snr / 10)).groupby("spotter")
        avg = 10 * np.log10(g["_power"].mean()) if method == "linear" else g["_snr"].median()
        return pd.DataFrame({"snr": avg, "n": g.size()})

    t = per_receiver(spots_a, 0.0).join(per_receiver(spots_b, offset), how="inner", lsuffix="_a", rsuffix="_b")
    t = t[(t["n_a"] >= min_spots) & (t["n_b"] >= min_spots)]
    geo = {}
    for who in t.index:
        loc = locate(who, locs)
        if loc is not None:
            geo[who] = _inverse(float(home[0]), float(home[1]), float(loc[0]), float(loc[1]))
    t = t[t.index.isin(geo)].copy()
    t["km_a"] = [geo[w][0] for w in t.index]
    t["bearing_a"] = [geo[w][1] for w in t.index]
    t = t[t["km_a"] >= min_km]
    t[["snr_a", "snr_b"]] = t[["snr_a", "snr_b"]].round(1)  # a tenth of a dB is plenty, and keeps the table readable
    t["gap"] = (t["snr_a"] - t["snr_b"]).round(1)
    t["delta"] = t["gap"]
    t.index.name = "receiver"
    return t


def mean_interval(values):
    """(mean, low, high) of a set of numbers, with a 95% range on the mean (NaN when there are fewer than two numbers)."""
    g = np.asarray(values, dtype=float)
    g = g[~np.isnan(g)]
    if not len(g):
        return math.nan, math.nan, math.nan
    mean = float(g.mean())
    if len(g) < 2:
        return mean, math.nan, math.nan
    t_value = t_crit(len(g) - 1)
    half = t_value * float(g.std(ddof=1)) / math.sqrt(len(g))
    return mean, mean - half, mean + half


def gain_by_window(spots_a, spots_b, locs, home, windows, method="linear", min_spots=3, offset=0.0, receiver=None,
                   floor=None, min_km=0.0):
    """Relative gain (B minus A) in each time window: the average over every receiver that heard both sides in that window,
    with a 95% range, and the number of receivers. `windows` is a list of (start, end) times. With `receiver` set, the gain at
    that one receiver is returned as well. Returns a DataFrame, one row per window."""
    rows = []
    for start, end in windows:
        a = spots_a[(spots_a["time"] >= start) & (spots_a["time"] < end)]
        b = spots_b[(spots_b["time"] >= start) & (spots_b["time"] < end)]
        row = {"start": start, "end": end, "n": 0, "mean": math.nan, "lo": math.nan, "hi": math.nan, "one": math.nan,
               "one_n": (0, 0)}
        if len(a) and len(b):
            table = station_gains(a, b, locs, home, method, min_spots, offset, floor, min_km)
            if len(table):
                row["n"] = len(table)
                row["mean"], row["lo"], row["hi"] = mean_interval(-table["gap"])
            if receiver is not None:
                x, y = a[a["spotter"] == receiver]["snr"].to_numpy(float), b[b["spotter"] == receiver]["snr"].to_numpy(float) + offset
                row["one_n"] = (len(x), len(y))
                if len(x) and len(y):
                    row["one"] = (10 * np.log10(np.mean(10 ** (y / 10))) - 10 * np.log10(np.mean(10 ** (x / 10)))
                                  if method == "linear" else float(np.median(y) - np.median(x)))
        rows.append(row)
    return pd.DataFrame(rows)


def power_method(spots_a, spots_b, locs, home, min_spots=1, floor=None, offset=0.0, min_km=0.0):
    """The same comparison done by averaging POWER instead of dB, for a cross-check against the default.

    For each receiver: every report is turned from dB into power (10 ** (dB / 10)), each antenna's reports are averaged,
    and the ratio is turned back into dB. That is that receiver's relative gain (A minus B). The receivers' gains are then
    averaged (each place counted once) with a 95 % range. `min_spots` keeps only receivers with at least that many
    reports on EACH antenna; `floor` drops reports at or below that SNR (WSPR's decoding edge); `min_km` leaves out receivers
    closer than that.
    Returns {'table', 'n', 'mean', 'lo', 'hi', 'median', 'sd', 'near_floor', 'dropped'}."""
    def clip(spots):
        return spots if floor is None else spots[spots["snr"] > floor]

    near_floor = float(((spots_a["snr"] <= WSPR_FLOOR_DB).sum() + (spots_b["snr"] <= WSPR_FLOOR_DB).sum())
                       / max(len(spots_a) + len(spots_b), 1))
    a, b = clip(spots_a), clip(spots_b)
    dropped = (len(spots_a) - len(a)) + (len(spots_b) - len(b))
    pa = a.assign(p=10 ** (a["snr"] / 10)).groupby("spotter").agg(spots_a=("p", "size"), power_a=("p", "mean"))
    pb = b.assign(p=10 ** ((b["snr"] + offset) / 10)).groupby("spotter").agg(spots_b=("p", "size"), power_b=("p", "mean"))
    t = pa.join(pb, how="inner")
    t = t[(t["spots_a"] >= min_spots) & (t["spots_b"] >= min_spots)]
    empty = {"table": t.assign(gain=np.nan), "n": 0, "mean": math.nan, "lo": math.nan, "hi": math.nan,
             "median": math.nan, "sd": math.nan, "near_floor": near_floor, "dropped": dropped, "sites": t.iloc[0:0]}
    if t.empty:
        return empty
    t = t.assign(gain=10 * np.log10(t["power_a"] / t["power_b"]))
    geo = {}
    for who in t.index:
        loc = locate(who, locs)
        if loc is not None:
            geo[who] = _inverse(float(home[0]), float(home[1]), float(loc[0]), float(loc[1]))
    t = t[t.index.isin(geo)].copy()
    if t.empty:
        return empty
    t["km_a"] = [geo[w][0] for w in t.index]
    t["bearing_a"] = [geo[w][1] for w in t.index]
    t = t[t["km_a"] >= min_km]
    if t.empty:
        return empty
    t.index.name = "receiver"
    sites = _one_per_site(t)
    g = sites["gain"].to_numpy()
    n = len(g)
    mean = float(g.mean())
    sd = float(g.std(ddof=1)) if n >= 2 else math.nan
    if n >= 2:
        t_value = t_crit(n - 1)
        lo, hi = mean - t_value * sd / math.sqrt(n), mean + t_value * sd / math.sqrt(n)
    else:
        lo = hi = math.nan
    return {"table": t, "sites": sites, "n": n, "mean": mean, "lo": lo, "hi": hi, "median": float(np.median(g)),
            "sd": sd, "near_floor": near_floor, "dropped": dropped}


GAIN_OCTANTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]  # centred on the compass points, like the direction charts
GAIN_QUADRANTS = ["N", "E", "S", "W"]


def gain_summary(every, by="octant", edges_km=None, labels=None):
    """The average relative gain (B minus A) of the receivers that heard both antennas, grouped by direction or by distance.
    `every` is the per-receiver table the gain charts use (it needs gap, km_a and bearing_a). Receivers at the same place are
    counted once. `by` is 'octant' (8 directions), 'quadrant' (4) or 'distance'; for distance, `edges_km` are the cut points
    (so two cuts make three bands) and `labels` names the bands. Returns a table with one row per group: its receivers, the
    average gain and the 95% range (NaN when a group has none, or only one for the range)."""
    sites = _one_per_site(every) if len(every) > 1 else every
    gain = -sites["gap"].to_numpy(float)
    if by == "distance":
        edges = sorted(edges_km)
        group = np.searchsorted(edges, sites["km_a"].to_numpy(float), side="right")
        names = list(labels) if labels else [f"Band {i + 1}" for i in range(len(edges) + 1)]
    else:
        width = 45 if by == "octant" else 90
        group = (((sites["bearing_a"].to_numpy(float) + width / 2) % 360) // width).astype(int)
        names = GAIN_OCTANTS if by == "octant" else GAIN_QUADRANTS
    rows = []
    for k, name in enumerate(names):
        values = gain[group == k]
        mean, low, high = mean_interval(values) if len(values) else (math.nan, math.nan, math.nan)
        rows.append({"group": name, "receivers": int(len(values)), "mean": mean, "low": low, "high": high})
    return pd.DataFrame(rows)


def direction_advantage(shared, width_deg=45):
    """Which side is stronger in each direction sector, at the stations that heard both: one row per sector with the number
    of stations and the average A-minus-B difference (positive = A stronger). `width_deg` is 45, 30 or 15."""
    n_sec = int(round(360 / width_deg))
    if shared.empty:
        sector = pd.Series([], dtype=int)
    else:
        sector = (((shared["bearing_a"] + width_deg / 2) % 360) // width_deg).astype(int)
    rows = []
    for i in range(n_sec):
        sh = shared[sector == i] if len(shared) else shared
        center = (i * width_deg) % 360
        start, end = (center - width_deg / 2) % 360, (center + width_deg / 2) % 360
        rows.append({"center": center, "n": len(sh), "delta": float(sh["delta"].mean()) if len(sh) else math.nan,
                     "label": SECTOR_NAMES[i] if width_deg == 45 else f"{start:.0f}\u2013{end:.0f}\u00b0"})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ small statistics

def _t_two_sided_p(t, df, steps=4000):
    """Two-sided p-value of a t statistic (Simpson's rule on the Student-t density; no SciPy needed)."""
    t = abs(t)
    if t == 0:
        return 1.0
    norm = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
    pdf = lambda x: norm * (1 + x * x / df) ** (-(df + 1) / 2)  # noqa: E731
    h = t / steps
    total = pdf(0) + pdf(t) + sum((4 if i % 2 else 2) * pdf(i * h) for i in range(1, steps))
    return max(0.0, min(1.0, 1 - 2 * total * h / 3))


@lru_cache(maxsize=512)
def _t_crit(df, level):
    if df > 300:  # close enough to the normal curve
        return 1.96 + 2.4 / df if level == 0.95 else 2.576 + 4.0 / df
    lo, hi = 0.0, 200.0
    for _ in range(36):  # bisection: the p-value falls as t rises
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _t_two_sided_p(mid, df, steps=800) > 1 - level else (lo, mid)
    return (lo + hi) / 2


def t_crit(df, level=0.95):
    """The exact two-sided Student t critical value for `df` degrees of freedom (12.71 for 1, 4.30 for 2, 2.78 for 4 at 95%)."""
    return _t_crit(round(max(float(df), 1.0), 2), level)


def mcnemar_exact(n_a, n_b):
    """Two-sided exact p-value for 'are n_a and n_b just a fair coin split of n_a + n_b?'."""
    n = n_a + n_b
    if n == 0:
        return 1.0
    lower_tail = sum(math.comb(n, i) for i in range(min(n_a, n_b) + 1)) / 2 ** n
    return min(1.0, 2 * lower_tail)


def bootstrap_mean_ci(values, n_boot=4000, alpha=ALPHA, seed=7):
    """(low, high) of the mean by resampling; a fixed seed keeps the answer steady between reruns."""
    x = np.asarray(values, dtype=float)
    if len(x) < 2:
        return math.nan, math.nan
    rng = np.random.default_rng(seed)
    means = x[rng.integers(0, len(x), size=(n_boot, len(x)))].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def db_from_watts(watts):
    return 10 * math.log10(watts * 1000) if watts and watts > 0 else None


def median_power_dbm(spots):
    """Median transmit power in dBm if the spots carry it (WSPR does), else None."""
    if "power" in spots.columns and spots["power"].notna().any():
        return float(spots["power"].median())
    return None


def describe_gain(db):
    """'3.0 dB (about half an S-unit, like 2.0x the power)' for a dB difference."""
    db = abs(db)
    s = db / 6
    s_text = "half an S-unit" if 0.4 <= s < 0.6 else f"{s:.1f} S-unit{'s' if abs(s - 1) > 0.05 else ''}"
    return f"{db:.1f} dB (about {s_text}, like {10 ** (db / 10):.1f}x the power)"


def magnitude_word(db):
    db = abs(db)
    return "marginally" if db < 1 else "noticeably" if db < 3 else "clearly" if db < 6 else "much"


# ------------------------------------------------------------------ breakdowns by distance and direction

def widen_for_fading(lo, hi, mean, round_means):
    """The receiver-by-receiver error bars only know about noise that differs between receivers. Fading on the
    transmitting end moves every receiver together, once per transmission, so a test of only a few pairs is less
    certain than that suggests. With 4+ rounds the round-to-round spread is measured; with fewer, a typical measured
    pair-to-pair spread (SLOT_NOISE_DB) is used. Returns the wider of the two ranges."""
    n = len(round_means)
    if n >= 4:
        se = float(round_means.std(ddof=1)) / math.sqrt(n)
        t = t_crit(n - 1)
    else:
        se, t = SLOT_NOISE_DB / math.sqrt(max(n, 1)), 1.96
    return min(lo, mean - t * se), max(hi, mean + t * se)


def distance_bins(units):
    """(edges in km, labels) for the distance breakdown, in the user's units."""
    k = KM_PER_MILE if units == "mi" else 1.0
    e = DISTANCE_EDGES[units]
    edges_km = [x * k for x in e] + [math.inf]
    labels = [f"{e[i]:,}–{e[i + 1]:,} {units}" for i in range(len(e) - 1)] + [f"{e[-1]:,}+ {units}"]
    return edges_km, labels


def _bin_of(series, inner_edges):
    return pd.Series(np.searchsorted(inner_edges, series.to_numpy(), side="right"), index=series.index)


def _breakdown(ta, tb, shared, bin_a, bin_b, bin_shared, labels, widen=0.0, allow_counts=True):
    rows = []
    for i, label in enumerate(labels):
        na, nb = int((bin_a == i).sum()), int((bin_b == i).sum())
        sh = shared[bin_shared == i]
        delta = float(sh["delta"].mean()) if len(sh) else math.nan
        call, basis = "", ""  # "" not None: pandas turns a missing string into NaN, which is truthy
        if len(sh) >= BIN_MIN_SHARED and abs(delta) >= DIRECTION_EDGE_DB:
            lo, hi = bootstrap_mean_ci(sh["delta"].to_numpy(), n_boot=1500, alpha=BIN_ALPHA)
            if lo - widen > 0 or hi + widen < 0:  # about 14 bins are checked at once, so each must hold up at a stricter level
                call, basis = ("A" if delta > 0 else "B"), "stronger"
        if not call and allow_counts and abs(na - nb) >= max(3, 0.3 * max(na, nb)):
            call, basis = ("A" if na > nb else "B"), "more receivers"
        rows.append({"label": label, "a": na, "b": nb, "shared": len(sh), "delta": delta, "call": call, "basis": basis})
    return pd.DataFrame(rows)


def distance_breakdown(ta, tb, shared, units, widen=0.0, allow_counts=True):
    edges_km, labels = distance_bins(units)
    inner = edges_km[1:-1]
    return _breakdown(ta, tb, shared, _bin_of(ta["km"], inner), _bin_of(tb["km"], inner),
                      _bin_of(shared["km_a"], inner), labels, widen, allow_counts)


def direction_breakdown(ta, tb, shared, widen=0.0, allow_counts=True):
    def sector(bearing):
        return ((bearing + 22.5) % 360 // 45).astype(int)
    return _breakdown(ta, tb, shared, sector(ta["bearing"]), sector(tb["bearing"]),
                      sector(shared["bearing_a"]), SECTOR_NAMES, widen, allow_counts)


# ------------------------------------------------------------------ who was actually listening

def eligible_receivers(spots_a, spots_b, listening, min_slots=2):
    """Receivers that were decoding something (anything, from any transmitter) in at least `min_slots` of side A's
    transmission slots AND of side B's. Many WSPR receivers hop between bands and listen to a band only in some slots;
    if A's transmissions happen to land on those slots and B's don't, that receiver "only hears A" because of its own
    schedule, not the antenna. Counting only receivers that had a fair chance at both sides removes that.
    `listening` is the DataFrame(time, spotter) from wspr_data.fetch_listening()."""
    def opportunities(spots):
        slots = set(spots["time"].unique())
        return listening[listening["time"].isin(slots)].groupby("spotter")["time"].nunique(), len(slots)
    (oa, na), (ob, nb) = opportunities(spots_a), opportunities(spots_b)
    # with only one or two transmissions on a side, asking for two listening slots there would exclude everyone
    return set(oa[oa >= min(min_slots, na)].index) & set(ob[ob >= min(min_slots, nb)].index)


# ------------------------------------------------------------------ fair exposure

EXPOSURE_TOLERANCE = 0.15  # time on the air within 15 % counts as balanced


def equalize_exposure(spots_a, spots_b, exposure, tolerance=EXPOSURE_TOLERANCE, seed=11, bucket="2min"):
    """More time on the air means more chances for a receiver to hear you, so a side that transmitted three times
    as long will "reach more receivers" even with an identical antenna. When the two sides' minutes on the air
    (`exposure` = (minutes_a, minutes_b)) differ by more than `tolerance`, this randomly keeps just enough of the
    longer side's time to match the shorter side. The thinning works on whole 2-minute slots, so one transmission's
    spots stay together, and a fixed seed keeps the answer steady between reruns.
    Returns (spots_a, spots_b, info) where info is None when nothing was changed."""
    if not exposure:
        return spots_a, spots_b, None
    ea, eb = exposure
    if min(ea, eb) <= 0 or abs(ea - eb) / max(ea, eb) <= tolerance:
        return spots_a, spots_b, None
    a_is_long = ea > eb
    keep_fraction = min(ea, eb) / max(ea, eb)
    long_side = spots_a if a_is_long else spots_b
    slots = long_side["time"].dt.floor(bucket)
    unique = slots.unique()
    kept = np.random.default_rng(seed).choice(unique, size=max(1, int(round(keep_fraction * len(unique)))), replace=False)
    thinned = long_side[slots.isin(kept)]
    info = {"side": "A" if a_is_long else "B", "fraction": keep_fraction, "minutes_a": ea, "minutes_b": eb}
    return (thinned, spots_b, info) if a_is_long else (spots_a, thinned, info)


# ------------------------------------------------------------------ the analysis

def _farthest(table):
    if table.empty:
        return None
    row = table["km"].idxmax()
    return {"km": float(table.loc[row, "km"]), "who": row, "p90": float(table["km"].quantile(0.9))}


def analyze(spots_a, spots_b, locs, home, power_a=None, power_b=None, normalize=True, units="mi", exposure=None):
    """Compare two groups of spots. `power_a` / `power_b` are dBm (None = unknown); when both are known and
    `normalize` is true, B's SNR is shifted to A's power before comparing. `exposure` = (minutes_a, minutes_b) of
    time on the air when the sides were separated in time; an unbalanced pair is thinned to equal exposure first."""
    n_all_a, n_all_b = len(spots_a), len(spots_b)
    spots_a, spots_b, equalized = equalize_exposure(spots_a, spots_b, exposure)
    ta, tb = receiver_table(spots_a, locs, home), receiver_table(spots_b, locs, home)
    offset = (power_a - power_b) if (normalize and power_a is not None and power_b is not None) else 0.0
    tb = tb.assign(snr=tb["snr"] + offset)
    paired = "round" in spots_a.columns and "round" in spots_b.columns
    round_means = None
    if paired:
        shared, round_means = paired_shared(spots_a, spots_b, locs, home, offset)
    else:
        shared = ta.join(tb, how="inner", lsuffix="_a", rsuffix="_b")
        shared["delta"] = shared["snr_a"] - shared["snr_b"]

    shared_all = shared  # every skimmer that heard both, for display; the result counts each site once
    shared = _one_per_site(shared_all)
    # What the dots, the table and the win counts show: A's median minus B's median, the two numbers on screen. The result
    # itself uses `delta` (the middle of the round-by-round differences), which is steadier but, for a station heard in
    # several rounds, can lean the other way from the two medians. The dots must sit where their numbers say.
    shared_all = shared_all.assign(gap=shared_all["snr_a"] - shared_all["snr_b"])
    shared = shared.assign(gap=shared["snr_a"] - shared["snr_b"])

    heard_a, heard_b = set(spots_a["spotter"]), set(spots_b["spotter"])
    only_a, only_b, both = len(heard_a - heard_b), len(heard_b - heard_a), len(heard_a & heard_b)
    reach_rounds = None
    if paired:
        # Slot-to-slot fading raises or lowers EVERY receiver's chance at once, so counting receivers as if they were
        # independent flags differences that are only fading. Judge reach one round (A/B pair) at a time instead.
        ca, cb = spots_a.groupby("round")["spotter"].nunique(), spots_b.groupby("round")["spotter"].nunique()
        common = ca.index.intersection(cb.index)
        a_ahead, b_ahead = int((ca[common] > cb[common]).sum()), int((cb[common] > ca[common]).sum())
        reach_rounds = {"n": len(common), "a_ahead": a_ahead, "b_ahead": b_ahead}
        reach_p = mcnemar_exact(a_ahead, b_ahead)
        reach_call = (("A" if a_ahead > b_ahead else "B")
                      if a_ahead + b_ahead >= MIN_REACH_ROUNDS and reach_p < ALPHA else "tie")
    else:
        reach_p = mcnemar_exact(only_a, only_b)
        if only_a + only_b >= MIN_DISCORDANT and reach_p < ALPHA:
            reach_call = "A" if only_a > only_b else "B"
        else:
            reach_call = "tie"

    # How likely it is that the side with more one-sided receivers (or more winning pairs) really reaches more: one minus the
    # chance of a split at least that lopsided if the two were truly equal.
    reach_lead, reach_chance = None, math.nan
    ahead_a, ahead_b = (a_ahead, b_ahead) if paired else (only_a, only_b)
    n_split = ahead_a + ahead_b
    if n_split >= 2 and ahead_a != ahead_b:
        k_lead = max(ahead_a, ahead_b)
        reach_lead = "A" if ahead_a > ahead_b else "B"
        reach_chance = 1 - sum(math.comb(n_split, i) for i in range(k_lead, n_split + 1)) / 2 ** n_split

    d = shared["delta"].to_numpy()
    n = len(d)
    lo, hi = bootstrap_mean_ci(d) if n >= 2 else (math.nan, math.nan)
    mean = float(d.mean()) if n else math.nan
    sd = float(d.std(ddof=1)) if n >= 2 else math.nan
    if not paired and 2 <= n < 40:
        # with only a few stations the bootstrap range comes out too narrow; use the t interval instead
        t_value = t_crit(n - 1)
        lo, hi = mean - t_value * sd / math.sqrt(n), mean + t_value * sd / math.sqrt(n)
    if paired and n >= 2:
        lo, hi = widen_for_fading(lo, hi, mean, round_means)
    gap = shared["gap"].to_numpy()
    wins_a, wins_b = int((gap > 0).sum()), int((gap < 0).sum())
    p_ahead = math.nan  # how likely it is that the side the average favours really is ahead
    if n >= 2 and hi > lo:
        se = (hi - lo) / (2 * t_crit(n - 1))
        p_ahead = 1 - _t_two_sided_p(abs(mean) / se, n - 1) / 2 if se > 0 else 1.0
    if n < MIN_SHARED:
        strength_call = "insufficient"
    elif (lo > 0 or hi < 0) and abs(mean) >= EQUAL_DB:
        strength_call = "A" if mean > 0 else "B"
    elif (lo > 0 or hi < 0) or (lo >= -EQUAL_DB and hi <= EQUAL_DB):
        strength_call = "equal"  # a lead under EQUAL_DB, however sure we are of it, is practically nothing
    else:
        strength_call = "inconclusive"
    lean = ("A" if mean > 0 else "B") if strength_call == "equal" and (lo > 0 or hi < 0) else None

    widen = 2.576 * SLOT_NOISE_DB / math.sqrt(max(len(round_means), 1)) if paired else 0.0
    few_pairs = paired and reach_rounds["n"] < MIN_REACH_ROUNDS  # too few pairs to tell reach from fading
    far_a, far_b = _farthest(ta), _farthest(tb)
    dx_call = None
    if far_a and far_b:
        big, small = max(far_a["km"], far_b["km"]), min(far_a["km"], far_b["km"])
        if big - small >= DX_MIN_KM and big >= small * (1 + DX_MARGIN) and not few_pairs:
            dx_call = "A" if far_a["km"] > far_b["km"] else "B"

    return {
        "ta": ta, "tb": tb, "shared": shared, "shared_all": shared_all,
        "reach": {"a": len(heard_a), "b": len(heard_b), "only_a": only_a, "only_b": only_b, "both": both,
                  "p": reach_p, "call": reach_call, "rounds": reach_rounds, "lead": reach_lead, "chance": reach_chance, "spots_a": len(spots_a), "spots_b": len(spots_b),
                  "spots_all_a": n_all_a, "spots_all_b": n_all_b},
        "equalized": equalized,
        "strength": {"n": n, "mean": mean, "sd": sd, "p_ahead": p_ahead, "median": float(np.median(d)) if n else math.nan, "lo": lo, "hi": hi,
                     "wins_a": wins_a, "wins_b": wins_b, "ties": n - wins_a - wins_b,
                     "p_sign": mcnemar_exact(wins_a, wins_b), "call": strength_call, "lean": lean},
        "dx": {"a": far_a, "b": far_b, "call": dx_call},
        "distance": (distance_breakdown(ta, tb, shared, units, widen, not few_pairs)
                     if n or len(ta) or len(tb) else pd.DataFrame()),
        "direction": direction_breakdown(ta, tb, shared, widen, not few_pairs),
        "power": {"a": power_a, "b": power_b, "offset": offset,
                  "applied": bool(offset), "mismatch": power_a is not None and power_b is not None
                  and abs(power_a - power_b) >= 0.5, "normalize": normalize},
        "units": units, "paired": paired,
    }


# ------------------------------------------------------------------ rounds (alternating tests)

def round_consistency(spots_a, spots_b, locs, home, offset_db=0.0):
    """For alternating blocks: the A-minus-B difference within each round (one A block and the B block after it).
    Returns a DataFrame(round, shared, delta). If B wins most rounds, drifting propagation isn't the explanation."""
    rows = []
    for r in sorted(set(spots_a["round"].dropna()) & set(spots_b["round"].dropna())):
        ta = receiver_table(spots_a[spots_a["round"] == r], locs, home)
        tb = receiver_table(spots_b[spots_b["round"] == r], locs, home)
        sh = ta.join(tb, how="inner", lsuffix="_a", rsuffix="_b")
        if len(sh) >= 3:
            rows.append((int(r), len(sh), float((sh["snr_a"] - sh["snr_b"]).mean() - offset_db)))
    return pd.DataFrame(rows, columns=["round", "shared", "delta"])


# ------------------------------------------------------------------ splitting spots into A and B by time

SLOT_SECONDS = 120  # a WSPR transmission starts every two minutes on the clock


def clock_labels(times, pattern="ABAB"):
    """A first guess at the antenna for each transmission, by the clock: the first transmission gets pattern[0], and
    every two-minute slot after it moves one step along the pattern ("ABAB", "BABA" or "ABBA"). A slot nobody reported
    still counts, so a gap doesn't shift the rest. Returns a list of 'A' / 'B' in the order of `times`."""
    t0 = times[0]
    return [pattern[int(round((t - t0).total_seconds() / SLOT_SECONDS)) % len(pattern)] for t in times]


def default_labels(times, pattern="ABAB"):
    """A first guess at the antenna for each transmission in `times` (sorted Timestamps): the antenna changes after
    every transmission, so the pattern runs down the list. A cycle with no spots usually means the transmitter skipped
    it (WSJT-X picks its slots at random), and nobody changes an antenna for a transmission that did not happen. A user
    whose antennas switch on a timer, whatever was sent, can correct any row, or use clock_labels()."""
    return [pattern[i % len(pattern)] for i in range(len(times))]


def split_by_labels(spots, labels, max_gap_minutes=10):
    """Split spots into antenna A and B using a log of {transmission time: 'A' or 'B'} (anything else is left out),
    like a spreadsheet of time slots with the antenna beside each. Neighbouring transmissions on different antennas
    are paired up (the same receiver heard in both is the fair comparison); each spot gets a `round` number for its
    pair, or NaN if its transmission had no partner. Returns (spots_a, spots_b)."""
    times = sorted(t for t, lab in labels.items() if lab in ("A", "B"))
    rounds, i, r = {}, 0, 0
    while i < len(times):
        partner = i + 1 < len(times) and labels[times[i]] != labels[times[i + 1]] \
            and (times[i + 1] - times[i]) <= pd.Timedelta(minutes=max_gap_minutes)
        if partner:
            rounds[times[i]] = rounds[times[i + 1]] = r
            r += 1
            i += 2
        else:
            rounds[times[i]] = np.nan
            i += 1
    keep = spots[spots["time"].isin(times)]
    tagged = keep.assign(antenna=keep["time"].map(labels), round=keep["time"].map(rounds).astype(float))
    return tagged[tagged["antenna"] == "A"], tagged[tagged["antenna"] == "B"]


def count_pairs(spots_a, spots_b):
    """How many neighbouring A/B transmission pairs the split found."""
    if "round" not in spots_a.columns or "round" not in spots_b.columns:
        return 0
    return len(set(spots_a["round"].dropna()) & set(spots_b["round"].dropna()))


# ------------------------------------------------------------------ the verdict

def _straddle_text(names, s):
    """'Loop looks ahead by about 1.4 dB, but it could be anywhere from Dipole ahead by 0.3 dB to Loop ahead by 3.0 dB.'
    for a strength range that includes zero, so nobody has to decode the sign of 'A minus B'."""
    lead = "A" if s["mean"] > 0 else "B"
    start = (f"{names[lead]} looks ahead by about {abs(s['mean']):.1f} dB" if abs(s["mean"]) >= 0.1
             else "Neither looks ahead on average")
    return (f"{start}, but it could be anywhere from {names['A']} ahead by {s['hi']:.1f} dB to "
            f"{names['B']} ahead by {-s['lo']:.1f} dB")


def _lead_text(names, v):
    """'Loop ahead by 1.4 dB' for a signed A-minus-B value, so the sign never has to be decoded."""
    if abs(v) < 0.05:
        return "no difference"
    return f"{names['A'] if v > 0 else names['B']} ahead by {abs(v):.1f} dB"


def _plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _dirs(analysis, side):
    d = analysis["direction"]
    return [r["label"] for _, r in d.iterrows() if r["call"] == side]


def build_verdict(an, names, exposure=None, rounds=None, receiver_word="receiver", sequential=False, n_pairs=None):
    """Plain-English result. Returns {'level', 'winner', 'headline', 'points', 'caveats'}.
    `names` = {'A': ..., 'B': ...}; `exposure` = (minutes_a, minutes_b) when the sides were separated in time;
    `rounds` = the round_consistency() table for alternating tests; `n_pairs` = how many neighbouring A/B transmission
    pairs there were (a test of only a pair or two can be swung by fading, and the verdict says so). `sequential` is true when A and B ran one after
    the other (separate frequencies or time windows): changing conditions can then explain a difference, so a winner
    is described as winning "this test" rather than as the better antenna."""
    r, s, dx, pw, units = an["reach"], an["strength"], an["dx"], an["power"], an["units"]
    other = {"A": "B", "B": "A"}
    sc, rc = s["call"], r["call"]
    nA, nB = names["A"], names["B"]
    recv = lambda n: _plural(n, receiver_word)  # noqa: E731

    # ---- headline
    dirs = {side: _dirs(an, side) for side in ("A", "B")}
    if dirs["A"] and dirs["B"]:
        dir_note = (f" It depends on direction: {nA} is better toward {', '.join(dirs['A'])}; "
                    f"{nB} toward {', '.join(dirs['B'])}.")
    elif dirs["A"] or dirs["B"]:
        side = "A" if dirs["A"] else "B"
        dir_note = f" {names[side]} is better toward {', '.join(dirs[side])}."
    else:
        dir_note = ""

    lean_note = ""
    if sc == "equal" and s["lean"] and rounds is not None and len(rounds) >= 3:
        a_r, b_r = int((rounds["delta"] > 0).sum()), int((rounds["delta"] < 0).sum())
        if mcnemar_exact(a_r, b_r) < ALPHA and (a_r > b_r) == (s["lean"] == "A"):
            lean_note = (f" {names[s['lean']]} was ahead in {max(a_r, b_r)} of {len(rounds)} rounds, but only by a hair "
                         f"(under {EQUAL_DB:g} dB), which is too little to matter.")

    # Reach that leans one way but is not enough to call: still worth saying, because more stations heard it is the thing
    # people look at first.
    reach_lean = None
    r_lead, r_chance = r.get("lead"), r.get("chance", math.nan)
    if rc == "tie" and r_lead and not math.isnan(r_chance) and r_chance >= REACH_LEAN_MIN \
            and (r["a"] > r["b"]) == (r_lead == "A") and r["a"] != r["b"]:
        more, fewer = (r["a"], r["b"]) if r_lead == "A" else (r["b"], r["a"])
        reach_lean = (f"reach leans {names[r_lead]}, heard by {more} {receiver_word}s against {fewer}, "
                      "but not enough to call")

    clearly = "did clearly better in this test" if sequential else "is clearly getting out better"
    stronger = "was stronger in this test" if sequential else "has the stronger signal"
    winner, level = None, "info"
    if r["a"] == 0 and r["b"] == 0:
        headline = "**No data.** No located receiver heard either side."
    elif sc in ("A", "B") and rc == sc:
        winner, level = sc, "success"
        headline = (f"**{names[sc]} {clearly}.** It reached more {receiver_word}s and was "
                    f"{magnitude_word(s['mean'])} stronger at the ones that heard both.{dir_note}")
    elif sc in ("A", "B") and rc == other[sc]:
        headline = (f"**Mixed result.** {names[sc]} was stronger at the {receiver_word}s that heard both, "
                    f"but {names[rc]} reached more {receiver_word}s.{dir_note}")
    elif sc in ("A", "B"):
        winner, level = sc, "success"
        headline = (f"**{names[sc]} {stronger}** ({abs(s['mean']):.1f} dB louder at shared "
                    f"{receiver_word}s). " + (reach_lean[0].upper() + reach_lean[1:] + "." if reach_lean else "Reach was about the same.")
                    + dir_note)
    elif rc in ("A", "B"):
        winner, level = rc, "success"
        why = {"equal": "there is no meaningful strength difference at shared receivers",
               "inconclusive": "strength at shared receivers is too variable to call",
               "insufficient": f"too few {receiver_word}s heard both to compare strength"}[sc]
        headline = f"**{names[rc]} reached more {receiver_word}s;** {why}.{dir_note}"
    elif sc == "equal" and dir_note:
        headline = (f"**No meaningful difference overall** (the strength gap is under {EQUAL_DB:g} dB; "
                    f"{reach_lean or 'same reach'}).{dir_note}")
    elif sc == "equal":
        headline = (f"**No meaningful difference in strength.** The average gap is under {EQUAL_DB:g} dB; {reach_lean}."
                    if reach_lean else f"**No meaningful difference.** Same reach, and the average signal-strength gap is under "
                    f"{EQUAL_DB:g} dB.")
    elif sc == "insufficient":
        headline = (f"**Too close to call.** Only {s['n']} {receiver_word}(s) heard both; need at least "
                    f"{MIN_SHARED} to compare strength, and {reach_lean or 'reach is about the same'}.{dir_note}")
    else:
        if s["lo"] > 0 or s["hi"] < 0:
            side = "A" if s["mean"] > 0 else "B"
            lo_, hi_ = sorted((abs(s["lo"]), abs(s["hi"])))
            gap = f"{names[side]} looks ahead by {lo_:.1f} to {hi_:.1f} dB, a small edge that isn't decisive"
        else:
            gap = _straddle_text(names, s)
        headline = (f"**Too close to call.** {reach_lean[0].upper() + reach_lean[1:] if reach_lean else 'No clear difference in reach'}, "
                    f"and {gap}.{dir_note}")

    if sc == "equal" and lean_note:
        headline += lean_note
    if n_pairs is not None and 0 < n_pairs < 4 and not (r["a"] == 0 and r["b"] == 0):
        headline += (f" **That's only {n_pairs} pair{'s' if n_pairs != 1 else ''} of transmissions, so a little fading "
                     "could swing it. Repeat the test to confirm.**")
    if sequential and not winner and dir_note:
        headline += " **They ran one after the other, so the time of day could explain the direction differences.**"
    if sequential and winner:
        headline += (" **But A and B ran one after the other, so changing conditions could explain some or all of "
                     "this.**")

    # ---- supporting points
    points = []
    rr = r.get("rounds")
    if rr is not None:
        if rr["n"] < 3:
            sig = "too few pairs of transmissions to tell this apart from fading"
        else:
            sig = (f"A heard by more receivers in {rr['a_ahead']} of {rr['n']} pairs, B in {rr['b_ahead']}: "
                   + ("a real difference" if rc in ("A", "B") else "within normal variation"))
    else:
        sig = (f"a real difference (p = {r['p']:.2g})" if rc in ("A", "B")
               else f"within normal variation (p = {r['p']:.2g})" if r["only_a"] + r["only_b"] >= MIN_DISCORDANT
               else "too few one-sided receivers to say")
    if rc == "tie" and r_lead and not math.isnan(r_chance) and r_chance >= REACH_LEAN_MIN:
        sig += (f". {names[r_lead]} leads, but if the two really reached equally, a split this lopsided in that direction "
                f"would still happen about {1 - r_chance:.0%} of the time: suggestive, not enough to call it")
    points.append(("\U0001F4E1", f"**Reach:** {recv(r['a'])} heard A and {recv(r['b'])} heard B "
                   f"({r['only_a']} only A, {r['only_b']} only B, {r['both']} both) — {sig}."))

    if s["n"]:
        higher = "A" if s["mean"] > 0 else "B"
        if sc in ("A", "B"):
            lo, hi = (s["lo"], s["hi"]) if sc == "A" else (-s["hi"], -s["lo"])  # the winner's lead, low to high
            body = (f"**{names[sc]} was stronger by {describe_gain(s['mean'])}** on average at the "
                    f"{s['n']} {receiver_word}s that heard both (95% range {lo:.1f} to {hi:.1f} dB).")
        elif sc == "equal":
            lean = f" {names[s['lean']]} is very slightly ahead, too little to matter." if s["lean"] else ""
            inside = s["lo"] >= -EQUAL_DB and s["hi"] <= EQUAL_DB
            body = (f"**No meaningful strength difference:** at the {s['n']} shared {receiver_word}s the average difference is only "
                    f"{abs(s['mean']):.1f} dB, and the 95% range runs from {_lead_text(names, s['lo'])} to "
                    f"{_lead_text(names, s['hi'])}"
                    + (f", inside ±{EQUAL_DB:g} dB." if inside else
                       f", so the small lead is probably real.") + lean)
        elif sc == "inconclusive":
            if s["lo"] > 0 or s["hi"] < 0:
                side = "A" if s["mean"] > 0 else "B"
                lo_, hi_ = sorted((abs(s["lo"]), abs(s["hi"])))
                body = (f"**A small edge for {names[side]}:** at the {s['n']} shared {receiver_word}s it is ahead by "
                        f"{abs(s['mean']):.1f} dB on average (95% range {lo_:.1f} to {hi_:.1f} dB), too small and "
                        f"uncertain to call decisively.")
            else:
                body = (f"**Strength is inconclusive:** at the {s['n']} shared {receiver_word}s, "
                        f"{_straddle_text(names, s)}.")
        else:
            body = (f"**Strength:** only {s['n']} {receiver_word}(s) heard both ({_lead_text(names, s['mean'])}); "
                    f"too few to judge.")
        chance = ""
        if sc == "inconclusive" and not math.isnan(s["p_ahead"]) and s["p_ahead"] >= 0.7:
            chance = (f" {names['A' if s['mean'] > 0 else 'B']} leads, but if the two were really equal, a lead this big "
                      f"would still show up about {1 - s['p_ahead']:.0%} of the time: suggestive, not enough to call it.")
        points.append(("\U0001F4F6", body + chance + f" A was stronger at {s['wins_a']}, B at {s['wins_b']}, "
                       f"tied at {s['ties']}."))
    else:
        points.append(("\U0001F4F6", f"**Strength:** no {receiver_word} heard both, so strength can't be compared directly."))

    if dx["a"] and dx["b"]:
        k = KM_PER_MILE if units == "mi" else 1
        who = f"**{names[dx['call']]} reached farther.** " if dx["call"] else "Similar farthest reach. "
        points.append(("\U0001F30D", f"{who}Farthest {receiver_word}: A {dx['a']['km'] / k:,.0f} {units} ({dx['a']['who']}), "
                       f"B {dx['b']['km'] / k:,.0f} {units} ({dx['b']['who']})."))

    dist = an["distance"]
    if len(dist):
        notes = []
        for _, row in dist.iterrows():
            if row["call"] and row["basis"] == "stronger":
                notes.append(f"{names[row['call']]} stronger at {row['label']} ({abs(row['delta']):.1f} dB)")
            elif row["call"]:
                notes.append(f"{names[row['call']]} heard by more {receiver_word}s at {row['label']} ({row['a']} vs {row['b']})")
        if notes:
            points.append(("\U0001F4CF", "**By distance:** " + "; ".join(notes) + "."))
        else:
            points.append(("\U0001F4CF", "**By distance:** no distance range stands out."))

    dir_bits = []
    for side in ("A", "B"):
        ds = _dirs(an, side)
        if ds:
            dir_bits.append(f"{names[side]} toward {', '.join(ds)}")
    if dir_bits:
        points.append(("\U0001F9ED", "**By direction:** " + "; ".join(dir_bits) + "."))
    else:
        points.append(("\U0001F9ED", "**By direction:** no direction stands out."))

    if rounds is not None and len(rounds) >= 3:
        a_rounds, b_rounds = int((rounds["delta"] > 0).sum()), int((rounds["delta"] < 0).sum())
        lead, n_r = max(a_rounds, b_rounds), len(rounds)
        if n_r < 3:
            round_text = "Too few rounds to judge consistency."
        elif mcnemar_exact(a_rounds, b_rounds) < ALPHA:
            round_text = "One side winning round after round means changing propagation isn't the explanation."
        elif lead >= 0.75 * n_r:
            round_text = "Suggestive, but too few rounds to be sure; a longer test would settle it."
        else:
            round_text = "A mixed record: propagation moved around more than the antennas differ."
        points.append(("\U0001F501", f"**Consistency:** over {n_r} rounds of A then B, A was stronger in "
                       f"{a_rounds} and B in {b_rounds}. {round_text}"))

    # ---- caveats
    caveats = []
    if pw["mismatch"] and pw["applied"]:
        caveats.append(f"Transmit power differed ({pw['a']:.0f} vs {pw['b']:.0f} dBm), so B's SNR was shifted by "
                       f"{pw['offset']:+.0f} dB to match A's power before comparing.")
    if pw["applied"] and abs(pw["offset"]) >= 3:
        caveats.append("With a power gap this large, weak signals only get decoded on the stronger-power side, so the "
                       "corrected result slightly favours the lower-power side. Equal power gives the cleanest test.")
    if pw["mismatch"] and not pw["applied"]:
        caveats.append(f"Transmit power differed ({pw['a']:.0f} vs {pw['b']:.0f} dBm) and was NOT corrected for; "
                       "turn on power normalization for a fair comparison.")
    eq = an["equalized"]
    if eq:
        caveats.append(f"{names[eq['side']]} was on the air longer (A {eq['minutes_a']:.0f} min, B {eq['minutes_b']:.0f} min), "
                       f"and more time means more receivers get a chance to hear you. To keep the comparison fair, "
                       f"{names[eq['side']]}'s spots were randomly cut down to {eq['fraction']:.0%} of its time before comparing.")
    if 0 < s["n"] < 10:
        caveats.append(f"Only {recv(s['n'])} heard both sides, so treat the strength result as a hint.")
    if sc == "inconclusive" and not an["paired"] and s["n"] >= 3 and not math.isnan(s["sd"]):
        smallest = 2.8 * s["sd"] / math.sqrt(s["n"])  # 80 % chance of seeing a difference this big at 95 % confidence
        need = math.ceil((2.8 * s["sd"] / 2.0) ** 2)
        if need > s["n"]:
            caveats.append(
                f"This is a question of how much data there is, not of the antennas being equal. With the scatter between "
                f"{receiver_word}s here (about {s['sd']:.1f} dB), {s['n']} {receiver_word}s that heard both can only reliably "
                f"show a difference of about {smallest:.1f} dB or more. To see a 2 dB difference you would need about {need}. "
                f"More transmissions on each side steady each {receiver_word}'s reading and get you there.")
    if sequential:
        caveats.append("A and B were sent a while apart. Which stations can hear you changes through the day, so the "
                       "difference may come from the time rather than the antenna. For a result you can pin on the "
                       "antenna, send them back to back (a minute or two apart on RBN, or alternating cycles on WSPR) so "
                       "both see the same conditions.")
    return {"level": level, "winner": winner, "headline": headline, "points": points, "caveats": caveats}
