"""Band conditions now: what the WSPR transmitters near you say about which bands are open and where a signal like yours
would be heard.

Every WSPR spot says: this transmitter, at this power, was decoded by this receiver at this SNR. SNR moves one-for-one with
power, so a spot can be restated for a different power: if you run 10 dB more than the WSPR station, your signal there would
be about 10 dB stronger. Each mode needs a minimum SNR (measured in a 2,500 Hz bandwidth). The gap between the two is the
margin. This is arithmetic on measured reports, good to a few dB, so margins are shown as a guide, not a promise.

A receiver that never heard the nearby WSPR stations is not counted, so with more power than they used the real reach is
wider than what is shown.
"""
import math
from datetime import timedelta

import numpy as np
import pandas as pd
import requests
from geographiclib.geodesic import Geodesic

from session_report import BAND_ORDER, REGION_ORDER, region_of
from wspr_data import BAND_CODES, HEADERS, MAX_ROWS, WSPR_URL, WsprError

# The SNR (dB, in a 2,500 Hz bandwidth) a mode needs for a solid contact. Approximate: CW and voice depend on the operator.
MODES = {"CW (by ear)": -5.0, "SSB (voice)": 8.0, "FT8": -21.0, "FT4": -17.0, "JS8 (normal)": -24.0, "WSPR": -28.0}
CODE_TO_BAND = {code: band for band, code in BAND_CODES.items()}
EARTH_KM = 6371.0


def _km_between(lat1, lon1, lat2, lon2):
    """Great-circle distance in km (haversine), vectorised over the second point."""
    p1, p2 = math.radians(lat1), np.radians(lat2)
    dl = np.radians(np.asarray(lon2, dtype=float) - lon1)
    a = np.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def fetch_nearby(lat, lon, radius_km, start, end, timeout=90):
    """WSPR spots, from every transmitter within `radius_km` of a point, between two naive UTC datetimes.
    Returns (DataFrame(time, tx, tx_lat, tx_lon, rx, rx_lat, rx_lon, band, power, snr), truncated)."""
    lat, lon, radius_km = float(lat), float(lon), float(radius_km)  # these go into the SQL, so they must be numbers
    dlat = radius_km / 111.0
    dlon = min(180.0, radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.05)))
    codes = ", ".join(str(c) for c in sorted(CODE_TO_BAND))
    query = (
        "SELECT time, tx_sign, tx_lat, tx_lon, rx_sign, rx_lat, rx_lon, band, power, snr FROM wspr.rx "
        f"WHERE time BETWEEN '{start:%Y-%m-%d %H:%M:%S}' AND '{end:%Y-%m-%d %H:%M:%S}' "
        f"AND tx_lat BETWEEN {lat - dlat:.3f} AND {lat + dlat:.3f} AND tx_lon BETWEEN {lon - dlon:.3f} AND {lon + dlon:.3f} "
        f"AND band IN ({codes}) ORDER BY time DESC LIMIT {MAX_ROWS} FORMAT JSONCompact"
    )
    try:
        resp = requests.get(WSPR_URL, params={"query": query}, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise WsprError(f"Couldn't reach wspr.live ({exc.__class__.__name__}). Try again in a minute.") from exc
    if resp.status_code == 429:
        raise WsprError("wspr.live is rate limiting requests (20 per minute). Wait a minute and try again.")
    if resp.status_code != 200:
        raise WsprError(f"wspr.live returned HTTP {resp.status_code}: {resp.text[:200].strip()}")
    try:
        reply = resp.json()
        rows, names = reply["data"], [c["name"] for c in reply["meta"]]
    except (ValueError, KeyError) as exc:
        raise WsprError(f"wspr.live sent an unexpected reply: {resp.text[:200].strip()}") from exc
    columns = ["time", "tx", "tx_lat", "tx_lon", "rx", "rx_lat", "rx_lon", "band", "power", "snr"]
    if not rows:
        return pd.DataFrame(columns=columns), False
    raw = pd.DataFrame(rows, columns=names)
    df = pd.DataFrame({
        "time": pd.to_datetime(raw["time"]), "tx": raw["tx_sign"].str.upper(),
        "tx_lat": raw["tx_lat"].astype(float), "tx_lon": raw["tx_lon"].astype(float),
        "rx": raw["rx_sign"].str.upper(), "rx_lat": raw["rx_lat"].astype(float), "rx_lon": raw["rx_lon"].astype(float),
        "band": raw["band"].map(CODE_TO_BAND), "power": raw["power"].astype(float), "snr": raw["snr"].astype(float)})
    df = df.dropna(subset=["band"])
    df = df[(df["rx_lat"] != 0) | (df["rx_lon"] != 0)]  # a receiver with no position can't be placed
    df = df[_km_between(lat, lon, df["tx_lat"], df["tx_lon"]) <= radius_km]  # the box is square, the area is a circle
    df = (df.sort_values("snr", ascending=False).drop_duplicates(["time", "rx", "tx"]).sort_values("time")
            .reset_index(drop=True))
    return df[columns], len(rows) >= MAX_ROWS


DX_REGIONS = ["Europe", "Africa", "Asia", "Oceania", "South America", "Mexico, Caribbean & Central America"]
DOMESTIC = ["US East", "US Central", "US West", "Canada"]
_REGION_WORDS = {"Mexico, Caribbean & Central America": "Mexico and the Caribbean"}


def _where(region):
    return _REGION_WORDS.get(region, region)


def _watts(w):
    return "under 0.1 W" if w < 0.1 else f"{w:.1f} W" if w < 10 else f"{w:.0f} W"


def headline(result, power_w, mode, margin_db, units="mi"):
    """The conditions in a few plain sentences, DX first. Returns a list of markdown strings."""
    reg, count, need, bands, dx = (result["regions"], result["regions_n"], result["power_needed"], result["bands"], result["dx"])
    if bands.empty:
        return []
    k = 1.609344 if units == "mi" else 1.0
    lines = []
    cells = [(b, r, float(reg.at[b, r])) for b in reg.index for r in DX_REGIONS
             if r in reg.columns and pd.notna(reg.at[b, r]) and count.at[b, r] >= 2]
    good = sorted((c for c in cells if c[2] >= margin_db), key=lambda c: -c[2])
    if good:
        picks = []
        for band, region, m in good:
            if len(picks) < 4:
                picks.append(f"**{band} to {_where(region)}** ({'strong' if m >= 10 else 'good'}, {m:+.0f} dB)")
        lines.append(f"🌍 **Best DX right now:** {', '.join(picks)}.")
    elif cells:
        band, region, m = max(cells, key=lambda c: c[2])
        lines.append(f"🌍 **DX is hard at {power_w:g} W {mode} right now.** The best chance is {band} to {_where(region)}, "
                     f"which would need about {_watts(float(need.at[band, region]))}.")
    else:
        lines.append(f"🌍 **No DX paths show up yet:** nobody outside North America heard the stations near you. "
                     "Check again later or widen the area.")
    if len(dx):
        far = dx.iloc[0]
        lines.append(f"The farthest receiver that would hear you is **{far['rx']}** in {_where(far['region'])}, "
                     f"{far['km'] / k:,.0f} {units} away on {far['band']}.")
    if good and cells:
        need_more = [c for c in cells if c[2] < margin_db and c[2] >= margin_db - 10]
        if need_more:
            band, region, m = max(need_more, key=lambda c: c[2])
            lines.append(f"With more power: {band} to {_where(region)} would need about {_watts(float(need.at[band, region]))}.")
    status = bands["Status"]
    open_, marg, quiet = (list(status[status == s].index) for s in ("Open", "Marginal", "Quiet"))
    parts = []
    if open_:
        parts.append(f"**open:** {', '.join(open_)}")
    if marg:
        parts.append(f"**marginal:** {', '.join(marg)}")
    if quiet:
        parts.append(f"**quiet:** {', '.join(quiet)}")
    lines.append("Bands " + "; ".join(parts) + ".")
    rising = [b for b in bands.index if bands.at[b, "Trend"] == "opening" and bands.at[b, "Status"] != "Quiet"]
    falling = [b for b in bands.index if bands.at[b, "Trend"] == "fading" and bands.at[b, "Status"] != "Quiet"]
    moves = ([f"{', '.join(rising)} {'is' if len(rising) == 1 else 'are'} opening up"] if rising else []) +             ([f"{', '.join(falling)} {'is' if len(falling) == 1 else 'are'} fading"] if falling else [])
    if moves:
        lines.append("Right now " + " and ".join(moves) + ".")
    home = [r for r in DOMESTIC if r in reg.columns]
    if home:
        avg = reg[home].mean(axis=1).dropna()
        if len(avg):
            top = avg.sort_values(ascending=False)
            best = top.index[0]
            near = [b for b in top.index[1:3] if top[b] >= top.iloc[0] - 2]
            lines.append(f"For contacts across the US and Canada, **{best}**"
                         + (f" (also {', '.join(near)})" if near else "") + f" is strongest, about {top.iloc[0]:+.0f} dB over the threshold.")
    return lines


def _label_margin(margin, good, marginal):
    return "good" if margin >= good else "marginal" if margin >= marginal else "weak"


def analyze(spots, home, power_w, mode, end, window_min=60, antenna_db=0.0, margin_db=3.0):
    """What the spots in the last `window_min` minutes say about a signal of `power_w` watts in `mode`.

    Each receiver's reports on a band are restated for your power (and an antenna adjustment in dB), and the middle of those is
    compared with the mode's threshold: that is the receiver's margin. Returns a dict with
      'receivers': one row per receiver and band (margin, distance, bearing, region, reports),
      'bands': one row per band (status, counts, farthest good receiver, trend),
      'regions': band x region table of the margin at a typical good receiver (75th percentile) and its receiver count,
      'power_needed': band x region watts needed to reach that typical receiver with `margin_db` to spare,
      'dx': the farthest receivers that would hear you."""
    threshold = MODES[mode]
    your_dbm = 10 * math.log10(max(power_w, 1e-6) * 1000)
    start = end - timedelta(minutes=window_min)
    s = spots[(spots["time"] > start) & (spots["time"] <= end)].copy()
    empty = {"receivers": pd.DataFrame(), "bands": pd.DataFrame(), "regions": pd.DataFrame(), "regions_n": pd.DataFrame(),
             "power_needed": pd.DataFrame(), "dx": pd.DataFrame(), "n_spots": 0, "n_tx": 0}
    if s.empty:
        return empty
    s["adj"] = s["snr"] + (your_dbm - s["power"]) + antenna_db  # the SNR you would have had at that receiver
    half = end - timedelta(minutes=window_min / 2)
    rows = []
    for (band, rx), g in s.groupby(["band", "rx"]):
        inv = Geodesic.WGS84.Inverse(home[0], home[1], float(g["rx_lat"].iloc[0]), float(g["rx_lon"].iloc[0]))
        rows.append({"band": band, "rx": rx, "adj": float(g["adj"].median()), "reports": len(g),
                     "km": inv["s12"] / 1000, "bearing": inv["azi1"] % 360,
                     "region": region_of(rx, (float(g["rx_lat"].iloc[0]), float(g["rx_lon"].iloc[0]))),
                     "recent": bool((g["time"] > half).any()), "earlier": bool((g["time"] <= half).any())})
    rec = pd.DataFrame(rows)
    rec["margin"] = rec["adj"] - threshold
    rec["verdict"] = [_label_margin(m, margin_db, 0.0) for m in rec["margin"]]

    bands = []
    for band in [b for b in BAND_ORDER if b in set(rec["band"])]:
        r = rec[rec["band"] == band]
        good, marginal = r[r["verdict"] == "good"], r[r["verdict"] == "marginal"]
        sb = s[s["band"] == band]
        recent, earlier = int(r["recent"].sum()), int(r["earlier"].sum())
        trend = "new" if earlier == 0 else "opening" if recent >= 1.3 * earlier else "fading" if recent <= 0.7 * earlier else "steady"
        status = "Open" if len(good) >= 3 else "Marginal" if len(good) + len(marginal) >= 2 else "Quiet"
        far = good.sort_values("km", ascending=False).head(1)
        bands.append({"Band": band, "Status": status, "Spots": len(sb), "Stations near you": sb["tx"].nunique(),
                      "Receivers": len(r), "Good": len(good), "Marginal": len(marginal), "Trend": trend,
                      "Farthest good (km)": float(far["km"].iloc[0]) if len(far) else float("nan"),
                      "Farthest good receiver": far["rx"].iloc[0] if len(far) else "",
                      "Farthest good region": far["region"].iloc[0] if len(far) else ""})
    bands = pd.DataFrame(bands).set_index("Band")

    regions = rec.groupby(["band", "region"])["margin"].quantile(0.75).unstack()
    regions_n = rec.groupby(["band", "region"]).size().unstack(fill_value=0)
    order_b = [b for b in BAND_ORDER if b in regions.index]
    order_r = [r for r in REGION_ORDER if r in regions.columns]
    regions, regions_n = regions.reindex(order_b)[order_r], regions_n.reindex(order_b)[order_r]
    extra_db = margin_db - regions  # the dB more (or less, if negative) than your power that a typical receiver would need
    power_needed = power_w * 10 ** (extra_db / 10)

    dx = rec[rec["margin"] >= 0].sort_values("km", ascending=False).head(12)
    return {"receivers": rec, "bands": bands, "regions": regions, "regions_n": regions_n, "power_needed": power_needed,
            "dx": dx, "n_spots": len(s), "n_tx": int(s["tx"].nunique())}
