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


# ------------------------------------------------------------------ small statistics

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
        t = 1.96 + 2.4 / (n - 1) + 3.0 / (n - 1) ** 2  # close to the t value for n - 1 degrees of freedom
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

    d = shared["delta"].to_numpy()
    n = len(d)
    lo, hi = bootstrap_mean_ci(d) if n >= 2 else (math.nan, math.nan)
    mean = float(d.mean()) if n else math.nan
    if paired and n >= 2:
        lo, hi = widen_for_fading(lo, hi, mean, round_means)
    wins_a, wins_b = int((d > 0).sum()), int((d < 0).sum())
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
        "ta": ta, "tb": tb, "shared": shared,
        "reach": {"a": len(heard_a), "b": len(heard_b), "only_a": only_a, "only_b": only_b, "both": both,
                  "p": reach_p, "call": reach_call, "rounds": reach_rounds, "spots_a": len(spots_a), "spots_b": len(spots_b),
                  "spots_all_a": n_all_a, "spots_all_b": n_all_b},
        "equalized": equalized,
        "strength": {"n": n, "mean": mean, "median": float(np.median(d)) if n else math.nan, "lo": lo, "hi": hi,
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

def default_windows(spots):
    """Two sensible A / B time windows for a run of spots. When someone stopped transmitting to change antenna
    there is a quiet gap; split at the longest gap that leaves each side at least a quarter of the run (a gap right
    at the start or end would make one window tiny), and keep the windows tight around the real activity so the
    quiet minutes aren't counted as time on the air. With no such gap, split down the middle. Half-open: [start, end)."""
    times = np.sort(spots["time"].unique())
    t0, t1 = pd.Timestamp(times[0]), pd.Timestamp(times[-1]) + pd.Timedelta(minutes=2)
    total = (t1 - t0).total_seconds()
    if len(times) > 1 and total > 0:
        gaps = np.diff(times).astype("timedelta64[s]").astype(float) / 60
        for i in np.argsort(-gaps):
            if gaps[i] < 6:
                break
            a_end = min(pd.Timestamp(times[i]) + pd.Timedelta(minutes=2), pd.Timestamp(times[i + 1]))
            b_start = pd.Timestamp(times[i + 1])
            if min((a_end - t0).total_seconds(), (t1 - b_start).total_seconds()) / total >= 0.25:
                return (t0, a_end), (b_start, t1)
    mid = t0 + (t1 - t0) / 2
    return (t0, mid), (mid, t1)


def split_windows(spots, window_a, window_b):
    """Spots inside each [start, end) window."""
    def inside(w):
        return spots[(spots["time"] >= w[0]) & (spots["time"] < w[1])]
    return inside(window_a), inside(window_b)


def split_alternating(spots, t0, block_minutes, guard_minutes=0.0, a_first=True):
    """A in blocks 0, 2, 4 ..., B in blocks 1, 3, 5 ... (swap with a_first=False). Spots in the first
    `guard_minutes` of each block are dropped (the antenna may still have been switching). Each spot is tagged
    with its block number and its round (a round = one A block plus the B block after it)."""
    minutes = (spots["time"] - t0).dt.total_seconds() / 60.0
    block = np.floor(minutes / block_minutes).astype(int)
    into_block = minutes - block * block_minutes
    keep = (minutes >= 0) & (into_block >= guard_minutes)
    is_a = ((block % 2 == 0) == a_first)
    tagged = spots.assign(block=block, round=block // 2)
    return tagged[keep & is_a], tagged[keep & ~is_a]


SLOT_SECONDS = 120  # a WSPR transmission starts every two minutes on the clock


def clock_labels(times, pattern="ABAB"):
    """A first guess at the antenna for each transmission, by the clock: the first transmission gets pattern[0], and
    every two-minute slot after it moves one step along the pattern ("ABAB", "BABA" or "ABBA"). A slot nobody reported
    still counts, so a gap doesn't shift the rest. Returns a list of 'A' / 'B' in the order of `times`."""
    t0 = times[0]
    return [pattern[int(round((t - t0).total_seconds() / SLOT_SECONDS)) % len(pattern)] for t in times]


def default_labels(times, pattern="ABAB"):
    """A first guess at the antenna for each transmission in `times` (sorted Timestamps): the antenna changes after
    every cycle. If the transmitter used nearly every two-minute slot, which is how a test is normally run, go by the
    clock, so a cycle nobody reported can't shift the ones after it. If it used only some slots, the antenna was
    presumably changed after each transmission it did make, so go down the list instead. A user can correct any row."""
    slots_spanned = int(round((times[-1] - times[0]).total_seconds() / SLOT_SECONDS)) + 1
    if len(times) >= 2 and len(times) / slots_spanned >= 0.8:
        return clock_labels(times, pattern)
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


def alternating_exposure(t0, t_end, block_minutes, guard_minutes=0.0, a_first=True):
    """Minutes on the air (after guard time) for A and for B across an alternating run."""
    total = (t_end - t0).total_seconds() / 60.0
    a = b = 0.0
    for i in range(max(0, math.ceil(total / block_minutes))):
        length = max(min(block_minutes, total - i * block_minutes) - guard_minutes, 0.0)
        if (i % 2 == 0) == a_first:
            a += length
        else:
            b += length
    return a, b


# ------------------------------------------------------------------ the verdict

def _straddle_text(names, s):
    """'Loop looks ahead by about 1.4 dB, but it could be anywhere from Dipole ahead by 0.3 dB to Loop ahead by 3.0 dB.'
    for a strength range that includes zero, so nobody has to decode the sign of 'A minus B'."""
    lead = "A" if s["mean"] > 0 else "B"
    start = (f"{names[lead]} looks ahead by about {abs(s['mean']):.1f} dB" if abs(s["mean"]) >= 0.1
             else "Neither looks ahead on average")
    return (f"{start}, but it could be anywhere from {names['A']} ahead by {s['hi']:.1f} dB to "
            f"{names['B']} ahead by {-s['lo']:.1f} dB")


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
                    f"{receiver_word}s). Reach was about the same.{dir_note}")
    elif rc in ("A", "B"):
        winner, level = rc, "success"
        why = {"equal": "strength at shared receivers is about equal",
               "inconclusive": "strength at shared receivers is too variable to call",
               "insufficient": f"too few {receiver_word}s heard both to compare strength"}[sc]
        headline = f"**{names[rc]} reached more {receiver_word}s;** {why}.{dir_note}"
    elif sc == "equal" and dir_note:
        headline = f"**About equal overall** (strength within ±{EQUAL_DB:g} dB, same reach).{dir_note}"
    elif sc == "equal":
        headline = f"**These two are equal.** Same reach, and signal strength is the same within ±{EQUAL_DB:g} dB."
    elif sc == "insufficient":
        headline = (f"**Too close to call.** Only {s['n']} {receiver_word}(s) heard both; need at least "
                    f"{MIN_SHARED} to compare strength, and reach is about the same.{dir_note}")
    else:
        if s["lo"] > 0 or s["hi"] < 0:
            side = "A" if s["mean"] > 0 else "B"
            lo_, hi_ = sorted((abs(s["lo"]), abs(s["hi"])))
            gap = f"{names[side]} looks ahead by {lo_:.1f} to {hi_:.1f} dB, a small edge that isn't decisive"
        else:
            gap = _straddle_text(names, s)
        headline = f"**Too close to call.** No clear difference in reach, and {gap}.{dir_note}"

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
            body = (f"**Equal strength:** at the {s['n']} shared {receiver_word}s, A minus B averages "
                    f"{s['mean']:+.1f} dB (95% range {s['lo']:+.1f} to {s['hi']:+.1f} dB), inside ±{EQUAL_DB:g} dB."
                    f"{lean}")
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
            body = (f"**Strength:** only {s['n']} {receiver_word}(s) heard both ({s['mean']:+.1f} dB, A minus B); "
                    f"too few to judge.")
        points.append(("\U0001F4F6", body + f" A was stronger at {s['wins_a']}, B at {s['wins_b']}, "
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
    if sequential:
        caveats.append("A and B ran one after the other. Which stations can hear you changes through the day, so the "
                       "difference may come from the time rather than the antenna. For a result you can pin on the "
                       "antenna, change antenna after every transmission, or every few minutes (the Every other "
                       "transmission and Swap every few minutes options).")
    return {"level": level, "winner": winner, "headline": headline, "points": points, "caveats": caveats}
