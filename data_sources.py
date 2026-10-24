"""Getting spots and skimmer locations: the RBN daily download and pasted rows, WSPR from wspr.live, and the skimmer list."""
import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd
import requests
import streamlit as st

import example_data
from rbn_data import get_band, load_skimmers, lookup_callsign_location, maidenhead_to_latlon, refresh_skimmer_cache
from wspr_data import fetch_listening, fetch_wspr_spots


# ----------------------------------------------------------------- data loading

@st.cache_data(show_spinner=False, ttl=3600)
def download_rbn_day(date, callsign):
    """Download one day of RBN history and keep only spots of `callsign` (streamed, never written to disk)."""
    resp = requests.get(f"https://data.reversebeacon.net/rbn_history/{date}.zip", timeout=180)
    if resp.status_code == 404:
        raise RuntimeError(f"RBN has no data file for {date} yet. Try an earlier date.")
    resp.raise_for_status()

    with zipfile.ZipFile(BytesIO(resp.content)) as z:
        name = next((n for n in z.namelist() if n.endswith(".csv")), None)
        if name is None:
            raise RuntimeError("No CSV file found in the RBN ZIP archive")
        with z.open(name) as f:
            parts = [c[c["dx"] == callsign] for c in pd.read_csv(f, chunksize=500_000)]

    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df = df.rename(columns={"callsign": "spotter", "db": "snr", "date": "time"})
    df["snr"] = pd.to_numeric(df["snr"], errors="coerce")
    df["freq"] = pd.to_numeric(df["freq"], errors="coerce")
    df["time"] = pd.to_datetime(df["time"])
    if "band" not in df.columns:
        df["band"] = df["freq"].apply(get_band)
    return df


# A row copied from the RBN spots page:
#   K1RA-4  K5OHY  DM81wx  1443 mi  14073.0  CW  CQ  7 dB  25 wpm  1908z 26 Sep  94 seconds ago
PASTE_ROW_RE = re.compile(
    r"^\W*(?P<spotter>[A-Z0-9/-]+)\s+(?P<dx>[A-Z0-9/-]+)\s+(?:[A-R]{2}\d{2}\w*\s+)?"
    r"(?:[\d,.]+\s*(?:mi|km)\s+)?(?P<freq>\d+\.\d+)\s+\S+\s+\S+\s+(?P<snr>-?\d+)\s*dB\b"
    r".*?(?P<time>\d{4})z\s+(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})",
    re.IGNORECASE,
)


def parse_pasted_data(text):
    """Parse rows copied from the RBN spots web page (header lines and trailing 'seen' column are ignored)."""
    year = datetime.now(timezone.utc).year
    rows, unread = [], []
    for line in filter(None, (l.strip() for l in text.splitlines())):
        m = PASTE_ROW_RE.match(line)
        if not m:
            if "spotter" not in line.lower():  # the copied header row is expected, anything else isn't
                unread.append(line)
            continue
        try:
            when = datetime.strptime(f"{year} {m['time']}z {m['day']} {m['month'].title()}", "%Y %H%Mz %d %b")
            rows.append([m["spotter"].upper(), m["dx"].upper(), float(m["freq"]), float(m["snr"]), when])
        except ValueError:
            unread.append(line)
    if not rows:
        raise RuntimeError("Couldn't read any spot rows from the pasted text."
                           + (f" The first line I couldn't read was: {unread[0][:120]!r}" if unread else ""))
    df = pd.DataFrame(rows, columns=["spotter", "dx", "freq", "snr", "time"])
    df["band"] = df["freq"].apply(get_band)
    return df


@st.cache_data(show_spinner=False, ttl=3600)
def skimmer_data():
    """Check hourly whether the skimmer list is stale (it only re-downloads if >24h old), so a
    long-running server keeps itself up to date. Returns (skimmers, status message)."""
    _, msg = refresh_skimmer_cache()
    return load_skimmers(), msg


def resolve_home(callsign, grid_override, skimmers):
    """Return (lat, lon, label) for the map pin: manual grid wins, otherwise look the callsign up."""
    if grid_override.strip():
        grid = grid_override.strip()
        lat, lon = maidenhead_to_latlon(grid)
        return lat, lon, f"{grid[:2].upper()}{grid[2:]} (manual)"
    found = lookup_callsign_location(callsign, skimmers)
    if not found:
        raise RuntimeError(
            f"Couldn't find a location for {callsign}. Enter your grid square in the sidebar to place the pin.")
    lat, lon, grid, source = found
    if "approximate" in source:
        st.warning(f"No exact location found for {callsign}, so the pin is at the {source}. "
                   "Enter your grid square in the sidebar for an accurate map.")
    return lat, lon, f"{grid or f'{lat:.2f}, {lon:.2f}'} via {source}"


def load_wspr(tx_call, start, end):
    """WSPR spots from wspr.live. Not cached: it only runs when you click Load spots, and then you want what is there
    now, including a test you have only just sent."""
    return fetch_wspr_spots(tx_call, start, end)


@st.cache_data(show_spinner=False, ttl=60)
def load_listening(band, start, end):
    """Which receivers were decoding on `band` in each slot. Remembered for a minute because the screen redraws often. The
    saved examples carry their own copy, so they work without asking wspr.live."""
    saved = example_data.listening(band, start, end)
    return saved if saved is not None else fetch_listening(band, start, end)
