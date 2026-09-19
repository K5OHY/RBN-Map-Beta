import json
import math
import zipfile
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path

import folium
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd
import requests
import streamlit as st
from geographiclib.geodesic import Geodesic

from rbn_data import (
    load_skimmers,
    lookup_callsign_location,
    maidenhead_to_latlon,
    refresh_skimmer_cache,
)

# Same colours the RBN website uses for each band.
BAND_COLORS = {
    "160m": "#ffe000", "80m": "#093F00", "60m": "#777777", "40m": "#ffa500",
    "30m": "#ff0000", "20m": "#800080", "17m": "#0000ff", "15m": "#444444",
    "12m": "#00ffff", "10m": "#ff00ff", "6m": "#ffc0cb",
}
BAND_RANGES_KHZ = [
    ("160m", 1800, 2000), ("80m", 3500, 4000), ("60m", 5300, 5500), ("40m", 7000, 7300),
    ("30m", 10100, 10150), ("20m", 14000, 14350), ("17m", 18068, 18168), ("15m", 21000, 21450),
    ("12m", 24890, 24990), ("10m", 28000, 29700), ("6m", 50000, 54000),
]
# Key-free basemaps: (base tiles, optional labels overlay). Esri "Canvas" is the closest match to CARTO's look.
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
_ESRI_ATTR = "Tiles &copy; Esri"
TILE_STYLES = {
    "Light": (_ESRI.format("Canvas/World_Light_Gray_Base"), _ESRI.format("Canvas/World_Light_Gray_Reference")),
    "Dark": (_ESRI.format("Canvas/World_Dark_Gray_Base"), _ESRI.format("Canvas/World_Dark_Gray_Reference")),
    "Satellite": (_ESRI.format("World_Imagery"), _ESRI.format("Reference/World_Boundaries_and_Places")),
    "Street": ("OpenStreetMap", None),
}
SNR_CMAP = mcolors.LinearSegmentedColormap.from_list("snr", ["#2ecc71", "#f1c40f", "#e74c3c"])
KM_PER_MILE = 1.609344
MAX_DAYS = 7
SETTINGS_FILE = Path(__file__).with_name("settings.json")


# ----------------------------------------------------------------- data loading

def get_band(freq):
    try:
        freq = float(freq)
    except (TypeError, ValueError):
        return "unknown"
    for name, lo, hi in BAND_RANGES_KHZ:
        if lo <= freq <= hi:
            return name
    return "unknown"


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


def parse_pasted_data(text):
    """Parse rows copied from the RBN spots web page."""
    rows = []
    for line in filter(None, (l.strip() for l in text.splitlines())):
        p = line.split()
        if len(p) < 14:
            continue
        try:
            when = datetime.strptime(" ".join(p[11:14]), "%H%Mz %d %b")
            rows.append([p[0], p[1], float(p[4]), float(p[7]), when])
        except ValueError:
            continue
    df = pd.DataFrame(rows, columns=["spotter", "dx", "freq", "snr", "time"])
    df["band"] = df["freq"].apply(get_band)
    return df


def skimmer_location(spotter, skimmers):
    """Look up a spotter, tolerating the '-#' suffix used in RBN history files."""
    return skimmers.get(spotter) or skimmers.get(spotter.split("-")[0])


# --------------------------------------------------------------------- geometry

def distance_km(a, b):
    return Geodesic.WGS84.Inverse(a[0], a[1], b[0], b[1])["s12"] / 1000


def great_circle(start, end, num_points=50):
    """Points along the great circle, with longitudes unwrapped so lines don't jump across the antimeridian."""
    line = Geodesic.WGS84.InverseLine(start[0], start[1], end[0], end[1])
    pts, last_lon = [], None
    for i in range(num_points + 1):
        pos = line.Position(i * line.s13 / num_points)
        lon = pos["lon2"]
        if last_lon is not None:
            if lon - last_lon > 180:
                lon -= 360
            elif lon - last_lon < -180:
                lon += 360
        last_lon = lon
        pts.append((pos["lat2"], lon))
    return pts


# -------------------------------------------------------------------------- map

def snr_color(snr):
    return mcolors.to_hex(SNR_CMAP(float(np.clip(snr / 40, 0, 1))))


def build_map(spots, skimmers, home, home_label, callsign, show_all, tiles, units, farthest):
    k = KM_PER_MILE if units == "mi" else 1
    base, labels = TILE_STYLES[tiles]
    if base == "OpenStreetMap":
        m = folium.Map(location=home, zoom_start=3, tiles=base, control_scale=True)
    else:
        m = folium.Map(location=home, zoom_start=3, tiles=None, control_scale=True)
        folium.TileLayer(base, attr=_ESRI_ATTR, name=tiles, max_zoom=16).add_to(m)
        if labels:
            folium.TileLayer(labels, attr=_ESRI_ATTR, name="Labels", overlay=True, max_zoom=16).add_to(m)

    if show_all:
        layer = folium.FeatureGroup(name="All skimmers", show=True)
        for call, (lat, lon) in skimmers.items():
            folium.CircleMarker((lat, lon), radius=2, color="#555", weight=1, fill=True,
                                fill_opacity=0.6, tooltip=call).add_to(layer)
        layer.add_to(m)

    lines = folium.FeatureGroup(name="Paths", show=True)
    dots = folium.FeatureGroup(name="Spots (sized/coloured by SNR)", show=True)
    bounds = [home]

    for row in spots.itertuples():
        loc = skimmer_location(row.spotter, skimmers)
        if loc is None:
            continue
        path = great_circle(home, loc)
        end = path[-1]  # unwrapped end point keeps the marker on the line's end
        bounds.extend([path[0], end])
        color = BAND_COLORS.get(row.band, "#3388ff")

        folium.PolyLine(path, color=color, weight=2, opacity=0.65).add_to(lines)
        folium.CircleMarker(
            end,
            radius=float(np.clip(row.snr / 2, 3, 14)),
            color=snr_color(row.snr), weight=1, fill=True, fill_opacity=0.75,
            popup=folium.Popup(
                f"<b>{row.spotter}</b><br>{row.band} &middot; {row.freq:.1f} kHz<br>"
                f"SNR: {row.snr:.0f} dB<br>{row.time:%d %b %H:%M} UTC<br>{distance_km(home, loc) / k:,.0f} {units}",
                max_width=220),
        ).add_to(dots)

    lines.add_to(m)
    dots.add_to(m)

    far_loc = skimmer_location(farthest, skimmers) if farthest else None
    if far_loc:
        far_end = great_circle(home, far_loc)[-1]
        folium.CircleMarker(
            far_end, radius=18, color="#e11d48", weight=3, dash_array="6 6", fill=False,
            tooltip=folium.Tooltip(f"Farthest: {farthest} · {distance_km(home, far_loc) / k:,.0f} {units}",
                                   permanent=True, direction="top", offset=(0, -14)),
        ).add_to(m)

    folium.Marker(
        home, icon=folium.Icon(icon="star", color="red"),
        popup=f"{callsign}<br>{home_label}", tooltip=f"{callsign} ({home_label})",
    ).add_to(m)

    if len(bounds) > 1:
        m.fit_bounds(bounds, padding=(30, 30))
    folium.LayerControl(collapsed=True).add_to(m)

    bands_present = spots["band"].value_counts()
    rows = "".join(
        f'<div><span style="display:inline-block;width:18px;height:4px;background:{BAND_COLORS.get(b, "#3388ff")};'
        f'margin-right:6px;vertical-align:middle;border-radius:2px"></span>{b} <span style="opacity:.6">({n})</span></div>'
        for b, n in sorted(bands_present.items(), key=lambda kv: list(BAND_COLORS).index(kv[0]) if kv[0] in BAND_COLORS else 99)
    )
    legend = f"""
    <div style="position:fixed;bottom:34px;right:12px;z-index:9999;background:rgba(255,255,255,.92);
      padding:10px 12px;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.3);
      font:12px/1.5 system-ui,sans-serif;color:#222">
      <b>{callsign}</b> &middot; {len(spots)} spots<div style="margin:6px 0 2px;font-weight:600">Band</div>{rows}
      <div style="margin:8px 0 2px;font-weight:600">SNR</div>
      <div style="height:8px;width:120px;border-radius:4px;background:linear-gradient(90deg,#2ecc71,#f1c40f,#e74c3c)"></div>
      <div style="display:flex;justify-content:space-between;width:120px;opacity:.7"><span>0</span><span>40+ dB</span></div>
    </div>"""
    m.get_root().html.add_child(folium.Element(legend))
    return m


def compute_stats(spots, skimmers, home):
    best = (0.0, None)
    for spotter in spots["spotter"].unique():
        loc = skimmer_location(spotter, skimmers)
        if loc:
            d = distance_km(home, loc)
            if d > best[0]:
                best = (d, spotter)
    return {
        "spots": len(spots),
        "skimmers": spots["spotter"].nunique(),
        "max_km": best[0],
        "farthest": best[1],
        "max_snr": spots["snr"].max(),
        "avg_snr": spots["snr"].mean(),
    }


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


def main():
    st.set_page_config(layout="wide", page_title="RBN Signal Mapper", page_icon="📡")
    st.markdown(CSS, unsafe_allow_html=True)
    st.title("📡 RBN Signal Mapper")
    st.markdown('<p class="subtitle">Map the Reverse Beacon Network stations that spotted your CQ, and how strong your signal was.</p>',
                unsafe_allow_html=True)

    skimmers, refresh_msg = skimmer_data()

    ss = st.session_state
    for key in ("raw", "home", "callsign", "file_date"):
        ss.setdefault(key, None)

    cfg = load_settings()

    def pick(options, key, default=None):
        """Index of the saved choice in `options` (falls back to the first/default)."""
        value = cfg.get(key, default if default is not None else options[0])
        return options.index(value) if value in options else 0

    with st.sidebar:
        st.header("Your signal")
        callsign = st.text_input("Callsign", value=cfg.get("callsign", ""), placeholder="K5OHY").strip().upper()
        grid_override = st.text_input(
            "Grid square (optional)", value=cfg.get("grid", ""), placeholder="Looked up from your callsign",
            help="Leave blank to use your callsign's registered address. "
                 "Enter a grid if you were portable or operating from elsewhere, or if the lookup fails.")

        sources = ["Download by date", "Paste from RBN site"]
        source = st.radio("Where are the spots from?", sources, index=pick(sources, "source"), horizontal=True)
        if source == "Download by date":
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
            span = st.radio("Period", ["Single day", "Date range"], horizontal=True,
                            index=pick(["Single day", "Date range"], "span"))
            if span == "Single day":
                days = [st.date_input("Date (UTC)", value=yesterday, max_value=yesterday)]
            else:
                first = st.date_input("From (UTC)", value=yesterday - timedelta(days=MAX_DAYS - 1), max_value=yesterday)
                last = st.date_input("To (UTC)", value=yesterday, max_value=yesterday,
                                     help=f"Up to {MAX_DAYS} days. Each day is a separate download.")
                days = [first + timedelta(n) for n in range((last - first).days + 1)] if last >= first else []
                if last < first:
                    st.caption("⚠️ 'To' must be on or after 'From'.")
                elif len(days) > MAX_DAYS:
                    st.caption(f"⚠️ That's {len(days)} days; the limit is {MAX_DAYS}.")
            pasted = ""
        else:
            pasted = st.text_area("Paste spot rows here", height=150)
            days = []

        load = st.button("Load spots", type="primary", use_container_width=True)

        st.divider()
        st.header("Filters")
        bands = ["All"] + list(BAND_COLORS)
        band_choice = st.selectbox("Band", bands, index=pick(bands, "band"))
        start_t, end_t = st.slider("UTC time window", value=(time(0, 0), time(23, 59)), format="HH:mm")
        min_snr = st.slider("Minimum SNR (dB)", 0, 40, cfg.get("min_snr", 0))

        st.divider()
        st.header("Map")
        styles = list(TILE_STYLES)
        tiles = st.selectbox("Style", styles, index=pick(styles, "tiles"))
        show_all = st.checkbox("Show all skimmers", value=cfg.get("show_all", False),
                               help="Adds a small grey dot for every RBN skimmer, including ones that didn't hear you.")
        unit_opts = ["mi", "km"]
        units = st.radio("Distance units", unit_opts, index=pick(unit_opts, "units"), horizontal=True)

        st.caption(f"🛰️ {refresh_msg}")

    save_settings({"callsign": callsign, "grid": grid_override, "source": source, "span": span if source == sources[0] else cfg.get("span"), "band": band_choice,
                   "min_snr": min_snr, "tiles": tiles, "show_all": show_all, "units": units})

    if load:
        try:
            if not callsign:
                raise RuntimeError("Enter a callsign first.")
            if len(days) > MAX_DAYS:
                raise RuntimeError(f"Please pick {MAX_DAYS} days or fewer.")
            ss.home = resolve_home(callsign, grid_override, skimmers)
            if source == "Paste from RBN site":
                if not pasted.strip():
                    raise RuntimeError("Paste some RBN spot rows, or switch to 'Download by date'.")
                df = parse_pasted_data(pasted)
                df = df[df["dx"] == callsign] if (df["dx"] == callsign).any() else df
                ss.file_date = datetime.now(timezone.utc).strftime("%Y%m%d")
            else:
                if not days:
                    raise RuntimeError("The 'To' date must be on or after the 'From' date.")
                frames, failed = [], []
                bar = st.progress(0.0, text="Downloading RBN data… (each day can take a minute)")
                for i, d in enumerate(days):
                    bar.progress(i / len(days), text=f"Downloading {d:%Y-%m-%d} ({i + 1} of {len(days)})…")
                    try:
                        frames.append(download_rbn_day(d.strftime("%Y%m%d"), callsign))
                    except Exception as e:
                        failed.append(f"{d:%Y-%m-%d}: {e}")
                bar.empty()
                if not frames:
                    raise RuntimeError("Download failed. " + " | ".join(failed))
                for msg in failed:
                    st.warning(f"Skipped {msg}")
                df = pd.concat(frames, ignore_index=True)
                ss.file_date = f"{days[0]:%Y%m%d}" + (f"-{days[-1]:%Y%m%d}" if len(days) > 1 else "")
            ss.raw, ss.callsign = df, callsign
        except Exception as e:
            ss.raw = None
            st.error(str(e))
    if ss.raw is None:
        st.info("👈 Enter your callsign and click **Load spots**. "
                "The map is centered on your callsign's registered location unless you enter a grid square.")
        return
    if ss.raw.empty:
        st.warning(f"RBN has no spots of {ss.callsign} for that date. Check the callsign, or try another day.")
        return

    spots = ss.raw
    spots = spots[(spots["time"].dt.time >= start_t) & (spots["time"].dt.time <= end_t) & (spots["snr"] >= min_snr)]
    if band_choice != "All":
        spots = spots[spots["band"] == band_choice]
    if spots.empty:
        st.warning("No spots match the current filters.")
        return

    lat, lon, label = ss.home
    home = (lat, lon)
    stats = compute_stats(spots, skimmers, home)
    missing = {s for s in spots["spotter"].unique() if skimmer_location(s, skimmers) is None}

    k = KM_PER_MILE if units == "mi" else 1
    c = st.columns(5)
    c[0].metric("Spots", f"{stats['spots']:,}")
    c[1].metric("Skimmers", f"{stats['skimmers']:,}")
    c[2].metric(f"Farthest ({units})", f"{stats['max_km'] / k:,.0f}", help=stats["farthest"])
    c[3].metric("Best SNR", f"{stats['max_snr']:.0f} dB")
    c[4].metric("Average SNR", f"{stats['avg_snr']:.1f} dB")
    st.caption(f"📍 {ss.callsign} · {label}"
               + (f" · ⚠️ {len(missing)} skimmer(s) not in location list, skipped" if missing else ""))

    m = build_map(spots, skimmers, home, label, ss.callsign, show_all, tiles, units, stats["farthest"])
    map_html = m.get_root().render()
    st.components.v1.html(map_html, height=720)

    left, right = st.columns([1, 4])
    left.download_button("⬇️ Download map", map_html, f"RBN_map_{ss.callsign}_{ss.file_date}.html",
                         "text/html", use_container_width=True)
    with st.expander("Spot table"):
        st.dataframe(spots.sort_values("time").assign(time=lambda d: d["time"].dt.strftime("%d %b %H:%M")),
                     use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
