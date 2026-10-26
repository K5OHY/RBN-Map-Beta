"""Settings, colours, constants and small helpers shared by every page."""
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urlparse

import matplotlib.colors as mcolors
import numpy as np
import streamlit as st

from world_map import MAX_KM, SNR_CMAP


# Same colours the RBN website uses for each band.
BAND_COLORS = {
    "160m": "#ffe000", "80m": "#093F00", "60m": "#777777", "40m": "#ffa500",
    "30m": "#ff0000", "20m": "#800080", "17m": "#0000ff", "15m": "#444444",
    "12m": "#00ffff", "10m": "#ff00ff", "6m": "#ffc0cb",
}


# Key-free basemaps: (base tiles, optional labels overlay). Esri "Canvas" is the closest match to CARTO's look.
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"


_ESRI_ATTR = "Tiles &copy; Esri"


TILE_STYLES = {
    "Light": (_ESRI.format("Canvas/World_Light_Gray_Base"), _ESRI.format("Canvas/World_Light_Gray_Reference")),
    "Dark": (_ESRI.format("Canvas/World_Dark_Gray_Base"), _ESRI.format("Canvas/World_Dark_Gray_Reference")),
    "Satellite": (_ESRI.format("World_Imagery"), _ESRI.format("Reference/World_Boundaries_and_Places")),
    "Street": ("OpenStreetMap", None),
}


# Weak = small green, medium = yellow, strong = large dark red. What counts as weak or strong depends on the
# source: RBN CW reports typically run ~5-35 dB, while WSPR decodes far below the noise, roughly -28 to +2 dB.
RBN_SNR_SCALE = (5, 35)


WSPR_SNR_SCALE = (-28, 2)




KM_PER_MILE = 1.609344


MAX_DAYS = 7


SETTINGS_FILE = Path(__file__).with_name("settings.json")


def snr_strength(snr, scale=RBN_SNR_SCALE):
    """0 (weak) .. 1 (strong)"""
    lo, hi = scale
    return float(np.clip((snr - lo) / (hi - lo), 0, 1))


def snr_color(snr, scale=RBN_SNR_SCALE):
    return mcolors.to_hex(SNR_CMAP(snr_strength(snr, scale)))


def snr_radius(snr, scale=RBN_SNR_SCALE):
    return 5 + 6 * snr_strength(snr, scale)


# ----------------------------------------------------------------- comparing antennas

# The ways the Compare page can tell the A side from the B side (these are the labels people see).
SPLIT_FREQ = "Two frequencies"


SPLIT_LOG = "Antenna log"


def _bold(text):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def _pretty_date(file_date):
    """'20261004' -> '2026-10-04', '20261004-20261006' -> '2026-10-04 to 2026-10-06'."""
    parts = [f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else d for d in str(file_date).split("-")]
    return " to ".join(parts)


def _views(units):
    km = 1.609344 if units == "mi" else 1.0
    return {f"Nearby ({3000 if units == 'mi' else 5000:,} {units})": (3000 * km if units == "mi" else 5000.0),
            f"Half the world ({7500 if units == 'mi' else 12000:,} {units})": (7500 * km if units == "mi" else 12000.0),
            "Whole world": MAX_KM}


# -------------------------------------------------------------------------- app

CSS = """
<style>
  .block-container { padding-top: 2rem; max-width: 1400px; }
  h1 { font-weight: 700; letter-spacing: -0.5px; margin-bottom: 0; }
  .subtitle { color: #8a8f98; margin: 0 0 1.2rem 0; }
  div[data-testid="stMetric"] {
      background: rgba(128,128,128,.10); border-radius: 10px; padding: 12px 16px;
  }
  iframe { border-radius: 12px; }
</style>
"""


NEW_HOME, NEW_BETA = "https://farfieldlab.streamlit.app", "https://farfieldlab-beta.streamlit.app"


def moved_notice():
    """A note at the top of every page: this app is now FarField Lab, with a link that opens the same page and settings there."""
    here = urlparse(st.context.url or "")
    base = NEW_BETA if "beta" in here.netloc.lower() else NEW_HOME
    query = urlencode(dict(st.query_params))
    target = base + (here.path if here.path.strip("/") else "") + ("?" + query if query else "")
    st.info(f"**RBN Signal Mapper is now FarField Lab.** Same program, new name and a new address. "
            f"[Open this same page in FarField Lab]({target}). This page keeps working so old links do not break, but all new "
            "work goes to FarField Lab, so please update your bookmarks.", icon=":material/arrow_forward:")


def running_locally():
    """Settings are stored in a file, so only do it when run on your own PC, never on a shared web host."""
    try:
        return st.context.headers.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1")
    except Exception:
        return False


def load_settings():
    if not running_locally():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def save_settings(settings):
    if not running_locally():
        return
    try:
        if settings != load_settings():
            SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass  # read-only install; settings just won't be remembered


RBN_SOURCES = ["Download by date", "Paste from RBN site"]


WSPR_SOURCE = "WSPR"


SOURCE_CODES = {"rbn": "Download by date", "paste": "Paste from RBN site", "wspr": WSPR_SOURCE}  # as they appear in a link


GITHUB_URL = "https://github.com/K5OHY/RBN_Map"
GITHUB_BETA_URL = "https://github.com/K5OHY/RBN-Map-Beta"


def github_url():
    """The repository this copy of the app comes from: the beta's when it is running at the beta address."""
    return GITHUB_BETA_URL if "beta" in (st.context.url or "").lower() else GITHUB_URL
