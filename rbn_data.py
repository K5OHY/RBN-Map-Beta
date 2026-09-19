"""Data helpers for the RBN Signal Mapper.

- Skimmer (reverse beacon) locations are pulled live from reversebeacon.net and
  cached in spotter_coords.csv, refreshed automatically when the cache is stale.
- A callsign's own location is resolved from the skimmer list or a callsign lookup.

Run `python rbn_data.py` to force a skimmer refresh from the command line.
"""
import re
import time
from pathlib import Path

import pandas as pd
import requests

NODES_URL = "https://www.reversebeacon.net/cont_includes/status.php?t=skt"
CACHE_FILE = Path(__file__).with_name("spotter_coords.csv")
CACHE_MAX_AGE_HOURS = 24
HEADERS = {"User-Agent": "RBN-Signal-Mapper (personal project)"}

GRID_RE = re.compile(r"^[A-R]{2}\d{2}([A-X]{2}(\d{2})?)?$", re.IGNORECASE)
ROW_RE = re.compile(
    r"<tr class=\"([^\"]*)\">\s*<td><a[^>]*>\s*(\S+)\s*</a>.*?<td>([^<]*)</td>",
    re.DOTALL,
)


def maidenhead_to_latlon(locator):
    """Centre of a 4, 6 or 8 character Maidenhead locator as (lat, lon)."""
    loc = locator.strip().upper()
    if not GRID_RE.match(loc):
        raise ValueError(f"Invalid grid square: {locator!r}")

    lon = (ord(loc[0]) - 65) * 20 - 180 + int(loc[2]) * 2
    lat = (ord(loc[1]) - 65) * 10 - 90 + int(loc[3])
    lon_size, lat_size = 2.0, 1.0

    if len(loc) >= 6:
        lon_size, lat_size = 2 / 24, 1 / 24
        lon += (ord(loc[4]) - 65) * lon_size
        lat += (ord(loc[5]) - 65) * lat_size
    if len(loc) == 8:
        lon_size, lat_size = lon_size / 10, lat_size / 10
        lon += int(loc[6]) * lon_size
        lat += int(loc[7]) * lat_size

    return round(lat + lat_size / 2, 5), round(lon + lon_size / 2, 5)


def fetch_skimmers():
    """Scrape the current node list from RBN. Returns a DataFrame(callsign, latitude, longitude)."""
    resp = requests.get(NODES_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    rows = {}
    for _, callsign, grid in ROW_RE.findall(resp.text):
        grid = grid.strip()
        if not GRID_RE.match(grid):
            continue
        rows[callsign.strip().upper()] = maidenhead_to_latlon(grid)

    if len(rows) < 50:  # page layout probably changed; don't clobber a good cache
        raise RuntimeError(f"Only parsed {len(rows)} skimmers from RBN; page format may have changed")

    return pd.DataFrame(
        [(cs, lat, lon) for cs, (lat, lon) in sorted(rows.items())],
        columns=["callsign", "latitude", "longitude"],
    )


def refresh_skimmer_cache(force=False):
    """Refresh spotter_coords.csv if missing or older than CACHE_MAX_AGE_HOURS.

    Returns (updated: bool, message: str). Never raises: on failure the existing
    cache stays in place so the app keeps working offline.
    """
    if not force and CACHE_FILE.exists():
        age_h = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_AGE_HOURS:
            return False, f"Skimmer list is {age_h:.0f}h old (up to date)."

    try:
        df = fetch_skimmers()
    except Exception as exc:
        return False, f"Could not refresh skimmer list ({exc}); using cached copy."

    # Keep skimmers that have since dropped off RBN's list; old history files still reference them.
    if CACHE_FILE.exists():
        old = pd.read_csv(CACHE_FILE)
        df = pd.concat([old[~old["callsign"].isin(df["callsign"])], df], ignore_index=True)
        df = df.sort_values("callsign")

    df.to_csv(CACHE_FILE, index=False)
    return True, f"Skimmer list refreshed: {len(df)} skimmers."


def load_skimmers():
    """Return {callsign: (lat, lon)} from the cache."""
    df = pd.read_csv(CACHE_FILE)
    return {r.callsign: (r.latitude, r.longitude) for r in df.itertuples()}


CTY_URL = "https://www.country-files.com/cty/cty.dat"
CTY_FILE = Path(__file__).with_name("cty.dat")
CTY_MAX_AGE_DAYS = 30
_cty_cache = {}


def _load_cty():
    """Parse cty.dat into ({prefix: (lat, lon, country)}, {exact_call: (lat, lon, country)}).

    Refreshes the file monthly; falls back to whatever copy is on disk.
    """
    if _cty_cache:
        return _cty_cache["prefixes"], _cty_cache["exact"]

    stale = not CTY_FILE.exists() or (time.time() - CTY_FILE.stat().st_mtime) > CTY_MAX_AGE_DAYS * 86400
    if stale:
        try:
            resp = requests.get(CTY_URL, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            CTY_FILE.write_bytes(resp.content)
        except Exception:
            pass  # keep the old copy if there is one
    if not CTY_FILE.exists():
        return {}, {}

    prefixes, exact = {}, {}
    entity = None
    for line in CTY_FILE.read_text(errors="ignore").splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():  # entity header: name: cq: itu: cont: lat: lon(west+): tz: prefix:
            f = [x.strip() for x in line.split(":")]
            entity = (float(f[4]), -float(f[5]), f[0])
            continue
        for item in line.strip().rstrip(";").split(","):
            item = re.sub(r"[\(\[<\{~].*", "", item.strip())  # drop zone/lat-lon overrides
            if not item or entity is None:
                continue
            if item.startswith("="):
                exact[item[1:]] = entity
            else:
                prefixes[item] = entity

    _cty_cache.update(prefixes=prefixes, exact=exact)
    return prefixes, exact


def country_location(callsign):
    """Approximate (lat, lon, country) of the DXCC entity for a callsign, or None."""
    prefixes, exact = _load_cty()
    call = callsign.strip().upper()

    if "/" in call:  # portable: KH6/K5OHY -> use the prefix part; K5OHY/P -> the base call
        a, b = call.split("/")[:2]
        call = a if (len(b) <= 2 or b in ("QRP", "MM", "AM") or len(a) <= len(b)) else b

    if call in exact:
        return exact[call]
    for n in range(len(call), 0, -1):  # longest matching prefix wins
        if call[:n] in prefixes:
            return prefixes[call[:n]]
    return None


def lookup_callsign_location(callsign, skimmers=None):
    """Best-effort (lat, lon, grid, source) for a callsign, or None.

    Order: RBN skimmer with this callsign, callook.info (US/FCC), hamdb.org, and finally the
    centre of the callsign's DXCC country (approximate).
    """
    call = callsign.strip().upper()
    base = call.split("/")[0] if "/" in call else call
    if len(base) <= 3 and "/" in call:  # 'W1/K5OHY': the first part is just a prefix
        base = call.split("/")[1]

    if skimmers:
        for key in skimmers:
            if key in (call, base) or key.startswith(base + "-"):
                lat, lon = skimmers[key]
                return lat, lon, None, "RBN skimmer list"

    try:
        data = requests.get(f"https://callook.info/{base}/json", headers=HEADERS, timeout=10).json()
        if data.get("status") == "VALID":
            loc = data["location"]
            return float(loc["latitude"]), float(loc["longitude"]), loc.get("gridsquare"), "FCC (callook.info)"
    except Exception:
        pass

    try:
        data = requests.get(f"https://api.hamdb.org/{base}/json/rbn-signal-mapper", headers=HEADERS, timeout=10).json()
        info = data["hamdb"]["callsign"]
        if info.get("call") != "NOT_FOUND" and info.get("grid"):
            lat, lon = maidenhead_to_latlon(info["grid"])
            return lat, lon, info["grid"], "HamDB"
    except Exception:
        pass

    approx = country_location(call)
    if approx:
        lat, lon, country = approx
        return lat, lon, None, f"center of {country} (approximate - enter a grid for accuracy)"

    return None


if __name__ == "__main__":
    print(refresh_skimmer_cache(force=True)[1])
