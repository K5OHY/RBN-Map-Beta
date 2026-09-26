import json
import math
import re
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
from matplotlib.figure import Figure

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
# Weak = small green, medium = yellow, strong = large dark red. Typical RBN reports run ~5-35 dB.
SNR_MIN, SNR_MAX = 5, 35
SNR_COLORS = ["#22b14c", "#ffd60a", "#8b0000"]
SNR_CMAP = mcolors.LinearSegmentedColormap.from_list("snr", SNR_COLORS)
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

def snr_strength(snr):
    """0 (weak) .. 1 (strong)"""
    return float(np.clip((snr - SNR_MIN) / (SNR_MAX - SNR_MIN), 0, 1))


def snr_color(snr):
    return mcolors.to_hex(SNR_CMAP(snr_strength(snr)))


def snr_radius(snr):
    return 5 + 6 * snr_strength(snr)


def base_map(home, tiles):
    base, labels = TILE_STYLES[tiles]
    if base == "OpenStreetMap":
        return folium.Map(location=home, zoom_start=3, tiles=base, control_scale=True)
    m = folium.Map(location=home, zoom_start=3, tiles=None, control_scale=True)
    folium.TileLayer(base, attr=_ESRI_ATTR, name=tiles, max_zoom=16).add_to(m)
    if labels:
        folium.TileLayer(labels, attr=_ESRI_ATTR, name="Labels", overlay=True, max_zoom=16).add_to(m)
    return m


def fit_to(m, bounds):
    """Zoom the map to show every point in `bounds`. A map in a hidden Streamlit tab has zero size when it loads,
    so Leaflet would fit to nothing and zoom all the way in; fit again the moment the map becomes visible."""
    m.fit_bounds(bounds, padding=(30, 30))
    pts = [[float(lat), float(lon)] for lat, lon in bounds]
    m.get_root().script.add_child(folium.Element(f"""
      window.addEventListener('load', function () {{
        var map = {m.get_name()}, el = map.getContainer(), lastWidth = 0;
        new ResizeObserver(function () {{
          var w = el.clientWidth;
          if (w > 0 && lastWidth === 0) {{ map.invalidateSize(); map.fitBounds({pts}, {{padding: [30, 30]}}); }}
          lastWidth = w;
        }}).observe(el);
      }});"""))


def build_map(spots, skimmers, home, home_label, callsign, show_all, tiles, units, farthest,
              title=None, fit_spots=None):
    """`title` replaces the callsign in the legend; `fit_spots` also frames those spots in the view,
    so two maps can share the same extent."""
    k = KM_PER_MILE if units == "mi" else 1
    m = base_map(home, tiles)

    if show_all:
        layer = folium.FeatureGroup(name="All skimmers", show=True)
        for call, (lat, lon) in skimmers.items():
            folium.CircleMarker((lat, lon), radius=2, color="#555", weight=1, fill=True,
                                fill_opacity=0.6, tooltip=call).add_to(layer)
        layer.add_to(m)

    lines = folium.FeatureGroup(name="Paths", show=True)
    dots = folium.FeatureGroup(name="Spots (sized/coloured by SNR)", show=True)
    bounds = [home]

    for row in spots.sort_values("snr").itertuples():  # weakest first so strong dots draw on top
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
            radius=snr_radius(row.snr),
            color=snr_color(row.snr), weight=1.5, opacity=0.8, fill=True, fill_color=snr_color(row.snr), fill_opacity=0.55,
            popup=folium.Popup(
                f"<b>{row.spotter}</b><br>{row.band} &middot; {row.freq:.1f} kHz<br>"
                f"SNR: {row.snr:.0f} dB<br>{row.time:%d %b %H:%M} UTC<br>{distance_km(home, loc) / k:,.0f} {units}",
                max_width=220),
        ).add_to(dots)

    lines.add_to(m)
    dots.add_to(m)

    if fit_spots is not None:
        for spotter in fit_spots["spotter"].unique():
            loc = skimmer_location(spotter, skimmers)
            if loc:
                bounds.append(great_circle(home, loc)[-1])

    far_loc =skimmer_location(farthest, skimmers) if farthest else None
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
        fit_to(m, bounds)
    folium.LayerControl(collapsed=True).add_to(m)

    bands_present = spots["band"].value_counts()
    rows = "".join(
        f'<div><span style="display:inline-block;width:18px;height:4px;background:{BAND_COLORS.get(b, "#3388ff")};'
        f'margin-right:6px;vertical-align:middle;border-radius:2px"></span>{b} <span style="opacity:.6">({n})</span></div>'
        for b, n in sorted(bands_present.items(), key=lambda kv: list(BAND_COLORS).index(kv[0]) if kv[0] in BAND_COLORS else 99)
    )
    snr_key = "".join(
        f'<div style="text-align:center;width:26px"><div style="height:24px;display:flex;align-items:center;justify-content:center">'
        f'<span style="display:block;border-radius:50%;border:1.5px solid {snr_color(db)};'
        f'width:{2 * snr_radius(db):.0f}px;height:{2 * snr_radius(db):.0f}px;background:{snr_color(db)}88"></span></div>'
        f'<div style="opacity:.75">{db}{"+" if db == SNR_MAX else ""}</div></div>'
        for db in (5, 12, 20, 28, 35))
    legend = f"""
    <div style="position:fixed;bottom:34px;right:12px;z-index:9999;background:rgba(255,255,255,.92);
      padding:10px 12px;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.3);
      font:12px/1.5 system-ui,sans-serif;color:#222">
      <b>{title or callsign}</b> &middot; {len(spots)} spots<div style="margin:6px 0 2px;font-weight:600">Band</div>{rows}
      <div style="margin:8px 0 4px;font-weight:600">SNR (dB)</div>
      <div style="display:flex;justify-content:space-between;width:130px">{snr_key}</div>
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


SECTORS = 16
COMPASS_16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def bearing_chart(spots, skimmers, home):
    """Polar bar chart: bar length = number of spots in that direction, colour = average SNR there.
    Returns (figure, markdown summary) or None if no spot has a known skimmer location."""
    rows = []
    for row in spots.itertuples():
        loc = skimmer_location(row.spotter, skimmers)
        if loc:
            bearing = Geodesic.WGS84.Inverse(home[0], home[1], loc[0], loc[1])["azi1"] % 360
            rows.append((int(((bearing + 180 / SECTORS) % 360) // (360 / SECTORS)), row.snr))
    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["sector", "snr"])
    per = df.groupby("sector")["snr"].agg(["count", "mean"]).reindex(range(SECTORS))
    per["count"] = per["count"].fillna(0)

    fig = Figure(figsize=(4.4, 4.4))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot(projection="polar")
    ax.set_facecolor("none")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    grey = "#8a8f98"
    centers = np.radians(np.arange(SECTORS) * 360 / SECTORS)
    colors = [snr_color(m) if not np.isnan(m) else "#00000000" for m in per["mean"]]
    ax.bar(centers, per["count"], width=np.radians(360 / SECTORS) * 0.88, color=colors, alpha=0.75,
           edgecolor=colors, linewidth=1.2)
    ax.set_xticks(np.radians(np.arange(0, 360, 45)))
    ax.set_xticklabels(["N", "NE", "E", "SE", "S", "SW", "W", "NW"], color=grey, fontsize=11)
    ax.tick_params(axis="y", colors=grey, labelsize=8)
    ax.set_rlabel_position(22.5)
    ax.grid(color=grey, alpha=0.3)
    ax.spines["polar"].set_color(grey)
    ax.spines["polar"].set_alpha(0.3)

    top = int(per["count"].idxmax())
    best = int(per["mean"].idxmax())
    summary = (
        f"**Most spots:** {COMPASS_16[top]} ({int(per['count'][top])} spots)  \n"
        f"**Strongest on average:** {COMPASS_16[best]} ({per['mean'][best]:.0f} dB)  \n\n"
        "Bar length is the number of spots in that direction from your station. "
        "Colour is the average SNR, using the same scale as the map."
    )
    return fig, summary


# ----------------------------------------------------------------- compare mode

COLOR_A, COLOR_B, COLOR_TIE = "#2563eb", "#f97316", "#8a8f98"


def frequency_groups(spots, gap_khz):
    """Split spots into groups of nearby frequencies: a new group starts wherever the gap between
    neighbouring spot frequencies exceeds `gap_khz`. Returns [(median_freq, spots)] in frequency order."""
    ordered = spots.dropna(subset=["freq"]).sort_values("freq")
    group_id = (ordered["freq"].diff() > gap_khz).cumsum()
    return [(g["freq"].median(), g) for _, g in ordered.groupby(group_id)]


def skimmer_table(spots, skimmers, home):
    """One row per located skimmer: median SNR, spot count, distance (km) and bearing from `home`."""
    rows = []
    for spotter, g in spots.groupby("spotter"):
        loc = skimmer_location(spotter, skimmers)
        if loc is None:
            continue
        inv = Geodesic.WGS84.Inverse(home[0], home[1], loc[0], loc[1])
        rows.append((spotter, g["snr"].median(), len(g), inv["s12"] / 1000, inv["azi1"] % 360))
    return pd.DataFrame(rows, columns=["skimmer", "snr", "spots", "km", "bearing"]).set_index("skimmer")


def _themed_axes(fig, polar=False):
    fig.patch.set_alpha(0)
    ax = fig.add_subplot(projection="polar") if polar else fig.add_subplot()
    ax.set_facecolor("none")
    for spine in ax.spines.values():
        spine.set_color(COLOR_TIE)
        spine.set_alpha(0.3)
    ax.tick_params(colors=COLOR_TIE, labelsize=8)
    ax.grid(color=COLOR_TIE, alpha=0.3)
    return ax


SECTOR_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
DIRECTION_EDGE_DB = 2  # a direction only counts as "stronger" for one frequency past this many dB


def compare_snr(shared):
    """(average A, average B, winner) over the skimmers that heard both. The winner is 'A' or 'B', or None
    when there are too few skimmers or the difference is within the noise."""
    n = len(shared)
    if n == 0:
        return None, None, None
    d = shared["delta"]
    avg_a, avg_b = shared["snr_a"].mean(), shared["snr_b"].mean()
    if n < 3 or abs(d.mean()) <= 1.96 * d.std(ddof=1) / math.sqrt(n):  # ~95% confidence range
        return avg_a, avg_b, None
    return avg_a, avg_b, "A" if d.mean() > 0 else "B"


def headline(reach_winner, snr_winner, names):
    """(streamlit level, message): the overall answer in one sentence."""
    wins = {"A": [], "B": []}
    if reach_winner:
        wins[reach_winner].append("reached more skimmers")
    if snr_winner:
        wins[snr_winner].append("had the stronger signal")
    if wins["A"] and wins["B"]:
        return "info", f"**Mixed result:** {names['A']} {wins['A'][0]}, but {names['B']} {wins['B'][0]}."
    for side in "AB":
        if wins[side]:
            return "success", f"**{names[side]} is getting out better:** it " + " and ".join(wins[side]) + "."
    return "info", "**Too close to call.** Neither frequency is clearly ahead."


def sector_means(table):
    """Average per-skimmer SNR in each of the 8 compass directions (NaN where no skimmer heard you)."""
    if table.empty:
        return pd.Series(np.nan, index=range(8))
    sector = ((table["bearing"] + 22.5) % 360 // 45).astype(int)
    return table.groupby(sector)["snr"].mean().reindex(range(8))


def direction_chart(means_a, means_b, name_a, name_b):
    """Radar chart: distance from the centre = average SNR in that direction, one shape per frequency."""
    fig = Figure(figsize=(4.6, 4.6))
    ax = _themed_axes(fig, polar=True)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    angles = np.radians(np.arange(8) * 45)
    closed = np.append(angles, angles[0])
    for means, color, name in ((means_a, COLOR_A, name_a), (means_b, COLOR_B, name_b)):
        r = means.fillna(0).to_numpy()
        r = np.append(r, r[0])
        ax.plot(closed, r, color=color, linewidth=2.2, label=name)
        ax.fill(closed, r, color=color, alpha=0.18)
    ax.set_xticks(angles)
    ax.set_xticklabels(SECTOR_NAMES, color=COLOR_TIE, fontsize=12)
    ax.set_ylim(0, None)
    ax.set_rlabel_position(22.5)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.18), ncol=2, frameon=False, labelcolor=COLOR_TIE)
    return fig


def direction_summary(means_a, means_b, names):
    """Markdown: which directions each frequency is stronger toward."""
    both = means_a.notna() & means_b.notna()
    diff = (means_a - means_b)[both]

    def dirs(mask):
        return ", ".join(SECTOR_NAMES[i] for i in mask.index[mask])

    lines = []
    for side, mask, icon in (("A", diff >= DIRECTION_EDGE_DB, "🔵"), ("B", diff <= -DIRECTION_EDGE_DB, "🟠")):
        if mask.any():
            lines.append(f"{icon} **{names[side]} is stronger toward:** {dirs(mask)}")
    for side, mine, other, icon in (("A", means_a, means_b, "🔵"), ("B", means_b, means_a, "🟠")):
        only = mine.notna() & other.isna()
        if only.any():
            lines.append(f"{icon} **Only {names[side]} was heard toward:** {dirs(only)}")
    if not lines:
        lines.append("No clear difference by direction.")
    return "\n\n".join(lines)


def best_direction(means):
    return f"{SECTOR_NAMES[int(means.idxmax())]} ({means.max():.0f} dB)" if means.notna().any() else "—"


def head_to_head_table(shared, units):
    """Styled table of the skimmers that heard both frequencies, with the stronger side's SNR tinted."""
    k = KM_PER_MILE if units == "mi" else 1
    dist, col_a, col_b, col_d = f"Distance ({units})", "🔵 A SNR (dB)", "🟠 B SNR (dB)", "Difference A − B (dB)"
    view = pd.DataFrame({
        dist: (shared["km_a"] / k).round(0).astype(int),
        col_a: shared["snr_a"], col_b: shared["snr_b"], col_d: shared["delta"],
    }).sort_values(dist, ascending=False)
    view["Stronger"] = np.where(view[col_d] > 0, "A", np.where(view[col_d] < 0, "B", "Tie"))
    view.index.name = "Skimmer"

    def tint(row):
        style = [""] * len(row)
        for side, col, color in (("A", col_a, COLOR_A), ("B", col_b, COLOR_B)):
            if row["Stronger"] == side:
                style[view.columns.get_loc(col)] = f"background-color:{color}33;font-weight:600"
        return style
    return view.style.apply(tint, axis=1).format({col_a: "{:g}", col_b: "{:g}", col_d: "{:+g}", dist: "{:,}"})


def scoreboard_html(name_a, name_b, rows):
    """HTML table, one row per measure, with the winning cell highlighted.
    rows: (label, note, (value_a, note_a), (value_b, note_b), winner 'A'/'B'/None)"""
    def cell(side, value, note, winner):
        color = COLOR_A if side == "A" else COLOR_B
        won = winner == side
        style = f"background:{color}26;border-left:4px solid {color}" if won else "border-left:4px solid transparent"
        sub = f'<div style="opacity:.6;font-size:.8rem">{note}</div>' if note else ""
        return (f'<td style="padding:12px 16px;{style}"><span style="font-size:1.5rem;font-weight:{700 if won else 400}">'
                f'{value}</span>{" &nbsp;✔" if won else ""}{sub}</td>')

    body = "".join(
        f'<tr style="border-top:1px solid rgba(128,128,128,.25)"><td style="padding:12px 16px">{label}'
        f'<div style="opacity:.6;font-size:.8rem">{note}</div></td>{cell("A", *a, w)}{cell("B", *b, w)}</tr>'
        for label, note, a, b, w in rows)
    return (f'<table style="width:100%;border-collapse:collapse"><tr>'
            f'<th style="text-align:left;padding:8px 16px"></th>'
            f'<th style="text-align:left;padding:8px 16px;color:{COLOR_A}">🔵 {name_a}</th>'
            f'<th style="text-align:left;padding:8px 16px;color:{COLOR_B}">🟠 {name_b}</th></tr>{body}</table>')


def compare_view(spots, skimmers, home, label, callsign, file_date, tiles, show_all, units, gap_khz):
    """Compare mode: split `spots` into frequency groups, pick two, and show which one is getting out better."""
    groups = frequency_groups(spots, gap_khz)
    freqs = ", ".join(f"{f:.1f}" for f, _ in groups)
    if len(groups) < 2:
        st.warning(f"Found only {len(groups)} frequency group ({freqs or 'none'} kHz). Compare mode needs spots on at "
                   f"least two frequencies. If your tests were close together, lower **Frequency gap** in the sidebar.")
        return

    def option_label(i):
        f, g = groups[i]
        return f"{f:.1f} kHz · {len(g)} spots · {g['time'].min():%H:%M}–{g['time'].max():%H:%M} UTC"

    top_two = sorted(sorted(range(len(groups)), key=lambda i: -len(groups[i][1]))[:2])
    pick_a, pick_b = st.columns(2)
    ia = pick_a.selectbox("🔵 Frequency A", range(len(groups)), index=top_two[0], format_func=option_label)
    ib = pick_b.selectbox("🟠 Frequency B", range(len(groups)), index=top_two[1], format_func=option_label)
    if ia == ib:
        st.warning("Pick two different frequencies to compare.")
        return

    (fa, spots_a), (fb, spots_b) = groups[ia], groups[ib]
    names = {"A": f"A ({fa:.1f} kHz)", "B": f"B ({fb:.1f} kHz)"}
    table_a, table_b = skimmer_table(spots_a, skimmers, home), skimmer_table(spots_b, skimmers, home)
    shared = table_a.join(table_b, how="inner", lsuffix="_a", rsuffix="_b")
    shared["delta"] = shared["snr_a"] - shared["snr_b"]

    tab_results, tab_maps = st.tabs(["📊 Results", "🗺️ Side-by-side maps"])

    with tab_results:
        heard_a, heard_b = set(spots_a["spotter"]), set(spots_b["spotter"])
        reach_winner = "A" if len(heard_a) > len(heard_b) else "B" if len(heard_b) > len(heard_a) else None
        avg_a, avg_b, snr_winner = compare_snr(shared)
        level, message = headline(reach_winner, snr_winner, names)
        getattr(st, level)(message)

        means_a, means_b = sector_means(table_a), sector_means(table_b)
        snr_note = f"at the {len(shared)} skimmers that heard both" if len(shared) else "no skimmer heard both"
        st.markdown(scoreboard_html(names["A"], names["B"], [
            ("Skimmers that heard you", "more is better",
             (len(heard_a), f"{len(heard_a - heard_b)} heard only this one"),
             (len(heard_b), f"{len(heard_b - heard_a)} heard only this one"), reach_winner),
            ("Average SNR", snr_note,
             ("—" if avg_a is None else f"{avg_a:.1f} dB", ""),
             ("—" if avg_b is None else f"{avg_b:.1f} dB", ""), snr_winner),
            ("Strongest direction", "where your signal was best",
             (best_direction(means_a), ""), (best_direction(means_b), ""), None),
        ]), unsafe_allow_html=True)
        st.caption("Keep tests close together in time: propagation drifts, so a gap of an hour or more can make one "
                   "frequency look better for reasons that have nothing to do with the antenna.")

        if means_a.notna().any() or means_b.notna().any():
            st.subheader("Direction")
            chart_col, text_col = st.columns([2, 3])
            chart_col.pyplot(direction_chart(means_a, means_b, names["A"], names["B"]), use_container_width=True)
            text_col.markdown(direction_summary(means_a, means_b, names))
            text_col.caption("Distance from the centre is the average SNR toward that compass direction. "
                             "A bigger shape in a direction means a stronger signal that way.")

        if len(shared):
            wins_a, wins_b = int((shared["delta"] > 0).sum()), int((shared["delta"] < 0).sum())
            st.subheader("Same skimmer, both frequencies")
            st.caption(f"{len(shared)} skimmers heard both. 🔵 A was stronger at {wins_a}, 🟠 B at {wins_b}, "
                       f"tied at {len(shared) - wins_a - wins_b}. Each SNR is the median of that skimmer's spots. "
                       "Click a column heading to sort.")
            st.dataframe(head_to_head_table(shared, units), use_container_width=True,
                         height=min(38 * (len(shared) + 1), 420))

    with tab_maps:
        st.caption("Both maps use the same view, the same SNR scale and the same dot sizes.")
        both = pd.concat([spots_a, spots_b])
        col_a, col_b = st.columns(2)
        for col, side, freq, group in ((col_a, "A", fa, spots_a), (col_b, "B", fb, spots_b)):
            html = build_map(group, skimmers, home, label, callsign, show_all, tiles, units,
                             compute_stats(group, skimmers, home)["farthest"],
                             title=f"{callsign} · {freq:.1f} kHz", fit_spots=both).get_root().render()
            with col:
                st.markdown(f"#### {'🔵' if side == 'A' else '🟠'} {names[side]}")
                st.components.v1.html(html, height=620)
                st.download_button(f"⬇️ Download map {side}", html, f"RBN_map_{callsign}_{file_date}_{freq:.1f}kHz.html",
                                   "text/html", key=f"dl_{side}")



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

    # Read saved settings once per session. Re-reading them on every rerun changes each widget's
    # default, which Streamlit treats as a brand-new widget and resets, swallowing the first click.
    if "cfg" not in ss:
        ss.cfg = load_settings()
    cfg = ss.cfg

    def pick(options, key, default=None):
        """Index of the saved choice in `options` (falls back to the first/default)."""
        value = cfg.get(key, default if default is not None else options[0])
        return options.index(value) if value in options else 0

    with st.sidebar:
        st.header("Your signal")
        callsign = st.text_input("Callsign", value=cfg.get("callsign", ""), placeholder="Enter your callsign").strip().upper()
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
        st.header("Compare mode")
        compare = st.checkbox("Compare two frequencies", value=False,
                              help="Splits your spots by frequency so you can compare two tests, for example two antennas. "
                                   "Load spots from a period that includes both tests.")
        gap_khz = st.slider("Frequency gap (kHz)", 0.1, 5.0, 0.5, 0.1, disabled=not compare,
                            help="Spots closer together than this count as the same frequency. Skimmers report slightly "
                                 "different frequencies for the same signal. Lower it if two tests are close together.")

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
    if compare:
        st.caption(f"📍 {ss.callsign} · {label}")
        compare_view(spots, skimmers, home, label, ss.callsign, ss.file_date, tiles, show_all, units, gap_khz)
        return
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
               + (f" · ⚠️ No location for {', '.join(sorted(missing)[:5])}"
                  f"{f' and {len(missing) - 5} more' if len(missing) > 5 else ''}"
                  f" (not in RBN's skimmer list), so not shown on the map" if missing else ""))

    m = build_map(spots, skimmers, home, label, ss.callsign, show_all, tiles, units, stats["farthest"])
    map_html = m.get_root().render()
    st.components.v1.html(map_html, height=720)

    left, right = st.columns([1, 4])
    left.download_button("⬇️ Download map", map_html, f"RBN_map_{ss.callsign}_{ss.file_date}.html",
                         "text/html", use_container_width=True)
    st.subheader("Direction of your spots")
    chart_col, text_col = st.columns([2, 3])
    result = bearing_chart(spots, skimmers, home)
    if result:
        fig, summary = result
        chart_col.pyplot(fig, use_container_width=True)
        text_col.markdown(summary)
    else:
        chart_col.caption("No located skimmers to chart.")

    with st.expander("Spot table"):
        st.dataframe(spots.sort_values("time").assign(time=lambda d: d["time"].dt.strftime("%d %b %H:%M")),
                     use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
