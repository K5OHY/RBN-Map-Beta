"""WSPR spots for the RBN Signal Mapper, from the public wspr.live database.

wspr.live mirrors wsprnet.org's spot archive and exposes it over a plain HTTP SQL interface. A spot is one
receiver decoding one WSPR transmission, so it lines up with an RBN spot: spotter, frequency, SNR, time. Unlike RBN,
every spot also carries the receiver's own location and the transmit power, so no skimmer list is needed and
power differences between two tests can be corrected for.

Run `python wspr_data.py K5SWA` to print the last few hours of spots for a callsign (handy for checking access).
"""
import re
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from rbn_data import get_band

WSPR_URL = "https://db1.wspr.live/"
HEADERS = {"User-Agent": "RBN-Signal-Mapper (personal project)"}
MAX_ROWS = 300_000
MAX_LISTEN_ROWS = 600_000
# wspr.live numbers its bands by the first digits of the frequency; see https://wspr.live
BAND_CODES = {"160m": 1, "80m": 3, "60m": 5, "40m": 7, "30m": 10, "20m": 14, "17m": 18,
              "15m": 21, "12m": 24, "10m": 28, "6m": 50}
# wspr.live only honours a bare ?query= (no bound parameters), so anything that goes into the SQL is
# checked first. WSPR callsigns are letters, digits and '/'.
CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(/[A-Z0-9]{1,4})?$|^[A-Z0-9]{1,4}/[A-Z0-9]{1,6}$")

COLUMNS = ["spotter", "dx", "freq", "snr", "time", "band", "power", "drift", "rx_lat", "rx_lon", "rx_loc", "tx_loc"]


class WsprError(RuntimeError):
    """A problem talking to wspr.live that is worth showing to the user as-is."""


def clean_callsign(callsign):
    call = (callsign or "").strip().upper()
    if not CALL_RE.fullmatch(call):
        raise WsprError(f"{callsign!r} doesn't look like a WSPR callsign (letters, digits and one '/' only).")
    return call


def _utc_naive(dt):
    """Naive UTC datetime, as wspr.live stores times."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def fetch_wspr_spots(tx_call, start, end, band=None, timeout=90):
    """WSPR spots heard from `tx_call` between two UTC datetimes.

    Returns (DataFrame, truncated). Columns follow the RBN spot table (spotter, dx, freq in kHz, snr, time,
    band) plus power (dBm), drift, and the receiver's rx_lat / rx_lon / rx_loc and the transmitter's tx_loc.
    A receiver that decodes the same transmission twice is counted once, at its strongest decode.
    `truncated` is True when the row limit was hit; the newest spots are kept and the oldest are the ones missing.
    """
    call = clean_callsign(tx_call)
    start, end = _utc_naive(start), _utc_naive(end)
    if end <= start:
        raise WsprError("The end time must be after the start time.")
    band_sql = f" AND band = {BAND_CODES[band]}" if band in BAND_CODES else ""
    query = (
        "SELECT time, tx_sign, tx_loc, rx_sign, rx_loc, rx_lat, rx_lon, frequency, power, snr, drift "
        f"FROM wspr.rx WHERE tx_sign = '{call}'{band_sql} "
        f"AND time BETWEEN '{start:%Y-%m-%d %H:%M:%S}' AND '{end:%Y-%m-%d %H:%M:%S}' "
        f"ORDER BY time DESC LIMIT {MAX_ROWS} FORMAT JSONCompact"  # newest first: if the limit bites, the old spots go
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
        rows = reply["data"]
        names = [c["name"] for c in reply["meta"]]
    except (ValueError, KeyError) as exc:
        raise WsprError(f"wspr.live sent an unexpected reply: {resp.text[:200].strip()}") from exc

    if not rows:
        return pd.DataFrame(columns=COLUMNS), False
    raw = pd.DataFrame(rows, columns=names)  # JSONCompact sends bare arrays, which is about half the size
    df = pd.DataFrame({
        "spotter": raw["rx_sign"].str.upper(),
        "dx": raw["tx_sign"].str.upper(),
        "freq": raw["frequency"].astype(float) / 1000.0,  # Hz -> kHz, as everywhere else in the app
        "snr": raw["snr"].astype(float),
        "time": pd.to_datetime(raw["time"]),
        "power": raw["power"].astype(float),  # dBm
        "drift": raw["drift"].astype(float),
        "rx_lat": raw["rx_lat"].astype(float),
        "rx_lon": raw["rx_lon"].astype(float),
        "rx_loc": raw["rx_loc"],
        "tx_loc": raw["tx_loc"],
    })
    df["band"] = df["freq"].apply(get_band)
    df = df[df["rx_loc"].astype(bool)]  # a receiver with no grid can't be placed
    df = (df.sort_values("snr", ascending=False)
            .drop_duplicates(["time", "spotter"])
            .sort_values("time", kind="stable")
            .reset_index(drop=True))
    return df[COLUMNS], len(rows) >= MAX_ROWS


def fetch_listening(band, start, end, timeout=120):
    """Which receivers were decoding anything on `band` (a name like '20m') in each transmission slot, from ANY
    transmitter. A receiver that reported nothing in a slot may well not have been listening on that band: many WSPR
    receivers hop between bands. Returns (DataFrame(time, spotter), truncated)."""
    if band not in BAND_CODES:
        raise WsprError(f"{band!r} is not a band wspr.live knows.")
    start, end = _utc_naive(start), _utc_naive(end)
    query = (f"SELECT time, rx_sign FROM wspr.rx WHERE band = {BAND_CODES[band]} "
             f"AND time BETWEEN '{start:%Y-%m-%d %H:%M:%S}' AND '{end:%Y-%m-%d %H:%M:%S}' "
             f"GROUP BY time, rx_sign LIMIT {MAX_LISTEN_ROWS} FORMAT JSONCompact")
    try:
        resp = requests.get(WSPR_URL, params={"query": query}, headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise WsprError(f"Couldn't reach wspr.live ({exc.__class__.__name__}).") from exc
    if resp.status_code != 200:
        raise WsprError(f"wspr.live returned HTTP {resp.status_code}.")
    try:
        reply = resp.json()
        rows, names = reply["data"], [c["name"] for c in reply["meta"]]
    except (ValueError, KeyError) as exc:
        raise WsprError("wspr.live sent an unexpected reply.") from exc
    if not rows:
        return pd.DataFrame(columns=["time", "spotter"]), False
    raw = pd.DataFrame(rows, columns=names)
    return (pd.DataFrame({"time": pd.to_datetime(raw["time"]), "spotter": raw["rx_sign"].str.upper()}),
            len(rows) >= MAX_LISTEN_ROWS)


def receiver_locations(spots):
    """{receiver callsign: (lat, lon)} taken from the spots themselves (the same shape as the RBN skimmer list)."""
    last = spots.drop_duplicates("spotter", keep="last")
    return {r.spotter: (r.rx_lat, r.rx_lon) for r in last.itertuples()}


if __name__ == "__main__":
    who = sys.argv[1] if len(sys.argv) > 1 else "K5SWA"
    now = datetime.now(timezone.utc)
    spots, cut = fetch_wspr_spots(who, now - timedelta(hours=3), now)
    print(f"{len(spots)} spots from {spots['spotter'].nunique() if len(spots) else 0} receivers"
          + (" (row limit hit)" if cut else ""))
    print(spots.head(8).to_string(index=False))
