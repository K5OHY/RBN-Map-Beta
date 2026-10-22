import json
import re
import zipfile
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path

import folium
import matplotlib.colors as mcolors
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import requests
import streamlit as st
from geographiclib.geodesic import Geodesic
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from compare_stats import (
    BIN_MIN_SHARED,
    DALE_MIN_SPOTS,
    DALE_SITE_MIN,
    station_gains,
    gain_by_window,
    DIRECTION_EDGE_DB,
    gain_summary,
    WSPR_FLOOR_DB,
    SECTOR_NAMES,
    EQUAL_DB,
    SLOT_NOISE_DB,
    analyze,
    build_verdict,
    count_pairs,
    default_labels,
    direction_advantage,
    db_from_watts,
    eligible_receivers,
    locate as skimmer_location,
    median_power_dbm,
    power_method,
    t_crit,
    receiver_table,
    _farthest,
    _lead_text,
    round_consistency,
    split_by_labels,
)
from report_pdf import build_pdf
from world_map import MAX_KM, MIN_CELL, azimuth_map_figure
import session_report as sr
import conditions as cond
import space_weather as sw
import dashboard as dash
import ionosphere as iono
from contextlib import nullcontext
from rbn_data import (
    get_band,
    load_skimmers,
    lookup_callsign_location,
    maidenhead_to_latlon,
    refresh_skimmer_cache,
)
from wspr_data import WsprError, clean_callsign, fetch_listening, fetch_wspr_spots, receiver_locations

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
SNR_COLORS = ["#22b14c", "#ffd60a", "#8b0000"]
SNR_CMAP = mcolors.LinearSegmentedColormap.from_list("snr", SNR_COLORS)
KM_PER_MILE = 1.609344
MAX_DAYS = 7
SETTINGS_FILE = Path(__file__).with_name("settings.json")


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

def lon_near(lon, ref_lon):
    """`lon` shifted by a multiple of 360 to sit within 180 degrees of `ref_lon`. The map repeats sideways, and
    paths are drawn on the copy of the world nearest the home station, so markers must be placed there too."""
    return ref_lon + (lon - ref_lon + 180) % 360 - 180


def snr_strength(snr, scale=RBN_SNR_SCALE):
    """0 (weak) .. 1 (strong)"""
    lo, hi = scale
    return float(np.clip((snr - lo) / (hi - lo), 0, 1))


def snr_color(snr, scale=RBN_SNR_SCALE):
    return mcolors.to_hex(SNR_CMAP(snr_strength(snr, scale)))


def snr_radius(snr, scale=RBN_SNR_SCALE):
    return 5 + 6 * snr_strength(snr, scale)


def spot_history_svg(g, gid_seed, w=240, h=100, scale=RBN_SNR_SCALE):
    """Small SNR-over-time chart for one skimmer's repeat spots: a shaded line, its own vertical
    scale (so a narrow swing still fills the chart), coloured on the map's absolute SNR scale so it
    reads as an extension of the dots, with the best spot highlighted. Points are spaced by actual
    elapsed time (not by count), so a gap in testing shows up as a gap, and a day change is labelled
    instead of silently folding into the same HH:MM axis as everything else."""
    g = g.sort_values("time").reset_index(drop=True)
    n = len(g)
    pad_l, pad_r, pad_t, pad_b = 8, 8, 22, 18
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    lo, hi = g["snr"].min(), g["snr"].max()
    span = max(hi - lo, 1)
    span_s = (g["time"].iloc[-1] - g["time"].iloc[0]).total_seconds() or 1
    multi_day = g["time"].dt.date.nunique() > 1
    fmt = "%d %b %H:%M" if multi_day else "%H:%M"

    if n == 1:
        xs = [pad_l + plot_w / 2]
    else:
        xs = [pad_l + ((row.time - g["time"].iloc[0]).total_seconds() / span_s) * plot_w for row in g.itertuples()]
    ys = [pad_t + plot_h - ((row.snr - lo) / span) * plot_h * 0.82 - plot_h * 0.09 for row in g.itertuples()]
    peak_i = int(g["snr"].values.argmax())
    peak_color = snr_color(hi, scale)

    day_breaks = []
    if multi_day:
        prev_date = g["time"].iloc[0].date()
        for x, row in zip(xs, g.itertuples()):
            if row.time.date() != prev_date:
                day_breaks.append((x, row.time))
                prev_date = row.time.date()

    path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area_d = f"{path_d} L {xs[-1]:.1f},{pad_t + plot_h:.1f} L {xs[0]:.1f},{pad_t + plot_h:.1f} Z"
    gid = "snrgrad_" + re.sub(r"[^A-Za-z0-9]", "", gid_seed)
    defs = (f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="{peak_color}" stop-opacity="0.5"/>'
            f'<stop offset="100%" stop-color="{peak_color}" stop-opacity="0.02"/></linearGradient></defs>')

    # Skip a date label that would land on the peak's own value label (same collision that motivated
    # day tabs in the first place) - the dashed line alone still marks the boundary, and every date
    # is named on its own tab besides.
    peak_x = xs[peak_i]
    breaks = "".join(
        f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h:.1f}" stroke="currentColor" '
        f'stroke-opacity="0.25" stroke-dasharray="2 2"/>'
        + (f'<text x="{x:.1f}" y="{pad_t - 2}" font-size="7.5" text-anchor="middle" fill="currentColor" '
           f'opacity="0.6">{t:%d %b}</text>' if abs(x - peak_x) > 16 else "")
        for x, t in day_breaks)

    points, labels = [], []
    for i, (x, y, row) in enumerate(zip(xs, ys, g.itertuples())):
        color, is_peak = snr_color(row.snr, scale), i == peak_i
        r = 5.5 if is_peak else 3.4
        glow = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r + 4}" fill="{color}" opacity="0.25"/>' if is_peak else ""
        points.append(f'{glow}<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{color}" stroke="#fff" '
                       f'stroke-opacity="{0.85 if is_peak else 0}" stroke-width="1.2">'
                       f'<title>{row.time:{fmt}} UTC &middot; {row.snr:.0f} dB</title></circle>')
        if is_peak or i in (0, n - 1):
            labels.append(f'<text x="{x:.1f}" y="{y - (9 if is_peak else 8):.1f}" '
                           f'font-size="{9.5 if is_peak else 8.5}" font-weight="{700 if is_peak else 400}" '
                           f'text-anchor="middle" fill="{color if is_peak else "currentColor"}" '
                           f'opacity="{1 if is_peak else 0.65}">{row.snr:.0f}</text>')
    time_labels = (f'<text x="{xs[0]:.1f}" y="{h - 4}" font-size="8" fill="currentColor" opacity="0.5">'
                   f'{g["time"].iloc[0]:{fmt}}</text>'
                   f'<text x="{xs[-1]:.1f}" y="{h - 4}" font-size="8" text-anchor="end" fill="currentColor" '
                   f'opacity="0.5">{g["time"].iloc[-1]:{fmt}}</text>')
    title = (f'<text x="{pad_l}" y="12" font-size="9.5" font-weight="600" fill="currentColor" '
             f'opacity="0.75">SNR over time</text>')

    return (f'<svg width="{w}" height="{h}" style="display:block;color:inherit;overflow:visible">{defs}{title}'
            f'<path d="{area_d}" fill="url(#{gid})" stroke="none"/>'
            f'<path d="{path_d}" fill="none" stroke="{peak_color}" stroke-width="1.6" stroke-opacity="0.55" '
            f'stroke-linejoin="round"/>' + breaks + "".join(points) + "".join(labels) + time_labels + "</svg>")


def _stats_line(g):
    return (f"{len(g)} spots &middot; median {g['snr'].median():.0f} dB &middot; "
            f"range {g['snr'].min():.0f}&ndash;{g['snr'].max():.0f} dB")


def _single_spot_html(row, scale=RBN_SNR_SCALE):
    """A lone spot has no trend to chart, but it still gets the same colour-coded strength cue as
    every dot on the map and every point in a multi-spot chart, instead of dropping to bare text."""
    color = snr_color(row["snr"], scale)
    return (f'<div style="display:flex;align-items:center;gap:9px;margin:4px 0 2px 0">'
            f'<span style="display:inline-block;width:15px;height:15px;border-radius:50%;flex:none;'
            f'background:{color};box-shadow:0 0 0 4px {color}2a"></span>'
            f'<span><b>{row["snr"]:.0f} dB</b> &middot; {row["time"]:%d %b %H:%M} UTC</span></div>')


MAX_DAY_TABS = 12  # beyond this, a row of date pills stops being useful; fall back to the compressed view


def _history_block(g, gid_seed, scale=RBN_SNR_SCALE):
    """Chart plus stats for one skimmer's (band's) spots. A burst of close-together spots inside a
    test spanning many days used to get compressed to a sliver on one shared axis - unreadable, and
    prone to a peak label landing on a date divider. Splitting into a same-style tab per day fixes
    both: each day's tab gets the full chart width to itself, at only that day's own time scale."""
    if len(g) == 1:
        return _single_spot_html(g.iloc[0], scale)

    days = sorted(g["time"].dt.date.unique())
    if len(days) == 1 or len(days) > MAX_DAY_TABS:
        return spot_history_svg(g, gid_seed, scale=scale) + _stats_line(g)

    gid = "daytabs_" + re.sub(r"[^A-Za-z0-9]", "", gid_seed)
    options = [("All", g)] + [(f"{d:%d %b}", g[g["time"].dt.date == d]) for d in days]
    inputs, tabs, panels, rules = [], [], [], []
    for i, (label, dg) in enumerate(options):
        inputs.append(f'<input type="radio" name="{gid}" id="{gid}_{i}" style="display:none"'
                      + (" checked>" if i == 0 else ">"))
        tabs.append(f'<label for="{gid}_{i}" id="{gid}_tab{i}" class="{gid}_tab">{label} ({len(dg)})</label>')
        if len(dg) > 1:
            panel_content = spot_history_svg(dg, f"{gid_seed}{i}", scale=scale) + _stats_line(dg)
        else:
            panel_content = _single_spot_html(dg.iloc[0], scale)
        panels.append(f'<div id="{gid}_panel{i}">{panel_content}</div>')
        rules.append(f'#{gid}_{i}:checked ~ #{gid}_tab{i} {{background:#8888}}')
        rules += [f'#{gid}_{i}:checked ~ #{gid}_panel{j} {{display:none}}' for j in range(len(options)) if j != i]
    style = (f'<style>.{gid}_tab {{display:inline-block;padding:2px 7px;margin:0 3px 6px 0;'
             f'border-radius:10px;font-size:10.5px;font-weight:600;cursor:pointer;background:#8883}}'
             + "".join(rules) + "</style>")
    return style + "".join(inputs) + '<div style="line-height:2.1">' + "".join(tabs) + "</div>" + "".join(panels)


def _band_panel_html(spotter, band, g, gid_seed, scale=RBN_SNR_SCALE):
    """One band's worth of content for a popup: frequency range, then the (possibly day-tabbed) history."""
    best = g.loc[g["snr"].idxmax()]
    freq_label = (f"{best['freq']:.1f} kHz" if g["freq"].nunique() == 1
                  else f"{g['freq'].min():.1f}&ndash;{g['freq'].max():.1f} kHz")
    sub = f'<div style="opacity:.7;font-size:12px;margin:2px 0 6px 0">{band} &middot; {freq_label}</div>'
    return sub + _history_block(g, f"{gid_seed}{band}", scale)


def spot_popup_html(spotter, g, dist, units, scale=RBN_SNR_SCALE):
    """Popup for one skimmer, across every band it heard this session on. A single band renders
    directly; more than one gets small tab pills (pure CSS radio trick, no JS) so switching bands
    doesn't need a second marker fighting for the same map coordinate.

    The tabs use an id per label (not :nth-of-type sibling counting) so the "which tab is active"
    and "which panel to hide" rules stay simple, explicit ID selectors, which also beats needing
    !important: an unadorned base class rule sets the resting look, and each `#radio:checked ~
    #label` override rule outranks it on specificity alone because it includes an ID."""
    header = f"<b>{spotter}</b><br>{dist:,.0f} {units}"
    bands = sorted(g["band"].unique(), key=lambda b: -len(g[g["band"] == b]))  # busiest band first
    if len(bands) == 1:
        return header + "<br>" + _band_panel_html(spotter, bands[0], g, spotter, scale)

    gid = "bandtabs_" + re.sub(r"[^A-Za-z0-9]", "", spotter)
    base_style = (f'<style>.{gid}_tab {{display:inline-block;padding:3px 9px;margin:0 4px 6px 0;'
                  f'border-radius:12px;font-size:11.5px;font-weight:600;cursor:pointer;background:#8883}}')
    inputs, tabs, panels, rules = [], [], [], []
    for i, band in enumerate(bands):
        bg = g[g["band"] == band]
        inputs.append(f'<input type="radio" name="{gid}" id="{gid}_{i}" style="display:none"'
                      + (" checked>" if i == 0 else ">"))
        tabs.append(f'<label for="{gid}_{i}" id="{gid}_tab{i}" class="{gid}_tab">{band} ({len(bg)})</label>')
        panels.append(f'<div id="{gid}_panel{i}">{_band_panel_html(spotter, band, bg, spotter, scale)}</div>')
        rules.append(f'#{gid}_{i}:checked ~ #{gid}_tab{i} {{background:{BAND_COLORS.get(band, "#3388ff")}55}}')
        rules += [f'#{gid}_{i}:checked ~ #{gid}_panel{j} {{display:none}}' for j in range(len(bands)) if j != i]
    style = base_style + "".join(rules) + "</style>"
    return header + "<br>" + style + "".join(inputs) + "".join(tabs) + "".join(panels)


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
              title=None, fit_spots=None, scale=RBN_SNR_SCALE, noun="skimmer"):
    """`title` replaces the callsign in the legend; `fit_spots` also frames those spots in the view,
    so two maps can share the same extent. `scale` is the (weak, strong) SNR range the colours span;
    `noun` is what the stations that heard you are called (skimmer for RBN, receiver for WSPR)."""
    k = KM_PER_MILE if units == "mi" else 1
    m = base_map(home, tiles)

    if show_all:
        layer = folium.FeatureGroup(name=f"All {noun}s", show=True)
        for call, (lat, lon) in skimmers.items():
            folium.CircleMarker((lat, lon_near(lon, home[1])), radius=2, color="#555", weight=1, fill=True,
                                fill_opacity=0.6, tooltip=call).add_to(layer)
        layer.add_to(m)

    lines = folium.FeatureGroup(name="Paths", show=True)
    dots = folium.FeatureGroup(name=f"{noun.capitalize()}s (sized/coloured by best SNR)", show=True)
    bounds = [home]

    # One path per skimmer per band it reached on (so each band it hit still gets its own colour)...
    for (spotter, band), g in spots.groupby(["spotter", "band"], sort=False):
        loc = skimmer_location(spotter, skimmers)
        if loc is None:
            continue
        path = great_circle(home, loc)
        bounds.extend([path[0], path[-1]])
        folium.PolyLine(path, color=BAND_COLORS.get(band, "#3388ff"), weight=2, opacity=0.75).add_to(lines)

    # ...but one marker per skimmer, full stop, across every band. Grouping by band too would put a
    # skimmer heard on both 20m and 40m back at the original bug: two markers stacked on the exact
    # same coordinate, only the top one clickable. A multi-band popup gets its own tabs instead.
    groups = sorted(spots.groupby("spotter", sort=False), key=lambda kv: kv[1]["snr"].max())
    for spotter, g in groups:  # weakest-best skimmer first so strong dots draw on top
        loc = skimmer_location(spotter, skimmers)
        if loc is None:
            continue
        end = great_circle(home, loc)[-1]
        bounds.append(end)
        best_snr = g["snr"].max()

        folium.CircleMarker(
            end,
            radius=snr_radius(best_snr, scale),
            color=snr_color(best_snr, scale), weight=1.5, opacity=0.8, fill=True,
            fill_color=snr_color(best_snr, scale), fill_opacity=0.55,
            popup=folium.Popup(spot_popup_html(spotter, g, distance_km(home, loc) / k, units, scale), max_width=290),
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
        f'<span style="display:block;border-radius:50%;border:1.5px solid {snr_color(db, scale)};'
        f'width:{2 * snr_radius(db, scale):.0f}px;height:{2 * snr_radius(db, scale):.0f}px;'
        f'background:{snr_color(db, scale)}88"></span></div>'
        f'<div style="opacity:.75">{db}{"+" if db == scale[1] else ""}</div></div>'
        for db in (int(round(x)) for x in np.linspace(scale[0], scale[1], 5)))
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


def bearing_chart(spots, skimmers, home, scale=RBN_SNR_SCALE):
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
    colors = [snr_color(m, scale) if not np.isnan(m) else "#00000000" for m in per["mean"]]
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

# The ways compare mode can tell the A side from the B side (these are the labels people see).
SPLIT_FREQ = "Two frequencies"
SPLIT_LOG = "Antenna log"

COLOR_A, COLOR_B, COLOR_TIE = "#2563eb", "#f97316", "#8a8f98"


def frequency_groups(spots, gap_khz):
    """Split spots into groups of nearby frequencies: a new group starts wherever the gap between
    neighbouring spot frequencies exceeds `gap_khz`. Returns [(median_freq, spots)] in frequency order."""
    ordered = spots.dropna(subset=["freq"]).sort_values("freq")
    group_id = (ordered["freq"].diff() > gap_khz).cumsum()
    return [(g["freq"].median(), g) for _, g in ordered.groupby(group_id)]


def _themed_axes(fig, polar=False):
    fig.patch.set_alpha(0)
    ax = fig.add_subplot(projection="polar") if polar else fig.add_subplot()
    _style_axes(ax)
    return ax


def sector_means(table, min_receivers=1):
    """Average per-skimmer SNR in each of the 8 compass directions (NaN where no skimmer heard you, or fewer than
    `min_receivers` did, so one far-off outlier can't stretch the chart)."""
    if table.empty:
        return pd.Series(np.nan, index=range(8))
    sector = ((table["bearing"] + 22.5) % 360 // 45).astype(int)
    grouped = table.groupby(sector)["snr"]
    means, counts = grouped.mean().reindex(range(8)), grouped.size().reindex(range(8)).fillna(0)
    return means.where(counts >= min_receivers)


def direction_chart(means_a, means_b, name_a, name_b):
    """Radar chart: distance from the centre = average SNR in that direction, one shape per frequency.
    The centre is the weakest SNR (not 0 dB), so WSPR's negative values plot too; ring labels show real dB."""
    fig = Figure(figsize=(4.6, 4.6))
    ax = _themed_axes(fig, polar=True)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    seen = pd.concat([means_a, means_b]).dropna()
    base = min(0.0, float(np.floor(seen.min())) - 2) if len(seen) else 0.0
    angles = np.radians(np.arange(8) * 45)
    closed = np.append(angles, angles[0])
    for means, color, name in ((means_a, COLOR_A, name_a), (means_b, COLOR_B, name_b)):
        if not means.notna().any():
            continue
        r = (means - base).fillna(0).to_numpy()
        r = np.append(r, r[0])
        ax.plot(closed, r, color=color, linewidth=2.2, label=name)
        ax.fill(closed, r, color=color, alpha=0.18)
    ax.set_xticks(angles)
    ax.set_xticklabels(SECTOR_NAMES, color=COLOR_TIE, fontsize=12)
    ax.set_ylim(0, None)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v + base:.0f}"))
    ax.set_rlabel_position(22.5)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.18), ncol=2, frameon=False, labelcolor=COLOR_TIE)
    return fig


def best_direction(means):
    return f"{SECTOR_NAMES[int(means.idxmax())]} ({means.max():.0f} dB)" if means.notna().any() else "—"


def head_to_head_table(shared, units):
    """Styled table of the skimmers that heard both frequencies, with the stronger side's SNR tinted."""
    k = KM_PER_MILE if units == "mi" else 1
    dist, col_a, col_b, col_d = f"Distance ({units})", "🔵 A SNR (dB)", "🟠 B SNR (dB)", "Gain B−A (dB)"
    view = pd.DataFrame({
        dist: (shared["km_a"] / k).round(0).astype(int),
        col_a: shared["snr_a"], col_b: shared["snr_b"], col_d: -shared["gap"],  # B's SNR minus A's: positive = B stronger
    }).sort_values(dist, ascending=False)
    view["Stronger"] = np.where(shared["gap"].reindex(view.index) > 0, "A",
                                np.where(shared["gap"].reindex(view.index) < 0, "B", "Tie"))
    view = view[[dist, col_d, col_a, col_b, "Stronger"]]
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


def reach_bar_html(only_a, both, only_b):
    """One bar split into 'only A | both | only B', each segment as wide as its share of unique skimmers."""
    def seg(count, color, text_color, label):
        if not count:
            return ""
        return (f'<div title="{label}: {count}" style="flex:{count} 1 0;min-width:2.6rem;background:{color};'
                f'color:{text_color};padding:10px 0;text-align:center;font-weight:700;font-size:1.1rem">{count}</div>')

    bar = (seg(only_a, COLOR_A, "#fff", "Only A") + seg(both, "#6b7280", "#fff", "Both")
           + seg(only_b, COLOR_B, "#111", "Only B"))
    legend = (f'<span style="color:{COLOR_A}">■</span> Only A heard you: <b>{only_a}</b> &nbsp;&nbsp; '
              f'<span style="color:#6b7280">■</span> Heard both: <b>{both}</b> &nbsp;&nbsp; '
              f'<span style="color:{COLOR_B}">■</span> Only B heard you: <b>{only_b}</b>')
    return (f'<div style="display:flex;border-radius:8px;overflow:hidden;margin:6px 0 8px 0">{bar}</div>'
            f'<div style="font-size:.9rem">{legend}</div>')


def _style_axes(ax):
    ax.set_facecolor("none")
    for spine in ax.spines.values():
        spine.set_color(COLOR_TIE)
        spine.set_alpha(0.3)
    ax.tick_params(colors=COLOR_TIE, labelsize=8)
    ax.grid(color=COLOR_TIE, alpha=0.3)


def distance_chart(dist, names):
    """One row per distance range. Bars grow left where A is stronger and right where B is stronger, from a centre
    line that means 'equal'; the receiver counts sit at the right. Long paths usually mean low take-off angles."""
    rows = dist[(dist["a"] > 0) | (dist["b"] > 0)].reset_index(drop=True)
    short = {k: (v if len(v) <= 10 else k) for k, v in names.items()}  # long names would collide with the bars
    n_rows = max(len(rows), 1)
    fig = Figure(figsize=(6.4, 0.62 * n_rows + 1.5))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot()
    _style_axes(ax)
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", labelsize=9, length=0)
    delta = rows["delta"].to_numpy(dtype=float) if len(rows) else np.array([])
    reach = float(np.nanmax(np.abs(delta))) if len(delta) and np.isfinite(delta).any() else 1.0
    reach = max(reach * 1.45, 3.0)
    y = np.arange(len(rows))
    for i, row in rows.iterrows():
        d, n = row["delta"], int(row["shared"])
        if pd.isna(d) or n == 0:
            ax.text(0, i, "no receiver heard both", ha="center", va="center", fontsize=8, color=COLOR_TIE, alpha=0.7)
            continue
        color = COLOR_A if d > 0 else COLOR_B if d < 0 else COLOR_TIE
        ax.barh(i, -d, height=0.56, color=color, alpha=0.9 if n >= 3 else 0.3, edgecolor="none")
        label = f"{abs(d):.1f} dB" if d else "equal"
        ax.annotate(label, (-d, i), xytext=(6 if -d >= 0 else -6, 0), textcoords="offset points",
                    ha="left" if -d >= 0 else "right", va="center", fontsize=8, color=COLOR_TIE)
    ax.axvline(0, color=COLOR_TIE, linewidth=1.2)
    ax.set_xlim(-reach, reach)
    ax.set_ylim(len(rows) - 0.5, -0.5)  # nearest range on top
    ax.set_yticks(y)
    ax.set_yticklabels(rows["label"] if len(rows) else [])
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{abs(v):g}"))
    ax.set_xlabel("dB stronger", fontsize=8, color=COLOR_TIE)
    # B is drawn to the right, so a positive A - B (A stronger) points left
    ax.text(0.0, 1.04, f"◀  {short['A']} stronger", transform=ax.transAxes, ha="left", va="bottom",
            fontsize=9, color=COLOR_A, fontweight="bold")
    ax.text(1.0, 1.04, f"{short['B']} stronger  ▶", transform=ax.transAxes, ha="right", va="bottom",
            fontsize=9, color=COLOR_B, fontweight="bold")
    for i, row in rows.iterrows():
        ax.text(1.03, i, f"heard by {short['A']} {int(row['a'])} · {short['B']} {int(row['b'])}\n{int(row['shared'])} heard both",
                transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=7.5, color=COLOR_TIE)
    fig.subplots_adjust(left=0.2, right=0.66, top=0.86, bottom=0.18)
    return fig


def relative_gain_chart(shared, names, units, by="direction"):
    """Relative gain (B's SNR minus A's SNR at the same receiver) of every receiver that heard both, against its direction
    or its distance from you. Above the zero line B was stronger, below it A was."""
    k = KM_PER_MILE if units == "mi" else 1
    gain = -shared["gap"]
    fig = Figure(figsize=(5.4, 3.3))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot()
    _style_axes(ax)
    if by == "direction":
        x = shared["bearing_a"]
        ax.set_xlim(0, 360)
        ax.set_xticks(range(0, 361, 45))
        ax.set_xticklabels(["N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"])
        ax.set_xlabel("Direction from your station", fontsize=9, color=COLOR_TIE)
    else:
        x = shared["km_a"] / k
        ax.set_xlim(0, float(x.max()) * 1.05 if len(x) else 1)
        ax.set_xlabel(f"Distance from your station ({units})", fontsize=9, color=COLOR_TIE)
    colors = np.where(gain > 0, COLOR_B, np.where(gain < 0, COLOR_A, COLOR_TIE))
    ax.scatter(x, gain, c=colors, s=24, alpha=0.8, linewidths=0, zorder=3)
    ax.axhline(0, color=COLOR_TIE, linestyle="--", linewidth=1, zorder=2)
    mean = float(gain.mean())
    ax.axhline(mean, color="#e5e7eb", linewidth=1.1, alpha=0.8, zorder=2)
    ax.text(0.99, 0.02 if mean >= 0 else 0.98, f"average {mean:+.1f} dB", transform=ax.transAxes, fontsize=8,
            color=COLOR_TIE, ha="right", va="bottom" if mean >= 0 else "top")
    span = float(max(abs(gain.max()), abs(gain.min()), 2)) * 1.15
    ax.set_ylim(-span, span)
    ax.set_ylabel("Relative gain, B − A (dB)", fontsize=9, color=COLOR_TIE)
    ax.text(0.01, 0.97, f"{names['B']} stronger", transform=ax.transAxes, color=COLOR_B, fontsize=8, va="top")
    ax.text(0.01, 0.03, f"{names['A']} stronger", transform=ax.transAxes, color=COLOR_A, fontsize=8, va="bottom")
    fig.tight_layout()
    return fig


def _linear_db(snr):
    """The average power of a set of dB reports, in dB: 10 x log10 of the mean of 10 ^ (dB / 10)."""
    return float(10 * np.log10(np.mean(10 ** (np.asarray(snr, dtype=float) / 10))))


def _gain_interval(snr_a, snr_b):
    """B minus A from the two sets of dB reports at one receiver, with a 95% range (Welch's unequal-variance interval on the
    two means, as in Dale's article). Returns (difference, low, high); the range is NaN with fewer than 3 reports per side."""
    a, b = np.asarray(snr_a, dtype=float), np.asarray(snr_b, dtype=float)
    diff = float(b.mean() - a.mean())
    if len(a) < 3 or len(b) < 3:
        return diff, float("nan"), float("nan")
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    se = float(np.sqrt(va + vb))
    if se == 0:
        return diff, diff, diff
    df = (va + vb) ** 2 / (va ** 2 / (len(a) - 1) + vb ** 2 / (len(b) - 1))
    t_value = t_crit(df)
    return diff, diff - t_value * se, diff + t_value * se


def receiver_time_chart(spots_a, spots_b, receiver, names, offset=0.0, linear=True):
    """Every report from one receiver against time, A in blue and B in orange, with a line at each antenna's average. In a test
    run in long blocks the blocks are shaded so you can see when each antenna was connected."""
    a = spots_a[spots_a["spotter"] == receiver].sort_values("time")
    b = spots_b[spots_b["spotter"] == receiver].sort_values("time")
    snr_a, snr_b = a["snr"].to_numpy(float), b["snr"].to_numpy(float) + offset
    fig = Figure(figsize=(8.0, 3.7))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot()
    _style_axes(ax)
    # the stretches of time each antenna was connected, shaded when they are long (blocks), not when they alternate every 2 minutes
    seq = sorted([(t, "A") for t in spots_a["time"].unique()] + [(t, "B") for t in spots_b["time"].unique()])
    runs = []
    for t, who in seq:
        if runs and runs[-1][0] == who:
            runs[-1][2] = t
        else:
            runs.append([who, t, t])
    if runs and len(seq) / len(runs) >= 3:
        for who, start, end in runs:
            ax.axvspan(pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(minutes=2),
                       color=COLOR_A if who == "A" else COLOR_B, alpha=0.07, linewidth=0, zorder=0)
    for t_, s_, color, label in ((a["time"], snr_a, COLOR_A, names["A"]), (b["time"], snr_b, COLOR_B, names["B"])):
        if not len(s_):
            continue
        avg = _linear_db(s_) if linear else float(np.median(s_))
        ax.scatter(t_, s_, color=color, s=30, alpha=0.8, linewidths=0, zorder=3, label=f"{label} ({len(s_)} reports)")
        ax.axhline(avg, color=color, linewidth=1.6, alpha=0.85, zorder=2)
    ax.set_ylabel("SNR (dB)", fontsize=9, color=COLOR_TIE)
    ax.set_xlabel("UTC", fontsize=9, color=COLOR_TIE)
    _time_axis(ax, pd.concat([a["time"], b["time"]]))
    ax.grid(color=COLOR_TIE, alpha=0.2)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.14), ncol=2, frameon=False, labelcolor=COLOR_TIE, fontsize=9)
    fig.tight_layout()
    return fig


def gain_over_time_chart(windows, names, receiver=None):
    """Relative gain (B minus A) in each time window: a bar for the average over all stations with its 95% range, and a line
    for one picked receiver. Shows whether the answer drifts, for example when the band opens during a long test."""
    fig = Figure(figsize=(8.0, 3.6))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot()
    _style_axes(ax)
    x = np.arange(len(windows))
    mean = windows["mean"].to_numpy(float)
    colors = [COLOR_B if v >= 0 else COLOR_A for v in np.nan_to_num(mean)]
    err = np.vstack([np.nan_to_num(mean - windows["lo"].to_numpy(float)), np.nan_to_num(windows["hi"].to_numpy(float) - mean)])
    ax.bar(x, np.nan_to_num(mean), width=0.6, color=colors, alpha=0.55, zorder=2,
           label=f"All stations (average, 95% range)")
    ax.errorbar(x, np.nan_to_num(mean), yerr=err, fmt="none", ecolor=COLOR_TIE, elinewidth=1.2, capsize=4, zorder=3)
    for xi, v, n in zip(x, mean, windows["n"]):
        if not np.isnan(v):
            ax.text(xi, 0.12 if v >= 0 else -0.12, f"{v:+.1f}", ha="center", va="bottom" if v >= 0 else "top", fontsize=8,
                    color=COLOR_TIE, zorder=4)  # at the foot of the bar, clear of the line and its labels
    if receiver is not None and windows["one"].notna().any():
        ax.plot(x, windows["one"].to_numpy(float), color="#16a34a", marker="o", markersize=6, linewidth=1.8, zorder=5,
                label=f"{receiver}")
        for xi, v in zip(x, windows["one"].to_numpy(float)):
            if not np.isnan(v):
                ax.annotate(f"{v:+.1f}", (xi, v), textcoords="offset points", xytext=(8, 6), fontsize=8, color="#16a34a", zorder=6)
    ax.axhline(0, color=COLOR_TIE, linestyle="--", linewidth=1, zorder=1)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{s:%H:%M}\u2013{e:%H:%M}" for s, e in zip(windows["start"], windows["end"])], fontsize=8)
    ax.set_ylabel("Relative gain, B \u2212 A (dB)", fontsize=9, color=COLOR_TIE)
    ax.set_xlabel("UTC time window", fontsize=9, color=COLOR_TIE)
    ax.grid(axis="y", color=COLOR_TIE, alpha=0.2, zorder=0)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.16), ncol=2, frameon=False, labelcolor=COLOR_TIE, fontsize=9)
    fig.tight_layout()
    return fig


def paired_scatter(shared, names):
    """One dot per receiver that heard both: A's SNR across, B's up. Above the dashed line B was stronger."""
    fig = Figure(figsize=(4.4, 4.4))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot()
    _style_axes(ax)
    lo = float(min(shared["snr_a"].min(), shared["snr_b"].min())) - 2
    hi = float(max(shared["snr_a"].max(), shared["snr_b"].max())) + 2
    ax.plot([lo, hi], [lo, hi], color=COLOR_TIE, linestyle="--", linewidth=1)
    colors = np.where(shared["gap"] > 0, COLOR_A, np.where(shared["gap"] < 0, COLOR_B, COLOR_TIE))
    ax.scatter(shared["snr_a"], shared["snr_b"], c=colors, s=26, alpha=0.75, linewidths=0)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_aspect("equal")
    ax.set_xlabel(f"{names['A']} SNR (dB)", color=COLOR_A, fontsize=9)
    ax.set_ylabel(f"{names['B']} SNR (dB)", color=COLOR_B, fontsize=9)
    ax.text(0.04, 0.95, f"{names['B']} stronger", transform=ax.transAxes, color=COLOR_B, fontsize=8, va="top")
    ax.text(0.96, 0.05, f"{names['A']} stronger", transform=ax.transAxes, color=COLOR_A, fontsize=8, ha="right")
    return fig


def _time_axis(ax, times):
    """Readable clock labels on a time axis. A one-day run shows just the clock time, with the date in the axis label; a run over
    several days puts a short date under the clock time where the day changes. (A full date on every label ran together.)"""
    stamps = pd.to_datetime(pd.Series(times))
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    if stamps.dt.date.nunique() == 1:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.set_xlabel(f"UTC time, {stamps.min():%d %b %Y}", color=COLOR_TIE, fontsize=8)
    else:
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, show_offset=False))
        ax.set_xlabel("UTC time", color=COLOR_TIE, fontsize=8)


def _gap_text(minutes):
    return f"{minutes / 60:.1f} h later" if minutes >= 120 else f"{minutes:.0f} min later"


def timeline_chart(spots_a, spots_b, names):
    """SNR of every spot over time, with each transmission's median as a dot: shows when each side was connected and
    whether conditions drifted while you tested. Transmissions are placed side by side in order, so a short test fills
    the chart and a long pause (between two rounds, say) shrinks to a marked gap instead of leaving the dots in two thin
    stripes. A very long run (over 80 transmissions) is drawn on the real clock instead."""
    fig = Figure(figsize=(9.5, 2.9))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot()
    _style_axes(ax)
    times = [pd.Timestamp(t) for t in np.sort(pd.concat([spots_a["time"], spots_b["time"]]).unique())]
    if len(times) > 80:
        for spots, color, name in ((spots_a, COLOR_A, names["A"]), (spots_b, COLOR_B, names["B"])):
            if spots.empty:
                continue
            ax.scatter(spots["time"], spots["snr"], s=5, color=color, alpha=0.18, linewidths=0)
            med = spots.groupby("time")["snr"].median()
            ax.plot(med.index, med.to_numpy(), linestyle="none", marker="o", markersize=4, color=color, label=name)
        _time_axis(ax, times)
        fig.subplots_adjust(bottom=0.2)
    else:
        steps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:]) if b > a]
        typical = float(np.median(steps)) if steps else 0.0
        pos, x, gaps = {times[0]: 0.0}, 0.0, []
        for before, t in zip(times, times[1:]):
            gap = (t - before).total_seconds() / 60
            if typical and gap > max(3 * typical, 6):  # a long pause: leave a small marked gap, not hours of empty axis
                gaps.append((x + 1.0, gap))
                x += 1.0
            x += 1.0
            pos[t] = x
        shared = set(spots_a["time"]) & set(spots_b["time"])  # both sides at the same moment: nudge them apart
        few = len(times) <= 12
        for spots, color, name, side in ((spots_a, COLOR_A, names["A"], -1), (spots_b, COLOR_B, names["B"], 1)):
            if spots.empty:
                continue
            shift = lambda t: pos[t] + (0.14 * side if t in shared else 0.0)  # noqa: E731
            ax.scatter(spots["time"].map(shift), spots["snr"], s=16 if few else 7, color=color, alpha=0.2, linewidths=0)
            med = spots.groupby("time")["snr"].median()
            ax.plot([shift(t) for t in med.index], med.to_numpy(), linestyle="none", marker="o",
                    markersize=8 if few else 5, color=color, label=name)
        for gx, gap in gaps:
            ax.axvline(gx - 0.0, color=COLOR_TIE, linestyle=":", alpha=0.6, linewidth=1)
            ax.text(gx, 1.0, _gap_text(gap), transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                    fontsize=7, color=COLOR_TIE)
        multi_day = times[0].date() != times[-1].date()
        every = max(1, int(np.ceil(len(times) / 16)))
        shown = times[::every]
        ax.set_xticks([pos[t] for t in shown])
        ax.set_xticklabels([f"{t:%d %b %H:%M}" if multi_day else f"{t:%H:%M}" for t in shown],
                           rotation=0 if len(shown) <= 10 else 45, ha="center" if len(shown) <= 10 else "right")
        ax.set_xlim(-0.8, max(pos.values()) + 0.8)
        ax.set_xlabel("UTC time of each transmission", color=COLOR_TIE, fontsize=8)
    ax.set_ylabel("SNR (dB)", color=COLOR_TIE, fontsize=8)
    ax.legend(frameon=False, fontsize=8, labelcolor=COLOR_TIE, ncol=2, loc="upper right")
    return fig


def breakdown_table(df, names, first_col, noun):
    """Styled distance / direction table: receivers per side, those that heard both, the SNR gap, and who is better."""
    better = df["call"].map({"A": names["A"], "B": names["B"]}).fillna("–")
    better = better + np.where(df["basis"] == "more receivers", f" (more {noun}s)", "")
    view = pd.DataFrame({
        first_col: df["label"],
        names["A"]: df["a"], names["B"]: df["b"], "Both": df["shared"],
        "Stronger by (dB)": df["delta"].map(lambda v: "–" if pd.isna(v) or abs(v) < 0.05
                                            else f"{'A' if v > 0 else 'B'} +{abs(v):.1f}"),
        "Better": better,
    })

    def tint(row):
        color = {"A": COLOR_A, "B": COLOR_B}.get(df.loc[row.name, "call"])
        style = [""] * len(row)
        if color:
            style[list(row.index).index("Better")] = f"background-color:{color}33;font-weight:600"
        return style
    return view.style.apply(tint, axis=1)


def _bold(text):
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def verdict_card_html(v):
    """The overall answer in a card tinted for the winner (blue = A, orange = B, grey = neither)."""
    color = {"A": COLOR_A, "B": COLOR_B}.get(v["winner"], COLOR_TIE)
    points = "".join(f'<li style="margin:7px 0">{icon} {_bold(text)}</li>' for icon, text in v["points"])
    caveats = "".join(f'<div style="margin-top:6px">⚠️ {_bold(c)}</div>' for c in v["caveats"])
    return (f'<div style="border-left:6px solid {color};background:{color}1c;border-radius:10px;padding:16px 22px;'
            f'margin:4px 0 16px 0"><div style="font-size:1.3rem;line-height:1.45">{_bold(v["headline"])}</div>'
            f'<ul style="list-style:none;padding:0;margin:12px 0 0 0;font-size:.95rem">{points}</ul>'
            f'<div style="font-size:.85rem;opacity:.8;margin-top:8px">{caveats}</div></div>')


def _pretty_date(file_date):
    """'20261004' -> '2026-10-04', '20261004-20261006' -> '2026-10-04 to 2026-10-06'."""
    parts = [f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else d for d in str(file_date).split("-")]
    return " to ".join(parts)


def _breakdown_rows(df, names):
    """Rows for the distance / direction tables in a report: label, A count, B count, both, who by how much, better."""
    rows = []
    for _, row in df.iterrows():
        lead = "–" if pd.isna(row["delta"]) or abs(row["delta"]) < 0.05 else f"{'A' if row['delta'] > 0 else 'B'} +{abs(row['delta']):.1f} dB"
        better = names.get(row["call"], "–") if row["call"] else "–"
        rows.append([row["label"], str(int(row["a"])), str(int(row["b"])), str(int(row["shared"])), lead, better])
    return rows


def compare_report_pdf(ctx):
    """A/B comparison as a PDF: the verdict, scoreboard, who heard you, direction and distance, receiver by receiver."""
    names, an, verdict, noun, units = ctx["names"], ctx["an"], ctx["verdict"], ctx["noun"], ctx["units"]
    colors = {"A": COLOR_A, "B": COLOR_B, None: COLOR_TIE}
    r, s_ = an["reach"], an["strength"]
    sub = [f"{ctx['callsign']} · {ctx['label']}", f"{ctx['source']} · {ctx['bands']} · {_pretty_date(ctx['file_date'])}", ctx["test"]]
    if names["A"] != "A" or names["B"] != "B":
        sub.append(f"A = {names['A']}    B = {names['B']}")
    blocks = [("heading", "Result"), ("verdict", verdict, colors),
              ("heading", "Scoreboard"), ("scoreboard", names, ctx["score_rows"], colors),
              ("heading", f"Who heard you on each {'side' if ctx['time_based'] else 'frequency'}"),
              ("bar", [r["only_a"], r["both"], r["only_b"]], [f"Only {names['A']}", "Heard both", f"Only {names['B']}"],
               [COLOR_A, "#6b7280", COLOR_B]),
              ("note", f"Each {noun} is counted once, however many times it spotted you."),
              ("paragraph", ctx["counts_note"]),
              ("heading", "Data used"),
              ("table", [""] + [names["A"], names["B"]], [[k] + v for k, v in ctx["data_used"]["rows"].items()], [0.5, 0.25, 0.25]),
              ("bullets", ctx["data_used"]["lines"])]
    cols = ["", "A", "B", "Both", "Stronger by", "Better"]
    widths = [0.26, 0.09, 0.09, 0.09, 0.2, 0.27]
    if ctx["means_a"].notna().any() or ctx["means_b"].notna().any():
        if len(ctx["az_points"]):
            blocks += [("heading", "Where each antenna reached"),
                       ("figure", azimuth_map_figure(ctx["home"][0], ctx["home"][1], ctx["az_points"], names, ctx["callsign"],
                                                      units, MAX_KM, "advantage", True, ctx["scale"]), 5.6),
                       ("note", "The station is at the centre; a dot's distance and angle are its true distance and bearing from "
                                "you. Cells show which antenna was stronger there (blue A, orange B) where at least 3 receivers heard both; "+"filled dots heard both antennas, open rings only one.")]
        if ctx["adv"]["n"].sum():
            blocks += [("heading", "Which side is stronger, by direction"),
                       ("figure", direction_advantage_chart(ctx["adv"], names, 45, ctx["callsign"]), 3.8),
                       ("note", "A longer bar means a bigger difference; a more solid bar rests on more receivers; a hatched "
                                "one has too few to trust.")]
        blocks += [("heading", "Average SNR by direction"), ("figure", direction_chart(ctx["means_a"], ctx["means_b"], names["A"], names["B"]), 3.5),
                   ("note", "Distance from the centre is the average SNR toward that direction."),
                   ("table", ["Direction"] + cols[1:], _breakdown_rows(an["direction"], names), widths)]
    if len(an["distance"]) and an["distance"][["a", "b"]].to_numpy().sum():
        blocks += [("heading", "Distance"), ("figure", distance_chart(an["distance"], names), 4.0),
                   ("table", ["Distance"] + cols[1:], _breakdown_rows(an["distance"], names), widths),
                   ("note", "Longer paths usually mean a lower take-off angle.")]
    if s_["n"]:
        every = ctx.get("every", an["shared_all"])  # the table on screen, if the reader changed how it is averaged
        k = KM_PER_MILE if units == "mi" else 1
        order = every.sort_values("km_a", ascending=False)
        rows = [[i, f"{row.km_a / k:,.0f}", f"{row.snr_a:g}", f"{row.snr_b:g}",
                 "A" if row.gap > 0 else "B" if row.gap < 0 else "Tie", f"{-row.gap:+g}"]
                for i, row in zip(order.index, order.itertuples())]
        blocks += [("heading", f"{noun.capitalize()} by {noun}"), ("figure", paired_scatter(ctx.get("scatter_src", an["shared"]), names), 3.8),
                   ("heading", "Relative gain by direction and distance"),
                   ("figure", relative_gain_chart(every, names, units, "direction"), 3.0),
                   ("figure", relative_gain_chart(every, names, units, "distance"), 3.0),
                   ("note", f"Relative gain is B's SNR minus A's SNR at the same {noun}: above zero the second antenna "
                            "gave more signal. Treat A as the reference and B as the test."),
                   *([("heading", "Average relative gain by direction and distance"),
                      ("table", list(ctx["gain_summary"]["direction"].columns), ctx["gain_summary"]["direction"].astype(str).values.tolist(),
                       [0.22, 0.14, 0.24, 0.22, 0.18]),
                      ("table", list(ctx["gain_summary"]["distance"].columns), ctx["gain_summary"]["distance"].astype(str).values.tolist(),
                       [0.3, 0.12, 0.22, 0.2, 0.16]),
                      ("note", f"The average of B minus A at the {noun}s in each group, {noun}s at the same place counted once, with "
                               f"a 95% range. A group with fewer than {BIN_MIN_SHARED} {noun}s is a hint, not a result.")]
                     if ctx.get("gain_summary") else []),
                   ("note", f"{len(every)} {noun}s heard both sides, shown farthest first (up to 40). "
                            + ctx.get("rx_note", "Each SNR is the median of that station's spots.")),
                   ("table", [noun.capitalize(), f"Distance ({units})", "A SNR", "B SNR", "Stronger", "B − A (dB)"],
                    rows[:40], [0.28, 0.2, 0.13, 0.13, 0.13, 0.13])]
    if ctx["method"]:
        blocks += [("heading", "Linear average"), ("paragraph", ctx["method"]),
                   ("note", "The default compares the same receiver in neighbouring transmissions, so it suits a short alternating test. "
                            "The power average is a second measure for longer, well-balanced runs and needs many reports "
                            "per receiver. If they disagree a lot, find out why before drawing a conclusion.")]
    if ctx["timeline"] is not None:
        blocks += [("heading", "Timeline"), ("figure", timeline_chart(*ctx["timeline"], names), 3.0)]
    meta = {"title": "Antenna comparison report", "callsign": ctx["callsign"], "subtitle": sub}
    return build_pdf(meta, blocks)


def _session_pdf_blocks(spots, locs, home, noun, units):
    """The session report as PDF blocks: the headline, when each band was heard, the band scorecard, where each band reached."""
    blocks = [("heading", "Session report"), ("bullets", [l.replace("**", "") for l in sr.headline(spots, locs, home, units, noun)]),
              ("heading", "When you were heard, band by band"), ("figure", sr.timeline_figure(spots, BAND_COLORS), 3.4),
              ("note", "Each bar is a stretch of time you were being heard; each tick is one spot. A spot only exists when someone "
                       "heard you, so a gap can mean you were off the air, calling to nobody, or the band was closed to you."),
              ("heading", "When each band worked"),
              ("figure", sr.hour_grid_figure(spots, locs, home, "spots", units), 3.6),
              ("note", "Number of spots per band per hour (UTC)."),
              ("figure", sr.hour_grid_figure(spots, locs, home, "farthest", units), 3.6),
              ("note", f"Farthest {noun} that heard you per band per hour, in {units} ('k' means thousands).")]
    card = sr.band_scorecard(spots, locs, home, units)
    shown = _scorecard_display(card, noun, units, multi_day=spots["time"].dt.date.nunique() > 1)
    far = f"Farthest ({units})"
    rows = [[band, f"{r['Spots']:,}", f"{r[f'{noun.capitalize()}s']:,}", r["Heard (UTC)"], str(r["Minutes with spots"]),
             f"{r['Median SNR']:g}", str(r["Best SNR"]), f"{r[far]:,.0f}" if pd.notna(r[far]) else "–", r["Farthest " + noun]]
            for band, r in shown.iterrows()]
    thin = [b for b in card.index if card.at[b, "Spots"] < sr.THIN]
    blocks += [("heading", "Band scorecard"),
               ("table", ["Band", "Spots", f"{noun.capitalize()}s", "Heard (UTC)", "Min", "Median", "Best", far,
                          "Farthest " + noun], rows, [0.07, 0.08, 0.1, 0.2, 0.07, 0.09, 0.07, 0.14, 0.18]),
               ("note", "Min is the minutes with spots: the 10-minute blocks with at least one spot, so time you were heard, not "
                        "time on the air. Median and Best are SNR in dB."
                        + (f" {', '.join(thin)} had fewer than {sr.THIN} spots, so treat its numbers as a hint." if thin else ""))]
    regions = sr.region_table(spots, locs, home)
    cols = list(regions.columns)
    width = [0.09] + [0.91 / len(cols)] * len(cols)
    blocks += [("heading", "Where each band reached"),
               ("table", ["Band"] + [sr.REGION_SHORT.get(c, c) for c in cols],
                [[band] + [str(int(v)) if v else "" for v in row] for band, row in regions.iterrows()], width),
               ("note", f"Different {noun}s that heard you on each band, by region. The first row counts each {noun} once across "
                        "all bands.")]
    return blocks


def single_report_pdf(spots, locs, home, label, callsign, file_date, kind, noun, units, scale, all_bands=None):
    """One antenna / one set of spots as a PDF: key numbers, the session report, direction, the stations heard.
    `all_bands` is the spots with every band, for the session report; `spots` may be narrowed to one band."""
    all_bands = spots if all_bands is None else all_bands
    k = KM_PER_MILE if units == "mi" else 1
    stats = compute_stats(spots, locs, home)
    bands = ", ".join(sorted(spots["band"].unique(), key=lambda b: list(BAND_COLORS).index(b) if b in BAND_COLORS else 99))
    sub = [f"{callsign} · {label}",
           f"{'WSPR' if kind == 'WSPR' else 'Reverse Beacon Network'} · {bands} · {_pretty_date(file_date)}"]
    blocks = [("heading", "Summary"),
              ("metrics", [("Spots", f"{stats['spots']:,}"), (f"{noun.capitalize()}s", f"{stats['skimmers']:,}"),
                           (f"Farthest ({units})", f"{stats['max_km'] / k:,.0f}"), ("Best SNR", f"{stats['max_snr']:.0f} dB"),
                           ("Average SNR", f"{stats['avg_snr']:.1f} dB")]),
              ("paragraph", f"Farthest {noun}: {stats['farthest']}." if stats["farthest"] else "")]
    blocks += _session_pdf_blocks(all_bands, locs, home, noun, units)
    result = bearing_chart(spots, locs, home, scale)
    if result:
        fig, summary = result
        blocks += [("heading", "Direction of your spots"), ("figure", fig, 4.4), ("paragraph", summary)]
    pts = _single_points(spots, locs, home)
    if len(pts):
        one_band = spots["band"].nunique() == 1
        blocks += [("heading", "World view from your station"),
                   ("figure", azimuth_map_figure(home[0], home[1], pts, {"A": "", "B": ""}, callsign, units, MAX_KM,
                                                  "snr" if one_band else "dots", not one_band, scale), 5.6),
                   ("note", "You are at the centre; distance and angle from the centre are true distance and compass bearing. "
                            + ("Cells show the average SNR by direction and distance." if one_band else
                               "Dots are coloured by band."))]
    table = receiver_table(spots, locs, home)
    if len(table):
        best = spots.groupby("spotter")["snr"].max()
        on = spots.groupby("spotter")["band"].agg(lambda b: ", ".join(sorted(set(b))))
        order = table.sort_values("km", ascending=False)
        rows = [[i, f"{row.km / k:,.0f}", on.get(i, ""), str(int(row.spots)), f"{best[i]:g}", f"{row.snr:g}"]
                for i, row in zip(order.index, order.itertuples())]
        blocks += [("heading", f"{noun.capitalize()}s that heard you, farthest first"),
                   ("note", f"Showing up to 60 of {len(rows)}."),
                   ("table", [noun.capitalize(), f"Distance ({units})", "Bands", "Spots", "Best SNR", "Median SNR"],
                    rows[:60], [0.24, 0.18, 0.2, 0.12, 0.13, 0.13])]
    meta = {"title": "Signal report", "callsign": callsign, "subtitle": sub}
    return build_pdf(meta, blocks)


def _name_inputs():
    """Optional names for the two sides, used everywhere in the results."""
    with st.expander("Name your antennas (optional)"):
        c1, c2 = st.columns(2)
        return (c1.text_input("\U0001F535 Name for A", placeholder="e.g. Dipole", key="cmp_name_a").strip(),
                c2.text_input("\U0001F7E0 Name for B", placeholder="e.g. 40m loop", key="cmp_name_b").strip())


def _separate_by_frequency(spots, gap_khz):
    """{'a', 'b', 'default'} chosen from frequency groups, or None if that can't be done."""
    groups = frequency_groups(spots, gap_khz)
    for tighter in (0.2, 0.1):  # two tests very close together look like one group: look a little closer before giving up
        if len(groups) >= 2:
            break
        groups = frequency_groups(spots, tighter)
    freqs = ", ".join(f"{f:.1f}" for f, _ in groups)
    if len(groups) < 2:
        st.warning(f"Found only {len(groups)} frequency group ({freqs or 'none'} kHz). Compare mode needs spots on at "
                   f"least two frequencies. Call CQ on a different frequency with each antenna, at least 0.1 kHz apart.")
        return None

    def option_label(i):
        f, g = groups[i]
        return (f"{f:.1f} kHz · {g['spotter'].nunique()} skimmers ({len(g)} spots) · "
                f"{g['time'].min():%H:%M}–{g['time'].max():%H:%M} UTC")

    top_two = sorted(sorted(range(len(groups)), key=lambda i: -len(groups[i][1]))[:2])
    pick_a, pick_b = st.columns(2)
    ia = pick_a.selectbox("\U0001F535 Frequency A", range(len(groups)), index=top_two[0], format_func=option_label)
    ib = pick_b.selectbox("\U0001F7E0 Frequency B", range(len(groups)), index=top_two[1], format_func=option_label)
    if ia == ib:
        st.warning("Pick two different frequencies to compare.")
        return None
    (fa, spots_a), (fb, spots_b) = groups[ia], groups[ib]
    return {"a": spots_a, "b": spots_b, "default": {"A": f"A ({fa:.1f} kHz)", "B": f"B ({fb:.1f} kHz)"}}


def _without_ghost_cycles(spots):
    """Throw out cycles that only one or two receivers reported when ordinary cycles are heard by many more. They are
    nearly always one receiver with a wrong clock reporting your transmission under the next time slot, so they are
    not real transmissions. On a weak band, where every cycle has few reports, nothing is thrown out."""
    st.session_state["ghost_cycles"] = 0
    heard = spots.groupby("time")["spotter"].nunique()
    typical = heard[heard > GHOST_MAX].median()
    if pd.isna(typical) or typical < GHOST_MIN_TYPICAL:
        return spots
    ghosts = heard[heard <= GHOST_MAX].index
    if len(ghosts) == 0:
        return spots
    st.session_state["ghost_cycles"] = len(ghosts)
    st.caption(f"Left out {len(ghosts)} cycle{'s' if len(ghosts) > 1 else ''} that {GHOST_MAX} or fewer receivers heard, "
               f"when typical cycles were heard by about {typical:.0f}. A cycle that thin says little about an antenna, and "
               "it is often one receiver with a wrong clock reporting your signal under the wrong time slot.")
    return spots[~spots["time"].isin(ghosts)]


def _separate_by_log(spots):
    """WSPR antenna test: each transmission is logged with the antenna that was connected. Either the antenna changed
    after every transmission (the user says how many there were and where the test started, and the app lists them with a
    first guess the user can correct), or it stayed on one antenna for a long block and then the other, over hours."""
    spots = _without_ghost_cycles(spots)
    all_stamps = [pd.Timestamp(t) for t in np.sort(spots["time"].unique())]
    if len(all_stamps) < 2:
        st.warning("Only one transmission was found. An antenna test needs at least two, one on each antenna. Spots "
                   "take a few minutes to show up after you transmit, so try again shortly, or pick another day.")
        return None
    if spots["band"].nunique() > 1:
        st.warning(f"These spots cover {spots['band'].nunique()} bands. Test one band at a time: pick it under "
                   "**Filters \u2192 Band** in the sidebar.")
    st.markdown("**Your test**")
    mode = st.radio("How did you switch antennas?", ["After every transmission", "In long blocks"], horizontal=True,
                    key="test_mode",
                    help="After every transmission: A, then B, then A \u2026 (or A, B, B, A), the quick test. "
                         "In long blocks: one antenna for 30 minutes or an hour, then the other, repeated over several hours, "
                         "which suits a long, time-averaged test.")
    if mode == "In long blocks":
        return _blocks_test(spots, all_stamps)
    top = st.columns([4, 2, 3])
    n_cycles = int(top[0].number_input(
        "Transmissions in your test", min_value=2, max_value=max(2, min(len(all_stamps), 80)), value=2, step=2,
        key="test_cycles",
        help="Count every 2-minute transmission, on either antenna: A then B is 2, A B B A is 4, A B B A twice is 8. "
             "More is steadier: 8 or more (4 pairs) is a solid test, 12 to 16 for differences under 1 dB. "
             "Use whole rounds (a multiple of 2, or of 4 for A B B A): an odd count leaves one transmission without a partner."))
    n_cycles = min(n_cycles, len(all_stamps))
    if len(all_stamps) >= 20 and n_cycles < len(all_stamps) // 2:
        st.info(f"The data holds **{len(all_stamps)} transmissions**, but this quick test uses only the latest **{n_cycles}**. "
                "If you ran a long test with one antenna for 30 minutes or an hour at a time, choose **In long blocks** above "
                "to use all of it.")
    multi_day = all_stamps[0].date() != all_stamps[-1].date()
    fmt = (lambda t: f"{t:%d %b %H:%M}") if multi_day else (lambda t: f"{t:%H:%M}")
    LATEST = "Latest"
    start_choice = top[1].selectbox(
        "Starting at (UTC)", [LATEST] + [fmt(t) for t in reversed(all_stamps[:-1])], key="test_start",
        help="Leave on Latest for a test you have just run. To use an earlier test, pick the time of its first "
             "transmission.")
    patterns = {"A, B, A, B \u2026": "ABAB", "A, B, B, A \u2026": "ABBA"}  # every test starts on A
    order = top[2].radio("Order", list(patterns), key="test_order",
                         help="The order the antennas were connected, starting with A. A, B, B, A balances which "
                              "antenna goes first in each pair. If a test really started on B, change the Antenna "
                              "cells below.")
    pattern = patterns[order]
    if start_choice == LATEST:
        first = len(all_stamps) - n_cycles
    else:
        first = next(i for i, t in enumerate(all_stamps) if fmt(t) == start_choice)
    stamps = all_stamps[first:first + n_cycles]
    if len(stamps) < n_cycles:
        st.caption(f"Only {len(stamps)} transmissions are available from that start.")
    if len(stamps) < 2:
        st.warning("Pick an earlier start, or fewer transmissions.")
        return None
    if len(stamps) % 2:
        st.caption("⚠ An odd number of transmissions leaves one without a partner on the other antenna. It is left out "
                   "of the A/B comparison (it still shows on the maps), so an even count is better.")
    pairs = max(len(stamps) // 2, 1)
    smallest = 2.8 * SLOT_NOISE_DB / np.sqrt(pairs)  # 80 % chance of seeing a difference this big, from slot-to-slot fading
    st.caption(f"{len(stamps)} transmissions is about {pairs} pair{'s' if pairs != 1 else ''} of A and B: enough to "
               + (f"reliably show a difference of roughly {smallest:.1f} dB or more. More pairs show smaller differences."
                  if smallest > EQUAL_DB else
                  f"show differences down to about {EQUAL_DB:g} dB. Anything smaller counts as equal, so more pairs "
                  "won't change the answer much."))
    st.caption("Which antenna was connected for each transmission? The antenna changes after every transmission, down "
               "the list. Fix any row to match your notes, or clear a row to leave it out.")
    if any(g > 2.5 for g in [(y - x).total_seconds() / 60 for x, y in zip(stamps, stamps[1:])]):
        st.caption("\u26a0 Some cycles are missing between these rows (the transmitter skipped them, or nobody heard "
                   "them). The labels assume you changed antenna after each transmission you made; if you changed on "
                   "a timer instead, correct the rows after each gap.")
    heard = spots.groupby("time")["spotter"].nunique()
    right_now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    arriving = [(right_now - t).total_seconds() / 60 < DATA_LAG_MINUTES for t in stamps]
    gaps = [0.0] + [(b - a).total_seconds() / 60 for a, b in zip(stamps, stamps[1:])]
    table = pd.DataFrame({"UTC time": [fmt(t) for t in stamps],
                          "Receivers that heard it": [int(heard[t]) for t in stamps],
                          "Antenna": default_labels(stamps, pattern),
                          "Status": [("\u23f3 may be incomplete" if late else
                                      (f"\u26a0 {gap:.0f} min after the row above" if gap > 2.5 else ""))
                                     for late, gap in zip(arriving, gaps)]})
    edited = st.data_editor(
        table, key=f"antenna_log_{pattern}_{len(stamps)}_{stamps[0]:%d%H%M}", hide_index=True, width="stretch",
        disabled=["UTC time", "Receivers that heard it", "Status"], height=min(35 * (len(table) + 1) + 3, 420),
        column_config={"Antenna": st.column_config.SelectboxColumn("Antenna", options=["A", "B"],
                                                                   help="A, B, or empty to leave this one out.")})
    marked = {t: lab for t, lab in zip(stamps, edited["Antenna"]) if lab in ("A", "B")}
    late = [t for t, wait in zip(stamps, arriving) if wait and t in marked]
    labels = marked
    if late:  # still counted; this is only a heads-up that the newest reports may not all be in yet
        ready_at = max(late) + pd.Timedelta(minutes=DATA_LAG_MINUTES)
        plural = len(late) > 1
        st.info(f"\u23f3 The newest transmission{'s' if plural else ''} (from {late[0]:%H:%M} UTC) "
                f"{'are' if plural else 'is'} less than {DATA_LAG_MINUTES} minutes old, so not all of the spots may be in "
                f"yet and the result could still shift. Click **Load spots** again after {ready_at:%H:%M} UTC to refresh it.")
    if "A" not in labels.values() or "B" not in labels.values():
        st.warning("Mark at least one transmission as A and one as B.")
        return None
    in_test = spots[spots["time"].isin(list(labels))]
    spots_a, spots_b = split_by_labels(in_test, labels)
    return {"a": spots_a, "b": spots_b, "time_based": True, "alternating": True, "labeled": True,
            "default": {"A": "A", "B": "B"}}


def _clock(text):
    """(hour, minute) from a typed time such as 12:30, 1230, 12.30, 12 30 or 930, or None."""
    m = re.fullmatch(r"(\d{1,2})[:.\s]?(\d{2})", re.sub(r"\s*(utc|z)$", "", (text or "").strip(), flags=re.IGNORECASE))
    if m and int(m[1]) < 24 and int(m[2]) < 60:
        return int(m[1]), int(m[2])
    return None


def _cell_time(text, day0):
    """A block's From or To cell as a Timestamp: a clock time, with +1 (or +1d) after it for the next UTC day. `day0` is the date
    of the first block. Returns None when the cell isn't a time."""
    m = re.fullmatch(r"\s*(.*?)\s*(?:\+\s*(\d+)\s*d?)?\s*", str(text or ""))
    clock = _clock(m[1]) if m else None
    if clock is None:
        return None
    return pd.Timestamp(day0) + pd.Timedelta(days=int(m[2] or 0), hours=clock[0], minutes=clock[1])


def _parse_schedule(text, day0, pattern):
    """Blocks from pasted lines such as '16:40-17:12 A', one per line (the antenna is optional and then follows the A, B pattern;
    a block that starts earlier in the day than the one before it is taken to be on the next UTC day).
    Returns ([(antenna, lo, hi)], [problem lines])."""
    blocks, problems, day, last_lo = [], [], pd.Timestamp(day0).normalize(), None
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"\s*(\d{1,2}[:.]?\d{2})\s*(?:-|\u2013|to)\s*(\d{1,2}[:.]?\d{2})\s*([AaBb])?\s*", line)
        lo_c, hi_c = (_clock(m[1]), _clock(m[2])) if m else (None, None)
        if not m or lo_c is None or hi_c is None:
            problems.append(line.strip())
            continue
        lo = day + pd.Timedelta(hours=lo_c[0], minutes=lo_c[1])
        if last_lo is not None and lo < last_lo:
            day += pd.Timedelta(days=1)
            lo += pd.Timedelta(days=1)
        hi = day + pd.Timedelta(hours=hi_c[0], minutes=hi_c[1])
        if hi <= lo:
            hi += pd.Timedelta(days=1)
        antenna = m[3].upper() if m[3] else pattern[len(blocks) % len(pattern)]
        blocks.append((antenna, lo, hi))
        last_lo = lo
    return blocks, problems


def _block_start(label, all_stamps, fmt):
    """The start of a long test, from a transmission picked in the list or a time typed in. Times can be typed as 12:30, 1230,
    12.30, 12 30, 930 or 1230z, and a date can come first (08 Oct 12:30). Returns a Timestamp, or None if the text isn't a
    time. A time alone takes the date of the first transmission."""
    label = (label or "").strip()
    for t in all_stamps:
        if fmt(t) == label:
            return t
    first = all_stamps[0]
    text = re.sub(r"\s*(utc|z)$", "", label, flags=re.IGNORECASE).strip()
    clock = re.fullmatch(r"(\d{1,2})[:.\s]?(\d{2})", text)
    if clock:
        hour, minute = int(clock[1]), int(clock[2])
        if hour < 24 and minute < 60:
            return pd.Timestamp(first.year, first.month, first.day, hour, minute)
        return None
    for pattern in ("%d %b %H:%M", "%d %b %H%M"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return pd.Timestamp(first.year, parsed.month, parsed.day, parsed.hour, parsed.minute)
    return None


def _blocks_test(spots, all_stamps):
    """A long test: one antenna for a block of time, then the other, repeated. Every transmission inside a block counts
    for that block's antenna. Nothing is paired: each receiver's reports on A and on B are compared as a whole."""
    multi_day = all_stamps[0].date() != all_stamps[-1].date()
    fmt = (lambda t: f"{t:%d %b %H:%M}") if multi_day else (lambda t: f"{t:%H:%M}")
    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    block_min = int(c1.number_input("Minutes per block", 4, 480, 30, step=2, key="blk_len",
                                    help="How long each antenna stayed connected before you switched."))
    # Open on the whole data set: the first transmission, and enough blocks to reach the last one. The keys carry the data's
    # first and last times, so loading different spots starts fresh instead of keeping the last test's choices.
    sig = f"{all_stamps[0]:%d%H%M}_{all_stamps[-1]:%d%H%M}_{block_min}"
    span_min = (all_stamps[-1] - all_stamps[0]).total_seconds() / 60 + 2
    fit = int(np.ceil(span_min / block_min))
    default_blocks = int(min(40, max(2, fit + fit % 2)))
    n_blocks = int(c2.number_input("Number of blocks", 2, 40, default_blocks, step=2, key=f"blk_n_{sig}",
                                   help="Count every block, A and B together: A for 30 minutes then B for 30 is 2."))
    patterns = {"A, B, A, B \u2026": "ABAB", "A, B, B, A \u2026": "ABBA"}
    order = c4.radio("Block order", list(patterns), key="blk_order", horizontal=True,
                     help="Starting with A. A, B, B, A balances which antenna goes first.")
    start_label = c3.selectbox(
        "First block starts at (UTC)", [fmt(t) for t in all_stamps], index=0, key=f"blk_start_{sig}", accept_new_options=True,
        help="The time you first connected antenna A. Pick a transmission from the list, or type any time such as 12:30 or 1230, "
             "even if nothing was heard at that minute" + (" (a time alone means the first day; add the date as 08 Oct 12:30 "
                                                           "for another day)." if multi_day else "."))
    t0 = _block_start(start_label, all_stamps, fmt)
    if t0 is None:
        c3.caption(f"⚠ Couldn't read '{start_label}'. Type a time like 12:30; using {fmt(all_stamps[0])} for now.")
        t0 = all_stamps[0]
    pattern = patterns[order]
    day0 = t0.normalize()
    with st.expander("Paste a schedule instead (for uneven blocks)"):
        pasted = st.text_area("One block per line, like 16:40-17:12 A", key=f"blk_paste_{sig}", height=110,
                              help="Use this when the blocks are not all the same length, or when a switch came a cycle early or "
                                   "late. The antenna letter is optional: leave it off and the blocks follow your A, B order. "
                                   "A line that starts earlier than the one above it is taken to be after midnight UTC.")
    schedule, bad_lines = _parse_schedule(pasted, day0, pattern)
    if bad_lines:
        st.warning("Couldn't read: " + "; ".join(bad_lines[:3]) + (" and more" if len(bad_lines) > 3 else "")
                   + ". Use lines like 16:40-17:12 A.")
    pasted_used = bool(schedule)
    if not schedule:
        schedule = [(pattern[k % len(pattern)], t0 + pd.Timedelta(minutes=k * block_min),
                     t0 + pd.Timedelta(minutes=(k + 1) * block_min)) for k in range(n_blocks)]
    chain = False if pasted_used else st.checkbox(
        "Each block ends when the next one starts", value=True, key=f"blk_chain_{sig}",
        help="Then you only have to edit the From times: the To of a block follows the From of the next one, and the last block "
             "runs for the block length. Untick it to set every end time yourself, for blocks with a gap between them.")
    stamp = lambda ts: f"{ts:%H:%M}" + (f" +{(ts.normalize() - day0).days}d" if ts.normalize() > day0 else "")
    columns = {"Block": range(1, len(schedule) + 1), "Antenna": [a for a, _, _ in schedule],
               "From (UTC)": [stamp(lo) for _, lo, _ in schedule]}
    if not chain:
        columns["To (UTC)"] = [stamp(hi) for _, _, hi in schedule]
    table = pd.DataFrame(columns)
    if chain:
        st.caption("Each block is one antenna until the next block starts, so you only need to edit the From times (the last block "
                   "runs for the block length). Change an Antenna cell if you switched differently, or clear it to leave a block "
                   "out. A time after midnight UTC is shown with +1d.")
    else:
        st.caption("Each block is one antenna for its whole time. Change an Antenna cell if you switched differently, or clear it to "
                   "leave a block out. The From and To times can be edited too, for blocks that ran a little long or short; add +1 "
                   "after a time that falls on the next UTC day.")
    config = {"Antenna": st.column_config.SelectboxColumn("Antenna", options=["A", "B"],
                                                          help="A, B, or empty to leave this block out."),
              "From (UTC)": st.column_config.TextColumn("From (UTC)", help="Like 12:30, or 00:10 +1 for the next day.")}
    if not chain:
        config["To (UTC)"] = st.column_config.TextColumn("To (UTC)", help="Like 13:00, or 00:40 +1 for the next day.")
    edited = st.data_editor(
        table, hide_index=True, width="stretch",
        key=f"blocks_{pattern}_{block_min}_{n_blocks}_{t0:%d%H%M}_{abs(hash(pasted)) % 10**8}_{int(chain)}", disabled=["Block"],
        column_config=config)
    blocks, problems = [], []
    rows_list = list(edited.iterrows())
    los = [_cell_time(r["From (UTC)"], day0) for _, r in rows_list]
    if chain:
        his = []
        for i, lo in enumerate(los):
            later = [x for x in los[i + 1:] if x is not None and lo is not None and x > lo]
            his.append(later[0] if later else (lo + pd.Timedelta(minutes=block_min) if lo is not None else None))
    else:
        his = [_cell_time(r["To (UTC)"], day0) for _, r in rows_list]
    for (n, row), lo, hi in zip(rows_list, los, his):
        if row["Antenna"] not in ("A", "B"):
            continue
        if lo is None or hi is None:
            problems.append(f"block {int(row['Block'])}")
            continue
        if hi <= lo:  # a To that is not later than its From is taken to be after midnight
            hi += pd.Timedelta(days=1)
        blocks.append((int(row["Block"]), row["Antenna"], lo, hi))
    if problems:
        st.warning("Couldn't read the times for " + ", ".join(problems[:5]) + ". Use a time like 12:30; those blocks are left out.")
    overlaps = [(x[0], y[0]) for i, x in enumerate(blocks) for y in blocks[i + 1:] if x[2] < y[3] and y[2] < x[3]]
    if overlaps:
        st.warning(f"Blocks {overlaps[0][0]} and {overlaps[0][1]} overlap, so a transmission in the overlap counts for the first one.")
    labels = {}
    for t in all_stamps:
        for _, antenna, lo, hi in blocks:
            if lo <= t < hi:
                labels[t] = antenna
                break
    with st.expander("The blocks as the test will use them (From, To and what each caught)", expanded=True):
        caught = pd.DataFrame([{"Block": n, "Antenna": a, "From (UTC)": stamp(lo), "To (UTC)": stamp(hi),
                                "Transmissions": sum(lo <= t < hi for t in all_stamps),
                                "Receivers": int(spots[(spots["time"] >= lo) & (spots["time"] < hi)]["spotter"].nunique())}
                               for n, a, lo, hi in blocks])
        st.dataframe(caught, hide_index=True, width="stretch")
    if "A" not in labels.values() or "B" not in labels.values():
        st.warning("There are no transmissions in at least one antenna's blocks. Check the start time, block length and "
                   "number of blocks against your notes.")
        return None
    sched_start, sched_end = min(b[2] for b in blocks), max(b[3] for b in blocks)
    if sched_end > all_stamps[-1] + pd.Timedelta(minutes=2):
        st.caption("\u26a0 The last block runs past the newest transmission in the data, so it may be cut short.")
        if sched_end.normalize() > all_stamps[-1].normalize() or all_stamps[-1].hour >= 23:
            st.info("This test runs past midnight UTC but the spots loaded stop there. In the sidebar choose **Date range** and "
                    "set **To** to the next day, then click **Load spots** again.")
    in_test = spots[spots["time"].isin(list(labels))]
    in_test = in_test.assign(antenna=in_test["time"].map(labels))
    spots_a, spots_b = in_test[in_test["antenna"] == "A"], in_test[in_test["antenna"] == "B"]
    per_a, per_b = spots_a.groupby("spotter").size(), spots_b.groupby("spotter").size()
    both = per_a.index.intersection(per_b.index)
    if len(both):
        st.caption(f"{spots_a['time'].nunique()} transmissions on A and {spots_b['time'].nunique()} on B. At the "
                   f"{len(both)} receivers that heard both, the middle receiver has {int(per_a[both].median())} reports on A and "
                   f"{int(per_b[both].median())} on B. A long test aims for {DALE_MIN_SPOTS} or more on each; the published method keeps only receivers with {DALE_SITE_MIN}.")
    lengths = [(hi - lo).total_seconds() / 60 for _, _, lo, hi in blocks]
    return {"a": spots_a, "b": spots_b, "time_based": True, "alternating": False, "labeled": False, "blocks": True,
            "t0": sched_start, "t_end": sched_end, "block_min": int(round(float(np.median(lengths)))), "n_blocks_set": len(blocks),
            "n_blocks": len(blocks), "default": {"A": "A", "B": "B"}}

def _azimuth_points(ta_all, tb_all, an):
    """One row per receiver for the world map: where it is (km, bearing), whether only A, only B or both heard it, how much
    stronger A was than B (for both, from the same numbers the scatter chart uses), and each antenna's SNR there."""
    both = ta_all.index.intersection(tb_all.index)
    gap = an["shared_all"]["gap"] if "shared_all" in an else pd.Series(dtype=float)
    rows = [(ta_all.at[r, "km"], ta_all.at[r, "bearing"], "only_a", np.nan, ta_all.at[r, "snr"], np.nan)
            for r in ta_all.index.difference(both)]
    rows += [(tb_all.at[r, "km"], tb_all.at[r, "bearing"], "only_b", np.nan, np.nan, tb_all.at[r, "snr"])
             for r in tb_all.index.difference(both)]
    rows += [(ta_all.at[r, "km"], ta_all.at[r, "bearing"], "both", float(gap.get(r, ta_all.at[r, "snr"] - tb_all.at[r, "snr"])),
              ta_all.at[r, "snr"], tb_all.at[r, "snr"]) for r in both]
    return pd.DataFrame(rows, columns=["km", "bearing", "group", "gap", "snr_a", "snr_b"])


def _single_points(spots, locs, home):
    """Points for the one-antenna world map: one per receiver and band, coloured by band when several are loaded."""
    frames = []
    for band, g in spots.groupby("band"):
        t = receiver_table(g, locs, home)
        if len(t):
            frames.append(pd.DataFrame({"km": t["km"].to_numpy(), "bearing": t["bearing"].to_numpy(), "group": "single",
                                        "gap": np.nan, "snr_a": t["snr"].to_numpy(), "snr_b": np.nan,
                                        "color": BAND_COLORS.get(band, "#8a8f98")}))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["km", "bearing", "group", "gap", "snr_a", "snr_b", "color"])


def _views(units):
    km = 1.609344 if units == "mi" else 1.0
    return {f"Nearby ({3000 if units == 'mi' else 5000:,} {units})": (3000 * km if units == "mi" else 5000.0),
            f"Half the world ({7500 if units == 'mi' else 12000:,} {units})": (7500 * km if units == "mi" else 12000.0),
            "Whole world": MAX_KM}


def _azimuth_map_section(points, an, names, noun, units, home, callsign, scale):
    """The world map for a comparison, with a choice of what the cells show and how much of the world to draw."""
    k = KM_PER_MILE if units == "mi" else 1
    views = _views(units)
    layers = {"Which antenna is stronger": "advantage", f"SNR with {names['A']}": "snr_a", f"SNR with {names['B']}": "snr_b",
              "Receivers only": "dots"}
    c1, c2 = st.columns([3, 2])
    layer_name = c1.radio("Colour the map by", list(layers), horizontal=True, key="azmap_layer")
    layer = layers[layer_name]
    show_dots = c2.checkbox("Show the receivers", value=False, key="azmap_dots") or layer == "dots"
    choice = st.radio("How much of the world", list(views), index=1, horizontal=True, key="azmap_view")
    radius = views[choice]
    map_col, info_col = st.columns([3, 2])
    map_col.pyplot(azimuth_map_figure(home[0], home[1], points, names, callsign, units, radius, layer, show_dots, scale),
                   width="stretch")
    outside = int((points["km"] > radius).sum())
    counts = points["group"].value_counts()
    info_col.markdown(
        f"**{int(counts.get('both', 0))}** {noun}s heard both \u00b7 **{int(counts.get('only_a', 0))}** only {names['A']} \u00b7 "
        f"**{int(counts.get('only_b', 0))}** only {names['B']}" + (f" \u00b7 {outside} beyond this view" if outside else ""))
    for side in ("a", "b"):
        far = an["dx"][side]
        if far:
            info_col.markdown(f"Farthest {noun} for {names[side.upper()]}: **{far['km'] / k:,.0f} {units}** ({far['who']})")
    info_col.caption(
        "The station is at the centre. Distance from the centre is true distance from you and the angle is true compass "
        "bearing, so a straight line from the centre is the path the signal takes. Each coloured cell is one direction "
        f"(30\u00b0) and distance band, shown only where at least {MIN_CELL} {noun}s were heard. "
        + ("Blue cells are where A was stronger and orange where B was; a brighter colour is a bigger difference and a more solid "
           "cell rests on more receivers." if layer == "advantage" else
           "The colour is the average SNR in the cell." if layer in ("snr_a", "snr_b") else
           "Filled dots heard both antennas, open rings only one.")
        + " Blank means nobody there heard it, which also happens wherever there are no receivers, such as over the oceans.")


def _azimuth_single_section(spots, locs, home, callsign, noun, units, scale):
    """The world map for one antenna: average signal strength, or how many stations heard you, by direction and distance."""
    pts = _single_points(spots, locs, home)
    if pts.empty:
        st.caption(f"No located {noun}s to map.")
        return
    bands = spots["band"].nunique()
    views = _views(units)
    c1, c2 = st.columns([3, 2])
    if bands > 1:
        c1.caption(f"{bands} bands are loaded, so the dots are coloured by band. Choose a single band in the Band box to see average "
                   "signal strength and reach by direction and distance.")
        layer = "dots"
    else:
        layer = {"Average signal strength": "snr", f"{noun.capitalize()}s heard": "reach"}[
            c1.radio("Colour the map by", ["Average signal strength", f"{noun.capitalize()}s heard"], horizontal=True,
                     key="azmap_single_layer")]
    show_dots = c2.checkbox("Show the receivers", value=bands > 1, key="azmap_single_dots") or layer == "dots"
    choice = st.radio("How much of the world", list(views), index=1, horizontal=True, key="azmap_single_view")
    radius = views[choice]
    map_col, info_col = st.columns([3, 2])
    map_col.pyplot(azimuth_map_figure(home[0], home[1], pts, {"A": "", "B": ""}, callsign, units, radius, layer, show_dots, scale),
                   width="stretch")
    k = KM_PER_MILE if units == "mi" else 1
    far = pts.loc[pts["km"].idxmax()]
    info_col.markdown(f"**{len(pts)}** {noun}s heard you. Farthest: **{far['km'] / k:,.0f} {units}**.")
    info_col.caption(
        "You are at the centre. Distance from the centre is true distance and the angle is true compass bearing, so a straight "
        f"line from the centre is the path your signal takes. Each cell is one direction (30\u00b0) and distance band with at least "
        f"{MIN_CELL} {noun}s; the colour is the average SNR there, or how many {noun}s heard you. Strength falls with distance "
        "whatever the antenna, so read the shape and the spread, not just the colour. Blank means nobody there heard you, "
        "which also happens wherever there are no receivers.")


def direction_advantage_chart(adv, names, width_deg, callsign="", selected=None, scale=None):
    """A rose of direction sectors. A bar's LENGTH is how many dB stronger one side is in that direction, its colour says
    which side (blue A, orange B), and how solid it is says how many receivers it rests on (hatched: too few to trust).
    `selected` outlines one sector; `scale` fixes the dB at the outer ring so two roses can be compared."""
    fig = Figure(figsize=(5.2, 5.2))
    ax = _themed_axes(fig, polar=True)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    deltas = adv["delta"].dropna()
    top = float(deltas.abs().max()) if len(deltas) else 0.0
    scale = scale or max(4.0, float(np.ceil(top / 2) * 2))
    hole = 0.2
    theta = np.linspace(0, 2 * np.pi, 240)
    for frac in (0.25, 0.5, 0.75, 1.0):  # rings, labelled in dB
        ax.plot(theta, [hole + (1 - hole) * frac] * len(theta), linestyle="--", linewidth=0.7, color=COLOR_TIE, alpha=0.4)
        ax.text(np.radians(202), hole + (1 - hole) * frac, f"{scale * frac:g} dB", fontsize=6.5, color=COLOR_TIE, ha="center")
    width = np.radians(width_deg) * 0.94
    for _, row in adv.iterrows():
        if row["n"] == 0 or pd.isna(row["delta"]):
            continue
        length = max((1 - hole) * min(abs(row["delta"]) / scale, 1.0), 0.02)
        thin = row["n"] < BIN_MIN_SHARED
        chosen = selected is not None and row["label"] == selected
        color = COLOR_A if row["delta"] > 0 else COLOR_B
        ax.bar(np.radians(row["center"]), length, width=width, bottom=hole, color=color,
               alpha=0.28 if thin else 0.45 + 0.5 * min(row["n"] / 12, 1.0), hatch="///" if thin else None,
               edgecolor="white" if chosen else color, linewidth=2.6 if chosen else 0.6, zorder=2)
        if width_deg >= 30:
            ax.text(np.radians(row["center"]), min(hole + length + 0.06, 1.04), f"{abs(row['delta']):.1f}",
                    ha="center", va="center", fontsize=7.5, color=COLOR_TIE, fontweight="bold")
    ax.fill(theta, [hole] * len(theta), color="#1b2230", zorder=3)
    ax.text(0, 0, callsign or "TX", ha="center", va="center", fontsize=9, color="white", fontweight="bold", zorder=4)
    ax.set_ylim(0, 1.1)
    ax.set_yticks([])
    ax.set_xticks(np.radians(np.arange(0, 360, 45)))
    ax.set_xticklabels(SECTOR_NAMES, color=COLOR_TIE, fontsize=11)
    ax.text(0.5, -0.08, f"Length = how much stronger.  Blue: {names['A']}   Orange: {names['B']}", transform=ax.transAxes,
            ha="center", fontsize=8, color=COLOR_TIE)
    return fig


def _sector_detail(shared, adv, label, width_deg, names, noun):
    """One sentence about the chosen direction: how many receivers, who is stronger, and the 95% range."""
    row = adv[adv["label"] == label]
    if row.empty or int(row["n"].iloc[0]) == 0:
        return f"No {noun} in that direction."
    center, n = float(row["center"].iloc[0]), int(row["n"].iloc[0])
    sector = (((shared["bearing_a"] + width_deg / 2) % 360) // width_deg).astype(int)
    d = shared[sector == int(round(center / width_deg))]["delta"].to_numpy()
    text = f"**{label}: {n} {noun}{'s' if n != 1 else ''} heard both.** {_lead_text(names, float(d.mean()))}."
    if n >= 3:
        t_value = t_crit(n - 1)
        half = t_value * float(d.std(ddof=1)) / np.sqrt(n)
        text += f" 95% range: {_range_text(names, float(d.mean()) - half, float(d.mean()) + half)}."
    else:
        text += " Too few for a range."
    return text


def _shared_counts_note(an, noun):
    """Why 'heard both' and 'compared' are different numbers: pairing, and counting one place once."""
    both, every, n = an["reach"]["both"], len(an["shared_all"]), an["strength"]["n"]
    text = f"**{both} {noun}s heard both sides.**"
    if an["paired"] and every != both:
        text += (f" {every} of them heard both in the same pair of neighbouring transmissions, which is what the strength "
                 "comparison needs.")
    if n != every:
        text += f" Counting {noun}s at the same place once leaves {n}, the number the strength result rests on."
    elif n != both:
        text += f" The strength result rests on {n}."
    return text


def _linear_average_view(spots_a, spots_b, an, names, noun, locs, home, kind, callsign, paired=None, floor=None, min_km=0.0,
                         default_min=1):
    """The SNR average (the standard comparison) next to the same comparison worked out by averaging power instead of dB,
    overall and by direction, and
    when to use which."""
    st.markdown(
        "Reports use dB, a logarithmic scale. The **SNR average** (the standard comparison) measures the **typical SNR difference "
        "at each receiver**. The **linear average** converts SNR to power ratios, averages them for each antenna, then converts back "
        "to dB and subtracts. Linear power averages signal power over time, and it gives stronger "
        "signals more weight, so it needs many reports per receiver. The SNR average is the typical dB difference, and one "
        "strong report can't dominate it, so it holds up better with few reports. Both compare **relative signal strength "
        "at the same receivers**, not absolute antenna gain.")
    with st.expander("Show the arithmetic"):
        st.markdown(
            "1. For every report, turn dB into power: **power = 10 ^ (dB / 10)**\n"
            "2. Average those power numbers for each antenna.\n"
            "3. Turn each average back into dB: **dB = 10 x log10(average power)**\n"
            "4. Subtract the two. That is the relative gain of A against B.\n\n"
            "Example: reports of 0, 0, 0 and 20 dB are powers of 1, 1, 1 and 100. Their average is 25.75, which is **14.1 dB**. "
            "Averaging the dB numbers directly gives 5 dB. The single strong report counts for much more in power.")
    min_spots = st.slider(f"Only {noun}s with at least this many reports on each antenna", 1, 60, default_min,
                          key=f"pm_min_spots_{default_min}",
                          help=f"A long test aims for {DALE_MIN_SPOTS} or more per receiver on each antenna, and the published "
                               f"method keeps only receivers with {DALE_SITE_MIN} or more. A short test can't reach that, but "
                               "raising this drops receivers heard only once or twice, whose single reports can swing a power "
                               "average.")
    s_ = an["strength"]
    pm = power_method(spots_a, spots_b, locs, home, min_spots=min_spots, floor=floor, offset=an["power"]["offset"], min_km=min_km)
    if paired is not None and (paired[0]["time"].nunique() + paired[1]["time"].nunique()
                               != spots_a["time"].nunique() + spots_b["time"].nunique()):
        st.caption(f"The linear average uses every transmission ({spots_a['time'].nunique()} on A, {spots_b['time'].nunique()} on B), "
                   "because averaging power needs no partner transmission. The SNR average compares only paired transmissions "
                   f"({paired[0]['time'].nunique()} on A, {paired[1]['time'].nunique()} on B).")
    if pm["n"] < 2:
        st.info(f"Too few {noun}s heard both antennas with that many reports each. Lower the setting above.")
        return
    if min_spots < DALE_SITE_MIN:
        strict = power_method(spots_a, spots_b, locs, home, min_spots=DALE_SITE_MIN, floor=floor, offset=an["power"]["offset"],
                              min_km=min_km)
        st.caption(f"With the published minimum of {DALE_SITE_MIN} reports on each antenna, "
                   + (f"{strict['n']} {noun}s qualify: linear average {_lead_text(names, strict['mean'])}."
                      if strict["n"] >= 2 else f"too few {noun}s qualify ({strict['n']}), so a long test is needed for that."))
    m1, m2 = st.columns(2)
    m1.metric("SNR average: typical dB difference at the same receiver", _card_lead(s_["mean"]))
    m1.caption(f"{s_['n']} {noun}s counted once per place. 95% range: {_range_text(names, s_['lo'], s_['hi'])}.")
    m2.metric("Linear average: each receiver's average power, A against B", _card_lead(pm["mean"]))
    m2.caption(f"{pm['n']} {noun}s counted once per place. 95% range: {_range_text(names, pm['lo'], pm['hi'])}."
               + (" Reports or receivers you left out above are left out here, but not from the SNR average." if floor is not None or min_km else ""))
    if names["A"] != "A" or names["B"] != "B":
        st.caption(f"A is {names['A']}. B is {names['B']}.")
    gap = abs(s_["mean"] - pm["mean"]) if not np.isnan(s_["mean"]) else np.nan
    if not np.isnan(gap):
        if gap < 1.0:
            st.success(f"The two agree within 1 dB ({gap:.1f} dB apart), so how it is averaged isn't driving the answer. That is a consistency check, not proof.")
        else:
            st.warning(f"The two are {gap:.1f} dB apart. That usually means a few strong reports, or {noun}s with only one or "
                       "two reports each, are pulling the linear average. Try raising the minimum reports above.")
    if pm["near_floor"] >= 0.1:
        st.warning(f"{pm['near_floor']:.0%} of the reports are at or below {WSPR_FLOOR_DB} dB, the edge of what WSPR can decode. "
                   "Signals too weak to decode never appear at all, so the weaker antenna's weakest reports are missing, which can "
                   "shrink the apparent difference. Ticking the box to ignore these reports is a check on how much they matter, "
                   "not a correction. Compare how many receivers heard each antenna too.")
    elif pm["dropped"]:
        st.caption(f"{pm['dropped']} reports at or below {WSPR_FLOOR_DB} dB were ignored.")

    st.subheader("By direction, both ways")
    width = st.radio("Sector width", [45, 30, 15], horizontal=True, key="lin_width", format_func=lambda w: f"{w}\u00b0")
    adv_default = direction_advantage(an["shared"], width)
    adv_linear = direction_advantage(pm["sites"].rename(columns={"gain": "delta"}), width)
    tops = [float(a["delta"].abs().max()) for a in (adv_default, adv_linear) if a["delta"].notna().any()]
    shared_scale = max(4.0, float(np.ceil(max(tops) / 2) * 2)) if tops else 4.0
    left, right = st.columns(2)
    left.markdown("**SNR average**")
    left.pyplot(direction_advantage_chart(adv_default, names, width, callsign, scale=shared_scale), width="stretch")
    right.markdown("**Linear average**")
    right.pyplot(direction_advantage_chart(adv_linear, names, width, callsign, scale=shared_scale), width="stretch")
    both = adv_default.assign(linear=adv_linear["delta"], n_linear=adv_linear["n"])
    both = both[(both["n"] > 0) | (both["n_linear"] > 0)]
    st.dataframe(pd.DataFrame({
        "Direction": both["label"], f"{noun.capitalize()}s": both["n"].astype(int),
        "SNR average": [_gain_text(v) for v in both["delta"]], "Linear average": [_gain_text(v) for v in both["linear"]]}),
        width="stretch", hide_index=True, height=min(38 * (len(both) + 1), 360))
    st.caption("Both charts use the same scale, so you can compare bar lengths. Where the two differ a lot in one direction, "
               "a few strong reports from there are probably pulling the linear average.")

    with st.expander(f"Receiver by receiver, both ways"):
        t = pm["table"]
        default = an["shared_all"]["delta"] if "shared_all" in an else pd.Series(dtype=float)
        view = pd.DataFrame({
            noun.capitalize(): t.index, "Reports A": t["spots_a"].astype(int).to_numpy(),
            "Reports B": t["spots_b"].astype(int).to_numpy(), "Linear average": [_gain_text(v) for v in t["gain"]],
            "SNR average": [_gain_text(default.get(i, np.nan)) for i in t.index]}).sort_values("Reports A", ascending=False)
        st.dataframe(view, width="stretch", hide_index=True, height=min(38 * (len(view) + 1), 380))

    st.subheader("Which should you use?")
    left, right = st.columns(2)
    left.markdown(
        "**SNR average: the typical dB difference at the same receiver**\n\n"
        "Best for a **quick test** (8 to 16 transmissions, switching after each one). A and B are measured minutes apart, so "
        "fading and band changes mostly cancel, and one very strong report can't dominate. It gives a verdict and ranges by "
        "direction and distance.\n\n"
        "*Limits:* it averages in dB, which isn't a power average, and it assumes conditions barely change between "
        "neighbouring transmissions, which isn't always true (WSPR SNR can move several dB between 2-minute windows). It "
        "relies on having many receivers to average that out.")
    right.markdown(
        "**Linear average: each receiver's average power**\n\n"
        "Best for a **long test**: run one antenna for 30 minutes to an hour, then the other, and repeat for several hours. "
        "Each receiver then hears you 20 to 40 times on each antenna. For every receiver, the site averages all its reports "
        "for A and all its reports for B (as power, not dB) and compares the two averages.\n\n"
        "Any single report bounces up and down with fading, but over 20 to 40 reports those bounces even out, so the "
        "averages are steady. The result no longer depends on two particular transmissions lining up, and the 95% range "
        "(how far off the answer could plausibly be) comes out narrow.\n\n"
        "*Limits:* it needs many reports per receiver, so on a short test one lucky report can swing the result. Strong "
        "reports count for far more than weak ones, so a single peak can pull the average up. "
        "Reports right at WSPR's -28 dB floor throw it off, and a long run mixes in whatever the band did meanwhile.")
    st.markdown(
        "**In practice:** if the two agree within 1 dB, the answer holds either way. If they don't, look at the reports near the "
        "floor and raise the minimum reports. To use the long-test method as intended, choose **In long blocks** under *Your test* "
        "and give the start time, block length and number of blocks.")


def _method_summary(spots_a, spots_b, an, names, noun, locs, home):
    """One paragraph for the PDF: the SNR average next to the linear average, and whether they agree."""
    s_ = an["strength"]
    pm = power_method(spots_a, spots_b, locs, home, offset=an["power"]["offset"])
    if pm["n"] < 2 or np.isnan(s_["mean"]):
        return ""
    gap = abs(s_["mean"] - pm["mean"])
    return (f"SNR average (typical dB difference at the same {noun}): {_lead_text(names, s_['mean'])}. "
            f"Linear average (each {noun}'s average power, A against B): {_lead_text(names, pm['mean'])}. "
            + (f"The two agree within 1 dB ({gap:.1f} dB apart)." if gap < 1.0 else
               f"The two are {gap:.1f} dB apart, usually a few strong reports or {noun}s with only one or two reports."))


def _range_text(names, lo, hi):
    """'B ahead by 6.3 to 14.7 dB' when the whole range favours one side; otherwise 'A ahead by 1.4 dB to B ahead by 0.9 dB'."""
    if lo > 0 and hi > 0:
        return f"{names['A']} ahead by {lo:.1f} to {hi:.1f} dB"
    if lo < 0 and hi < 0:
        return f"{names['B']} ahead by {-hi:.1f} to {-lo:.1f} dB"
    return f"{_lead_text(names, lo)} to {_lead_text(names, hi)}"


def _gain_text(v):
    return "-" if pd.isna(v) or abs(v) < 0.05 else f"{'A' if v > 0 else 'B'} +{abs(v):.1f}"


def _gain_summary_view(summary, first_col, noun):
    """A gain_summary table as text for the screen and the PDF: the group, how many receivers, the average and its range."""
    def fmt(v):
        return "\u2013" if pd.isna(v) else f"{v:+.1f}"

    def rng(row):
        return "\u2013" if pd.isna(row["low"]) else f"{row['low']:+.1f} to {row['high']:+.1f}"

    return pd.DataFrame({
        first_col: summary["group"], f"{noun.capitalize()}s": summary["receivers"],
        "Average gain B \u2212 A (dB)": [fmt(v) for v in summary["mean"]],
        "95% range": [rng(r) for _, r in summary.iterrows()],
        "Note": ["" if n >= BIN_MIN_SHARED else ("none" if n == 0 else f"only {n}, a hint") for n in summary["receivers"]]})


def _card_lead(v):
    """A short value for a big summary card, such as 'B +4.9 dB', so a long antenna name can't cut it off."""
    if pd.isna(v) or abs(v) < 0.05:
        return "no difference"
    return f"{'A' if v > 0 else 'B'} +{abs(v):.1f} dB"


def _data_used(all_spots, spots, map_a, map_b, spots_a, spots_b, an, ta_all, tb_all, noun, sep, unpaired, listening_note, ghosts):
    """What went into a comparison and what was left out, as a short table and a few sentences, for the screen and the PDF."""
    n = lambda x: f"{int(x):,}"  # noqa: E731
    in_test, compared = len(map_a) + len(map_b), len(spots_a) + len(spots_b)
    heard_a, heard_b = set(map_a["spotter"]), set(map_b["spotter"])
    both_all = heard_a & heard_b
    shared = an["shared_all"]
    counted_once = int(an["strength"]["n"])
    unlocated = (heard_a | heard_b) - (set(ta_all.index) | set(tb_all.index))
    reports = pd.concat([spots_a.groupby("spotter").size().reindex(shared.index),
                         spots_b.groupby("spotter").size().reindex(shared.index)], axis=1).min(axis=1).fillna(0)
    enough = {t: int((reports >= t).sum()) for t in (5, 20, 40)}
    rows = {"Spots in the test": [n(len(map_a)), n(len(map_b))],
            "Spots used in the comparison": [n(len(spots_a)), n(len(spots_b))],
            f"{noun.capitalize()}s that heard it": [n(len(heard_a)), n(len(heard_b))]}
    if sep.get("time_based"):
        rows["Transmissions"] = [n(map_a["time"].nunique()), n(map_b["time"].nunique())]
    lines = [f"{n(len(all_spots))} spots were loaded; {n(len(spots))} remain after your filters; {n(in_test)} are in the test and "
             f"{n(compared)} were used in the comparison.",
             f"{len(heard_a)} {noun}s heard A and {len(heard_b)} heard B; {len(both_all)} heard both. {len(shared)} of those have a "
             f"known position and were compared, and {counted_once} remain after counting {noun}s at the same place once.",
             f"With at least 5, 20 and 40 reports on each antenna: {enough[5]}, {enough[20]} and {enough[40]} {noun}s."
             + (f" A long test aims for {DALE_MIN_SPOTS} or more, and the published method keeps {DALE_SITE_MIN}."
                if sep.get("blocks") else "")]
    left = []
    if unpaired:
        left.append(f"{unpaired} transmission{'s' if unpaired != 1 else ''} with no partner on the other antenna")
    if ghosts:
        left.append(f"{ghosts} cycle{'s' if ghosts != 1 else ''} heard by only 1 or 2 receivers")
    if listening_note and listening_note["kept"] < listening_note["total"]:
        gone = listening_note["total"] - listening_note["kept"]
        left.append(f"{gone} {noun}{'s' if gone != 1 else ''} not listening during both sides")
    if unlocated:
        left.append(f"{len(unlocated)} {noun}{'s' if len(unlocated) != 1 else ''} with no known position")
    if len(spots) - in_test > 0:
        left.append(f"{n(len(spots) - in_test)} spots outside the test")
    total_tx, used_tx = spots["time"].nunique(), pd.concat([map_a["time"], map_b["time"]]).nunique()
    if sep.get("time_based") and not sep.get("blocks") and total_tx >= 20 and used_tx < total_tx / 2:
        lines.insert(1, f"⚠ This quick test uses {used_tx} of the {total_tx} transmissions in the data. For a long test with "
                        "one antenna at a time for 30 minutes or more, choose In long blocks.")
    lines.append("Left out: " + "; ".join(left) + "." if left else "Nothing was left out.")
    label = (f"Data used: {n(compared)} spots from {len(set(spots_a['spotter']) | set(spots_b['spotter']))} {noun}s, "
             f"{counted_once} compared")
    return {"label": label, "rows": rows, "lines": lines}


def compare_view(spots, locs, home, label, callsign, file_date, tiles, show_all, units, split, gap_khz,
                 scale, noun, kind, all_spots):
    """Compare mode: separate `spots` into an A side and a B side (by frequency, time window, timed blocks,
    or every other transmission), then say which one is getting out better, by how much, and in which directions."""
    name_a, name_b = _name_inputs()
    if split == SPLIT_FREQ:
        sep = _separate_by_frequency(spots, gap_khz)
    else:
        sep = _separate_by_log(spots)
    if sep is None:
        return
    spots_a, spots_b = sep["a"], sep["b"]
    if spots_a.empty or spots_b.empty:
        st.warning("One side has no spots. Adjust the selection above, or the filters in the sidebar.")
        return
    map_a, map_b = spots_a, spots_b  # the maps and timeline show everything that was heard
    unpaired = 0
    if kind == "WSPR" and sep.get("time_based"):
        if sep.get("labeled") and count_pairs(spots_a, spots_b) >= 1:
            # An alternating test is judged pair by pair, and a pair has exactly one transmission on each antenna, so
            # the time on the air is equal by construction. A transmission with no partner on the other antenna has
            # nothing to be compared with, so it is left out of the comparison (it still shows on the maps). Thinning
            # the longer side at random instead could throw away a paired transmission and leave nothing to compare.
            spots_a, spots_b = spots_a[spots_a["round"].notna()], spots_b[spots_b["round"].notna()]
            sep["exposure"] = None
            unpaired = (map_a["time"].nunique() - spots_a["time"].nunique()
                        + map_b["time"].nunique() - spots_b["time"].nunique())
            if unpaired:
                st.caption(f"\u2139\ufe0f {unpaired} transmission{'s' if unpaired != 1 else ''} had no partner on the other "
                           "antenna, so they are left out of the A/B comparison (they still show on the maps).")
        else:
            # For WSPR the time on the air that matters is the transmissions that actually happened (2 minutes each),
            # not the scheduled minutes: a transmitter that picks its slots at random can put far more of them on one side.
            n_a, n_b = spots_a["time"].nunique(), spots_b["time"].nunique()
            sep["exposure"] = (2.0 * n_a, 2.0 * n_b) if abs(n_a - n_b) > 1 else None
    listening_note = None
    if kind == "WSPR" and sep.get("time_based"):
        bands = set(spots_a["band"]) | set(spots_b["band"])
        if len(bands) == 1:
            times = pd.concat([spots_a["time"], spots_b["time"]])
            try:
                with st.spinner("Checking which receivers were listening\u2026"):
                    listening, _ = load_listening(next(iter(bands)), times.min().to_pydatetime(),
                                                  (times.max() + pd.Timedelta(minutes=2)).to_pydatetime())
                eligible = eligible_receivers(spots_a, spots_b, listening)
                heard = set(spots_a["spotter"]) | set(spots_b["spotter"])
                listening_note = {"kept": len(heard & eligible), "total": len(heard)}
                spots_a = spots_a[spots_a["spotter"].isin(eligible)]
                spots_b = spots_b[spots_b["spotter"].isin(eligible)]
            except WsprError as exc:
                st.warning(f"Couldn't check which receivers were listening ({exc}). Receivers that only listen in some "
                           "time slots may make one side look like it reached more.")
            if spots_a.empty or spots_b.empty:
                st.warning("No receiver was listening during both sides' transmissions, so there is nothing fair to "
                           "compare. Try a longer test.")
                return
    names = {"A": name_a or sep["default"]["A"], "B": name_b or sep["default"]["B"]}
    if names["A"] == names["B"]:  # the names become table headings, so they have to differ
        names = {"A": names["A"] + " (A)", "B": names["B"] + " (B)"}

    # ---- power: WSPR sends it with every spot; for RBN you can say what you ran
    with st.expander("Power", expanded=False):
        if kind == "WSPR":
            pa, pb = median_power_dbm(spots_a), median_power_dbm(spots_b)
            watts = lambda dbm: 10 ** ((dbm - 30) / 10)  # noqa: E731
            st.caption(f"Transmit power from the spots: {names['A']} {pa:.0f} dBm ({watts(pa):.2g} W), "
                       f"{names['B']} {pb:.0f} dBm ({watts(pb):.2g} W).")
            normalize = st.checkbox("Correct for any power difference (recommended)", value=True,
                                    help="If the two tests used different power, B's SNR is shifted to A's power so the "
                                         "antennas are compared, not the transmitters.")
        else:
            st.caption("Optional. If the two tests ran at different power, enter both so the comparison is about "
                       "the antennas, not the transmitters. Leave at 0 if you don't know or they were equal.")
            c1, c2 = st.columns(2)
            watts_a = c1.number_input(f"Power for {names['A']} (watts)", min_value=0.0, value=0.0, step=5.0)
            watts_b = c2.number_input(f"Power for {names['B']} (watts)", min_value=0.0, value=0.0, step=5.0)
            pa, pb = db_from_watts(watts_a), db_from_watts(watts_b)
            normalize = True

    an = analyze(spots_a, spots_b, locs, home, pa, pb, normalize, units, exposure=sep.get("exposure"))
    # Describe what was heard the way the maps do, from every receiver that heard each side. The fairness checks (listening
    # during both sides, real A/B pairs) decide who counts for the reach and strength verdict, but they would otherwise
    # hide a receiver from the "farthest" figure that the map plainly shows.
    ta_all, tb_all = receiver_table(map_a, locs, home), receiver_table(map_b, locs, home)
    far_all_a, far_all_b = _farthest(ta_all), _farthest(tb_all)
    if far_all_a and far_all_b:
        an["dx"] = {**an["dx"], "a": far_all_a, "b": far_all_b}
    rounds = None
    if sep.get("alternating") and "round" in spots_a:
        rounds = round_consistency(spots_a, spots_b, locs, home, an["power"]["offset"])
    n_pairs = count_pairs(map_a, map_b)
    sequential = not sep.get("alternating")
    if sep.get("labeled"):  # a log of mostly unpaired transmissions (A A A B B B) is a one-after-the-other test
        transmissions = map_a["time"].nunique() + map_b["time"].nunique()
        sequential = transmissions > 0 and 2 * n_pairs < 0.5 * transmissions
    if sep.get("blocks"):
        sequential = sep["n_blocks"] < 4  # a few alternations average out the changing band; one A then one B does not
    close_in_time = None
    if sequential and not sep.get("labeled") and not sep.get("blocks"):
        # Two frequencies or two windows sent within a few minutes of each other saw the same band conditions, so the
        # warning that the time of day could explain the difference does not apply.
        all_times = pd.concat([map_a["time"], map_b["time"]])
        span_minutes = (all_times.max() - all_times.min()).total_seconds() / 60
        if span_minutes <= SIMULTANEOUS_MINUTES:
            sequential, close_in_time = False, span_minutes
    verdict = build_verdict(an, names, exposure=sep.get("exposure") if sep.get("time_based") else None,
                            rounds=rounds, receiver_word=noun, sequential=sequential,
                            n_pairs=n_pairs if sep.get("alternating") else None)

    if close_in_time is not None:
        verdict["caveats"].append(
            "A and B were sent within " + (f"{close_in_time:.0f} minute{'s' if round(close_in_time) != 1 else ''}"
                                           if close_in_time >= 1 else "a minute") +
            " of each other, so they saw the same band conditions and the time of day is not a concern here.")
    if listening_note and listening_note["kept"] < listening_note["total"]:
        verdict["caveats"].append(
            f"Only receivers that were listening during both sides' transmissions are counted ({listening_note['kept']} "
            f"of {listening_note['total']}). Many WSPR receivers hop between bands and only listen in some time slots, "
            "which would otherwise look like one side reaching more.")

    r, s_, dx = an["reach"], an["strength"], an["dx"]
    k = KM_PER_MILE if units == "mi" else 1
    means_a, means_b = sector_means(ta_all), sector_means(tb_all)
    with st.expander("Leave out some reports or receivers (for the averages)"):
        f1, f2 = st.columns(2)
        floor_on = kind == "WSPR" and f1.checkbox(
            f"Ignore reports at or below {WSPR_FLOOR_DB} dB", key="avg_floor",
            help="WSPR can only decode down to about -28 dB. A report at that edge is unreliable, and the weaker antenna's "
                 "weakest reports are often missing altogether.")
        near = float(f2.number_input(f"Leave out receivers closer than ({units})", 0, 5000, 0, step=10, key=f"avg_near_{units}",
                                     help="Close stations can hear you by ground wave, which says little about the antenna's "
                                          "take-off angle. Leave at 0 to keep every receiver."))
        st.caption("This changes the averages on the Receiver by receiver tab and the linear average on the Linear average tab. "
                   "The verdict, the SNR average and the other tabs are not changed.")
    avg_floor = WSPR_FLOOR_DB if floor_on else None
    avg_min_km = near * (KM_PER_MILE if units == "mi" else 1.0)
    avg_note = ", ".join(([f"reports at or below {WSPR_FLOOR_DB} dB left out"] if floor_on else [])
                         + ([f"receivers closer than {near:g} {units} left out"] if near else []))
    tab_result, tab_where, tab_each, tab_method, tab_maps = st.tabs(
        ["\U0001F4CA Result", "\U0001F9ED Direction & distance", "\U0001F4CB Receiver by receiver",
         "\U0001F4D0 Linear average", "\U0001F5FA️ Maps"])

    with tab_result:
        snr_note = f"at the {s_['n']} {noun}s that heard both" if s_["n"] else f"no {noun} heard both"
        avg_a = f"{an['shared']['snr_a'].mean():.1f} dB" if s_["n"] else "—"
        avg_b = f"{an['shared']['snr_b'].mean():.1f} dB" if s_["n"] else "—"
        far = lambda d: (f"{d['km'] / k:,.0f} {units}", d["who"]) if d else ("—", "")  # noqa: E731
        typical = lambda t: (f"{t['km'].median() / k:,.0f} {units}", "") if len(t) else ("—", "")  # noqa: E731
        score_rows = [
            (f"Unique {noun}s that heard you", ("equal time on the air, " if an["equalized"] else "") + ("listening during both, " if listening_note else "") + "each one counted once · more is better",
             (r["a"], f"from {r['spots_a']:,} spots"), (r["b"], f"from {r['spots_b']:,} spots"),
             r["call"] if r["call"] in ("A", "B") else None),
            ("Average SNR", snr_note, (avg_a, ""), (avg_b, ""), s_["call"] if s_["call"] in ("A", "B") else None),
            (f"Farthest {noun}", "your longest path", far(dx["a"]), far(dx["b"]), dx["call"]),
            ("Typical distance", "half the stations were closer than this", typical(ta_all), typical(tb_all), None),
            ("Strongest direction", "where your signal was best",
             (best_direction(means_a), ""), (best_direction(means_b), ""), None),
        ]
        st.markdown(verdict_card_html(verdict), unsafe_allow_html=True)
        n_a, n_b = map_a["time"].nunique(), map_b["time"].nunique()
        used = _data_used(all_spots, spots, map_a, map_b, spots_a, spots_b, an, ta_all, tb_all, noun, sep, unpaired,
                          listening_note, st.session_state.get("ghost_cycles", 0))
        pdf_ctx = dict(data_used=used, 
            names=names, an=an, verdict=verdict, noun=noun, units=units, score_rows=score_rows, callsign=callsign, label=label,
            file_date=file_date, source="WSPR" if kind == "WSPR" else "Reverse Beacon Network",
            bands=", ".join(sorted(set(map_a["band"]) | set(map_b["band"]))),
            test=(f"Test: {n_a} transmission{'s' if n_a != 1 else ''} on A, {n_b} on B" if sep.get("labeled")
                  else "Test: A and B separated by " + ("frequency" if not sep.get("time_based") else "time")),
            time_based=bool(sep.get("time_based")), means_a=means_a, means_b=means_b,
            adv=direction_advantage(an["shared"], 45), counts_note=_shared_counts_note(an, noun),
            az_points=_azimuth_points(ta_all, tb_all, an), home=home, scale=scale,
            method=_method_summary(spots_a, spots_b, an, names, noun, locs, home),
            timeline=(map_a, map_b))
        st.download_button("\u2B07\ufe0f Download report (PDF)", data=lambda: compare_report_pdf(pdf_ctx),
                           file_name=f"RBN_compare_{callsign}_{file_date}.pdf", mime="application/pdf",
                           help="The verdict, scoreboard and every chart and table, as a PDF you can keep or share.")
        with st.expander(used["label"]):
            st.dataframe(pd.DataFrame(used["rows"], index=[names["A"], names["B"]]).T, width="stretch")
            for line in used["lines"]:
                st.markdown(f"- {line}")
        st.markdown(scoreboard_html(names["A"], names["B"], score_rows), unsafe_allow_html=True)

        st.markdown(f"##### Who heard you on each {'side' if sep.get('time_based') else 'frequency'}?")
        st.markdown(reach_bar_html(r["only_a"], r["both"], r["only_b"]), unsafe_allow_html=True)
        st.caption(f"Each {noun} is counted once, however many times it spotted you. A *spot* is one report, so "
                   "spots always outnumber stations.")
        st.markdown(_shared_counts_note(an, noun))

        st.subheader("Timeline")
        st.pyplot(timeline_chart(map_a, map_b, names), width="stretch")
        st.caption("Each faint dot is one spot; the solid dots are the median of each transmission, placed in the "
                   "order they happened (a dotted line marks a long pause). If one colour sits above the other "
                   "throughout, that's a real difference; if they swap places, propagation moved around more than "
                   "the antennas differ.")
        if rounds is not None and len(rounds) >= 3:
            if len(rounds) <= 12:
                st.caption("Who was stronger in each round, and by how much (dB): "
                           + " \u00b7 ".join("equal" if abs(d) < 0.05 else f"{'A' if d > 0 else 'B'} +{abs(d):.1f}"
                                             for d in rounds["delta"]))
            else:
                st.caption(f"{len(rounds)} rounds (one A transmission and the B one after it). A was stronger in "
                           f"{int((rounds['delta'] > 0).sum())}, B in {int((rounds['delta'] < 0).sum())}; the typical "
                           f"round: {_lead_text(names, float(rounds['delta'].median()))}.")

    with tab_where:
        az_points = _azimuth_points(ta_all, tb_all, an)
        if len(az_points):
            st.subheader("Where each antenna reached")
            _azimuth_map_section(az_points, an, names, noun, units, home, callsign, scale)
        if s_["n"]:
            st.subheader("Which side is stronger, by direction")
            width = st.radio("Sector width", [45, 30, 15], horizontal=True, key="adv_width",
                             format_func=lambda w: f"{w}\u00b0", help="Narrower sectors show more detail but each holds fewer receivers.")
            adv = direction_advantage(an["shared"], width)
            have = adv[adv["n"] > 0]
            options = ["All"] + list(have["label"])
            picked = st.pills("Look at one direction", options, default="All", key="adv_sector",
                              help="Outlines that direction on the chart and gives its numbers.")
            picked = picked if picked in options else "All"
            chart_col, table_col = st.columns([1, 1])
            chart_col.pyplot(direction_advantage_chart(adv, names, width, callsign, None if picked == "All" else picked),
                             width="stretch")
            if picked != "All":
                table_col.markdown(_sector_detail(an["shared"], adv, picked, width, names, noun))
            table_col.dataframe(pd.DataFrame({
                "Direction": have["label"], f"{noun.capitalize()}s": have["n"].astype(int),
                "Stronger by": [_gain_text(v) for v in have["delta"]],
                "Note": ["only " + str(int(n)) + " - treat as a hint" if n < BIN_MIN_SHARED else "" for n in have["n"]]}),
                width="stretch", hide_index=True, height=min(38 * (len(have) + 1), 420))
            table_col.caption("The longer the bar, the more one side beat the other toward that direction; blue is A and orange "
                              "is B. A more solid bar rests on more receivers, and a hatched one on too few to trust. Unlike "
                              "the average-SNR chart below, there is nothing to subtract in your head.")
        if means_a.notna().any() or means_b.notna().any():
            st.subheader("Average SNR by direction")
            min_dir = int(st.slider(f"Only directions with at least this many {noun}s", 1, 20, 1, key="snr_dir_min",
                                    help="A direction with only one or two receivers can stretch the chart. Raise this to leave "
                                         "those directions out of the chart (the table keeps them)."))
            if min_dir > 1:
                means_a, means_b = sector_means(ta_all, min_dir), sector_means(tb_all, min_dir)
                pdf_ctx["means_a"], pdf_ctx["means_b"] = means_a, means_b
            chart_col, table_col = st.columns([1, 1])
            chart_col.pyplot(direction_chart(means_a, means_b, names["A"], names["B"]), width="stretch")
            table_col.dataframe(breakdown_table(an["direction"], names, "Direction", noun), width="stretch", hide_index=True,
                                height=35 * (len(an["direction"]) + 1) + 3)
            table_col.caption("Distance from the centre in the chart is the average SNR toward that direction. In the "
                              "table, “Stronger by” is measured at the stations that heard both, and a side is "
                              f"“better” past {DIRECTION_EDGE_DB:g} dB.")
        if len(an["distance"]) and an["distance"][["a", "b"]].to_numpy().sum():
            st.subheader("Distance")
            chart_col, table_col = st.columns([1, 1])
            chart_col.pyplot(distance_chart(an["distance"], names), width="stretch")
            table_col.dataframe(breakdown_table(an["distance"], names, "Distance", noun), width="stretch", hide_index=True,
                                height=35 * (len(an["distance"]) + 1) + 3)
            table_col.caption("Bars show how much stronger one side was at the stations that heard both; a faded bar "
                              "means fewer than 3 stations, so don't lean on it. "
                              "Longer paths usually mean a lower take-off angle. If one antenna wins close in and the "
                              "other wins far out, that's the signature of high-angle versus low-angle performance.")

    with tab_each:
        if s_["n"]:
            blocks_mode = bool(sep.get("blocks"))
            ctrl_method, ctrl_min = st.columns([2, 3])
            method_label = ctrl_method.radio(
                f"Average each {noun}'s reports by", ["SNR (dB)", "Linear power"], index=1 if blocks_mode else 0,
                horizontal=True, key=f"rx_method_{'blk' if blocks_mode else 'pair'}",
                help="SNR (dB) takes the middle of a station's reports in dB. Linear power turns each report into power, "
                     "averages the power and turns it back into dB. Linear is the proper average of signal power over "
                     "time and suits a long test with many reports per station; the dB middle is steadier when a station "
                     "has only a few reports.")
            min_reports = int(ctrl_min.slider(
                f"Only {noun}s with at least this many reports on each antenna", 1, 60, DALE_MIN_SPOTS if blocks_mode else 1,
                key=f"rx_min_reports_{'blk' if blocks_mode else 'pair'}",
                help="Raising this drops stations with few reports. The far, weak ones go first, but each station left has a "
                     "steadier average, and the answer settling as you raise it is a sign it can be trusted."))
            plain = method_label == "SNR (dB)" and min_reports == 1 and avg_floor is None and not avg_min_km
            if avg_note:
                st.caption("Averages here have " + avg_note + ".")
            fa = spots_a if avg_floor is None else spots_a[spots_a["snr"] > avg_floor]
            fb = spots_b if avg_floor is None else spots_b[spots_b["snr"] > avg_floor]
            if plain:
                every, scatter_src = an["shared_all"], an["shared"]
            else:
                every = station_gains(spots_a, spots_b, locs, home, "linear" if method_label == "Linear power" else "snr",
                                      min_reports, an["power"]["offset"], avg_floor, avg_min_km)
                scatter_src = every
            if every.empty:
                st.info(f"No {noun} has {min_reports} or more reports on both antennas. Lower the number above.")
            else:
                pdf_ctx["every"], pdf_ctx["scatter_src"] = every, scatter_src
                pdf_ctx["rx_note"] = (f"Each SNR is the {'average power, in dB,' if method_label == 'Linear power' else 'median'} "
                                      f"of that station's spots"
                                      + (f"; only {noun}s with at least {min_reports} reports on each antenna are included." if min_reports > 1
                                         else "."))
                chart_col, table_col = st.columns([1, 1])
                chart_col.pyplot(paired_scatter(scatter_src, names), width="stretch")
                if plain:
                    same_place = (f" ({len(every)} {noun}s; ones at the same place are averaged into one for the result)"
                                  if len(every) != s_["n"] else "")
                    table_col.caption(f"{s_['n']} {noun}s heard both{same_place}. \U0001F535 A was stronger at {s_['wins_a']}, "
                                      f"\U0001F7E0 B at {s_['wins_b']}, tied at {s_['ties']}. Each SNR is the median of that "
                                      f"{noun}'s spots. Click a column heading to sort.")
                else:
                    wins_a, wins_b = int((every["gap"] > 0).sum()), int((every["gap"] < 0).sum())
                    table_col.caption(f"{len(every)} {noun}s with at least {min_reports} report{'s' if min_reports != 1 else ''} on each antenna. "
                                      f"\U0001F535 A was stronger at {wins_a}, \U0001F7E0 B at {wins_b}, tied at "
                                      f"{len(every) - wins_a - wins_b}. Each SNR is the "
                                      f"{'average power, in dB,' if method_label == 'Linear power' else 'median'} of that "
                                      f"{noun}'s spots. Click a column heading to sort.")
                table_col.dataframe(head_to_head_table(every, units), width="stretch",
                                    height=min(38 * (len(every) + 1), 420))
            if not every.empty:
                st.subheader("Relative gain by direction and distance")
                st.caption(f"Relative gain is B's SNR minus A's SNR at the same {noun}. Think of A as the reference antenna and "
                           f"B as the test antenna: above zero, the test antenna gave that {noun} more signal. Because it is "
                           f"a difference at one {noun}, that {noun}'s own noise level cancels, so {noun}s can be compared "
                           "with each other. This holds when both antennas used the same power and receiver and have "
                           "similar patterns.")
                gain_dir, gain_dist = st.columns(2)
                gain_dir.pyplot(relative_gain_chart(every, names, units, "direction"), width="stretch")
                gain_dist.pyplot(relative_gain_chart(every, names, units, "distance"), width="stretch")

                st.markdown("##### Average relative gain by direction and distance")
                c_groups, c_short, c_medium = st.columns([2, 1, 1])
                groups = c_groups.radio("Group directions as", ["Octants (8)", "Quadrants (4)"], horizontal=True, key="gs_groups",
                                        help="Each direction is centred on its compass point, like the direction charts: N covers "
                                             "337\u00b0 to 22\u00b0 for octants, and 315\u00b0 to 45\u00b0 for quadrants.")
                kk_gs = KM_PER_MILE if units == "mi" else 1.0
                short_max = float(c_short.number_input(f"Short ends at ({units})", min_value=50, max_value=20000,
                                                       value=1000 if units == "mi" else 1500, step=100, key=f"gs_short_{units}"))
                medium_max = float(c_medium.number_input(f"Medium ends at ({units})", min_value=100, max_value=20000,
                                                         value=2500 if units == "mi" else 4000, step=100, key=f"gs_med_{units}"))
                if medium_max <= short_max:
                    st.warning("Medium has to end beyond where Short ends. Using the two numbers in order.")
                    short_max, medium_max = sorted((short_max, medium_max))
                dir_sum = gain_summary(every, "octant" if groups.startswith("Octants") else "quadrant")
                dist_labels = [f"Short: under {short_max:,.0f} {units}", f"Medium: {short_max:,.0f} to {medium_max:,.0f} {units}",
                               f"Long: over {medium_max:,.0f} {units}"]
                dist_sum = gain_summary(every, "distance", [short_max * kk_gs, medium_max * kk_gs], dist_labels)
                gs_dir = _gain_summary_view(dir_sum, "Direction", noun)
                gs_dist = _gain_summary_view(dist_sum, "Distance", noun)
                pdf_ctx["gain_summary"] = {"direction": gs_dir, "distance": gs_dist}
                t_dir, t_dist = st.columns(2)
                t_dir.dataframe(gs_dir, width="stretch", hide_index=True, height=35 * (len(gs_dir) + 1) + 3)
                t_dist.dataframe(gs_dist, width="stretch", hide_index=True, height=35 * (len(gs_dist) + 1) + 3)
                st.caption(f"The average of B \u2212 A at the {noun}s in each group, with {noun}s at the same place counted once and "
                           f"a 95% range. Above zero, B was stronger there. A group with fewer than {BIN_MIN_SHARED} {noun}s is a "
                           f"hint, not a result. It uses the same averaging ({method_label}) and minimum reports as the charts above.")

                st.subheader(f"One {noun} over time")
                reports_a, reports_b = fa.groupby("spotter").size(), fb.groupby("spotter").size()
                kk = KM_PER_MILE if units == "mi" else 1
                sort_by = st.radio("Sort the list by", ["A\u2013Z", "Distance", "Direction", "Most reports"], horizontal=True,
                                   key="rx_sort", help="You can also click the box and type part of a callsign to find it.")
                order_key = {"A\u2013Z": lambda r: r,
                             "Distance": lambda r: every.at[r, "km_a"],
                             "Direction": lambda r: every.at[r, "bearing_a"],
                             "Most reports": lambda r: (-min(reports_a.get(r, 0), reports_b.get(r, 0)), every.at[r, "km_a"])}[sort_by]
                ranked = sorted(every.index, key=order_key)
                pick = st.selectbox(
                    f"Pick a {noun}", ranked,
                    format_func=lambda r: (f"{r} \u00b7 {every.at[r, 'km_a'] / kk:,.0f} {units} "
                                           f"{COMPASS_16[int(((every.at[r, 'bearing_a'] + 11.25) % 360) // 22.5)]} \u00b7 "
                                           f"{reports_a.get(r, 0)} on A, {reports_b.get(r, 0)} on B"))
                offset = an["power"]["offset"]
                st.pyplot(receiver_time_chart(fa, fb, pick, names, offset, linear=method_label == "Linear power"),
                          width="stretch")
                one_a = fa[fa["spotter"] == pick]["snr"].to_numpy(float)
                one_b = fb[fb["spotter"] == pick]["snr"].to_numpy(float) + offset
                m_a, m_b, m_gain = st.columns(3)
                for col, label, values in ((m_a, names["A"], one_a), (m_b, names["B"], one_b)):
                    col.metric(label, f"{_linear_db(values):.1f} dB")
                    col.caption(f"Linear average \u00b7 {len(values)} reports \u00b7 dB average {values.mean():.1f} \u00b7 middle {np.median(values):g} "
                                f"\u00b7 spread (std) {values.std(ddof=1) if len(values) > 1 else float('nan'):.1f} dB")
                diff, lo, hi = _gain_interval(one_a, one_b)
                m_gain.metric("Relative gain B \u2212 A", f"{_linear_db(one_b) - _linear_db(one_a):+.1f} dB")
                m_gain.caption(f"Linear power. dB averages: {diff:+.1f} dB"
                               + (f", 95% range {lo:+.1f} to {hi:+.1f} dB" if not np.isnan(lo) else ", too few reports for a range"))
                st.caption(f"Each dot is one report from this {noun}; the line is each antenna's average "
                           f"({'linear power' if method_label == 'Linear power' else 'middle of the dB reports'}). "
                           "Wide scatter means the signal was fading or the band was changing, which is why a few reports "
                           "can give a misleading average. In a long-block test the shading shows when each antenna was "
                           "connected.")
                if sep.get("blocks") and sep.get("t0") is not None:
                    st.subheader("Relative gain over time")
                    block_min = sep["block_min"]
                    t_start = sep["t0"]
                    t_end = sep.get("t_end") or t_start + pd.Timedelta(minutes=block_min * sep["n_blocks_set"])
                    multiples = [m for m in (2, 4, 6, 8) if m * block_min <= (t_end - t_start).total_seconds() / 60]
                    c_len, c_min = st.columns(2)
                    mult = c_len.selectbox("Window length", multiples or [2], index=min(1, max(len(multiples) - 1, 0)),
                                           format_func=lambda m: (f"{m * block_min // 60} hour{'s' if m * block_min != 60 else ''}"
                                                                  if m * block_min >= 60 and m * block_min % 60 == 0
                                                                  else f"{m * block_min:g} minutes"),
                                           key=f"rx_win_len_{block_min}",
                                           help="A window should hold whole A and B blocks so both antennas get equal time. "
                                                "Two hours (four 30-minute blocks) balances either block order.")
                    win_min = c_min.slider(f"Minimum reports per {noun} in a window", 1, 20, 3, key="rx_win_min",
                                           help=f"Each window is averaged on its own, so it holds fewer reports per {noun} than "
                                                "the whole test. A low number keeps more stations; a higher one is steadier.")
                    step = pd.Timedelta(minutes=mult * block_min)
                    windows, cursor = [], t_start
                    while cursor < t_end:
                        windows.append((cursor, min(cursor + step, t_end)))
                        cursor += step
                    over_time = gain_by_window(spots_a, spots_b, locs, home, windows,
                                               "linear" if method_label == "Linear power" else "snr", win_min, offset,
                                               receiver=pick, floor=avg_floor, min_km=avg_min_km)
                    if over_time["n"].sum() == 0:
                        st.caption("No window has enough reports at that minimum. Lower it.")
                    else:
                        st.pyplot(gain_over_time_chart(over_time, names, pick), width="stretch")
                        st.dataframe(pd.DataFrame({
                            "Window (UTC)": [f"{s:%H:%M}\u2013{e:%H:%M}" for s, e in zip(over_time["start"], over_time["end"])],
                            f"{noun.capitalize()}s": over_time["n"],
                            "Average gain B \u2212 A (dB)": over_time["mean"].round(1),
                            "95% range": [f"{lo:+.1f} to {hi:+.1f}" if not np.isnan(lo) else "" for lo, hi in
                                          zip(over_time["lo"], over_time["hi"])],
                            f"{pick} (dB)": over_time["one"].round(1),
                            f"{pick} reports (A / B)": [f"{a} / {b}" for a, b in over_time["one_n"]]}),
                            width="stretch", hide_index=True)
                        st.caption("The bars are the average relative gain over every station that heard both antennas in that "
                                   "window, with the 95% range across stations; the line is the picked "
                                   f"{noun}. If the bars and line move up or down during the test, a single average over the "
                                   "whole test hides it. A band opening, for example Europe late in the day, shows up here.")
        else:
            st.info(f"No {noun} heard both sides, so there is nothing to compare receiver by receiver.")

    with tab_method:
        _linear_average_view(map_a, map_b, an, names, noun, locs, home, kind, callsign, paired=(spots_a, spots_b),
                             floor=avg_floor, min_km=avg_min_km, default_min=DALE_MIN_SPOTS if sep.get("blocks") else 1)

    with tab_maps:
        st.caption("Both maps use the same view, the same SNR scale and the same dot sizes.")
        both = pd.concat([map_a, map_b])
        col_a, col_b = st.columns(2)
        for col, side, group in ((col_a, "A", map_a), (col_b, "B", map_b)):
            html = build_map(group, locs, home, label, callsign, show_all, tiles, units,
                             compute_stats(group, locs, home)["farthest"],
                             title=f"{callsign} · {names[side]}", fit_spots=both, scale=scale, noun=noun
                             ).get_root().render()
            icon = "\U0001F535" if side == "A" else "\U0001F7E0"
            slug = re.sub(r"[^A-Za-z0-9]+", "_", names[side]).strip("_")
            with col:
                st.markdown(f"#### {icon} {names[side]}")
                st.iframe(html, height=620)
                st.download_button(f"⬇️ Download map {side}", html,
                                   f"RBN_map_{callsign}_{file_date}_{slug}.html", "text/html", key=f"dl_{side}")


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


RBN_SOURCES = ["Download by date", "Paste from RBN site"]
WSPR_SOURCE = "WSPR"
# wspr.live fills a slot in over several minutes: about 1 receiver at 2 minutes old, half by 3-4, complete by 6-8 (measured).
# A transmission younger than this is left out of a comparison, since a half-filled slot makes its antenna look worse.
GHOST_MAX, GHOST_MIN_TYPICAL = 2, 16  # see _without_ghost_cycles
SIMULTANEOUS_MINUTES = 10  # A and B sent within this many minutes of each other count as the same conditions
DATA_LAG_MINUTES = 5  # measured: reports for a slot are complete ~3 min after it ends, ~5 min after it starts


def load_wspr(tx_call, start, end):
    """WSPR spots from wspr.live. Not cached: it only runs when you click Load spots, and then you want what is there
    now, including a test you have only just sent."""
    return fetch_wspr_spots(tx_call, start, end)


@st.cache_data(show_spinner=False, ttl=60)
def load_listening(band, start, end):
    """Which receivers were decoding on `band` in each slot. Remembered for a minute because the screen redraws often."""
    return fetch_listening(band, start, end)


def _scorecard_display(card, noun, units, multi_day):
    """The band scorecard as the tab and the PDF show it."""
    fmt = "%d %b %H:%M" if multi_day else "%H:%M"
    shown = pd.DataFrame({
        "Spots": card["Spots"], f"{noun.capitalize()}s": card["Receivers"],
        "Heard (UTC)": [f"{a:{fmt}} to {b:{fmt}}" for a, b in zip(card["Heard from"], card["Heard until"])],
        "Minutes with spots": card["Minutes with spots"],
        "Median SNR": card["Median SNR"].round(1), "Best SNR": card["Best SNR"].round(0).astype(int),
    })
    far_col = f"Farthest ({units})"
    shown[far_col] = card[far_col].round(0)
    shown["Farthest " + noun] = card["Farthest receiver"]
    for col in card.columns:
        if col.startswith("Receivers beyond"):
            shown[col.replace("Receivers", f"{noun.capitalize()}s")] = card[col]
    return shown


def _session_report_view(spots, locs, home, units, noun, band_choice):
    """The Session report tab: what the loaded spots say about each band you used, and when."""
    if band_choice != "All":
        st.caption(f"This tab shows every band you loaded; the Band filter ({band_choice}) only applies to the map tab.")
    for line in sr.headline(spots, locs, home, units, noun):
        st.markdown(f"- {line}")

    st.subheader("When you were heard, band by band")
    days = sorted(spots["time"].dt.date.unique())
    day = None
    if len(days) > 1:
        day = st.selectbox("Day", days, format_func=lambda d: f"{d:%a %d %b %Y}")
    st.pyplot(sr.timeline_figure(spots, BAND_COLORS, day), width="stretch")
    st.caption("Each bar is a stretch of time you were being heard (a pause of up to 30 minutes stays inside one bar) and each "
               "white tick is one spot. A spot only exists when someone heard you, so a gap can mean you were off the air, "
               f"calling to nobody, or the band was closed to you. The number after each band is its spots.")

    st.subheader("When each band worked")
    label = st.radio("Show", list(sr.GRID_METRICS), horizontal=True, key="sr_grid_metric")
    st.pyplot(sr.hour_grid_figure(spots, locs, home, sr.GRID_METRICS[label], units), width="content")
    st.caption("Each square is one band in one hour (UTC)"
               + (", with all the days you loaded combined" if len(days) > 1 else "")
               + ". " + {"spots": "The number is how many spots you got.",
                         "farthest": f"The number is the farthest {noun} that heard you, in {units} ('k' means thousands).",
                         "snr": "The number is the median SNR of your spots in that hour."}[sr.GRID_METRICS[label]]
               + " Empty means nobody heard you on that band that hour.")

    st.subheader("Band scorecard")
    card = sr.band_scorecard(spots, locs, home, units)
    st.dataframe(_scorecard_display(card, noun, units, multi_day=len(days) > 1), width="stretch")
    thin = [b for b in card.index if card.at[b, "Spots"] < sr.THIN]
    st.caption("Minutes with spots counts the 10-minute blocks that had at least one spot, so it is time you were heard, not "
               "time on the air."
               + (f" ⚠️ {', '.join(thin)} had fewer than {sr.THIN} spots, so treat its numbers as a hint." if thin else ""))

    st.subheader("Where each band reached")
    regions = sr.region_table(spots, locs, home)
    cols = [c for c in regions.columns if c != "Total"]
    view = regions.astype(float).replace(0, np.nan)
    try:
        styled = view.style.format("{:.0f}", na_rep="").background_gradient(
            cmap="Blues", axis=None, subset=pd.IndexSlice[view.index[1:], cols])
        st.dataframe(styled, width="stretch")
    except Exception:  # styling is a nicety; the numbers matter
        st.dataframe(view.fillna(0).astype(int), width="stretch")
    st.caption(f"The number of different {noun}s that heard you on each band, grouped by where they are. The first row counts "
               f"each {noun} once across all bands. US regions come from the {noun}'s position, the rest from its callsign.")
    with st.expander("Countries heard"):
        st.dataframe(sr.country_table(spots), width="stretch", hide_index=True)


def spots_page():
    st.title("📡 RBN Signal Mapper")
    st.markdown('<p class="subtitle">Map the Reverse Beacon Network and WSPR stations that heard you, see how strong '
                'your signal was, and compare antennas.</p>', unsafe_allow_html=True)

    skimmers, refresh_msg = skimmer_data()

    ss = st.session_state
    for key in ("raw", "home", "callsign", "file_date", "kind", "locs", "tx_call", "notice", "loaded_at"):
        ss.setdefault(key, None)
    # Read saved settings once per session. Re-reading them on every rerun changes each widget's
    # default, which Streamlit treats as a brand-new widget and resets, swallowing the first click.
    if "cfg" not in ss:
        ss.cfg = load_settings()
        if ss.cfg.get("source") == "WSPR (wspr.live)":  # the name an older version saved
            ss.cfg["source"] = WSPR_SOURCE
    cfg = ss.cfg

    def pick(options, key, default=None):
        """Index of the saved choice in `options` (falls back to the first/default)."""
        value = cfg.get(key, default if default is not None else options[0])
        return options.index(value) if value in options else 0

    with st.sidebar:
        st.header("Your signal")
        ss.setdefault("callsign_in", cfg.get("callsign", ""))
        callsign = st.text_input(
            "Callsign", key="callsign_in", placeholder="Enter your callsign",
            on_change=lambda: ss.update(callsign_in=ss.callsign_in.strip().upper())).strip().upper()
        grid_override = st.text_input(
            "Grid square (optional)", value=cfg.get("grid", ""), placeholder="Looked up from your callsign",
            help="Leave blank to use your callsign's registered address. "
                 "Enter a grid if you were portable or operating from elsewhere, or if the lookup fails.")

        sources = RBN_SOURCES + [WSPR_SOURCE]
        source = st.radio("Where are the spots from?", sources, index=pick(sources, "source"),
                          help="RBN: stations that decoded your CW/RTTY CQ. WSPR: stations that decoded your WSPR "
                               "beacon, handy for antenna tests because the power is fixed and known.")
        wspr_start, wspr_end = None, None
        pasted, days, span = "", [], cfg.get("span")
        if source in ("Download by date", WSPR_SOURCE):
            # RBN publishes a day's file once the UTC day is over; WSPR spots are there up to a few minutes ago
            last_day = (datetime.now(timezone.utc) - timedelta(days=0 if source == WSPR_SOURCE else 1)).date()
            span = st.radio("Period", ["Single day", "Date range"], horizontal=True,
                            index=pick(["Single day", "Date range"], "span"))
            if span == "Single day":
                days = [st.date_input("Date (UTC)", value=last_day, max_value=last_day)]
            else:
                first = st.date_input("From (UTC)", value=last_day - timedelta(days=MAX_DAYS - 1), max_value=last_day)
                last = st.date_input("To (UTC)", value=last_day, max_value=last_day,
                                     help=f"Up to {MAX_DAYS} days." + (" Each day is a separate download."
                                                                      if source != WSPR_SOURCE else ""))
                days = [first + timedelta(n) for n in range((last - first).days + 1)] if last >= first else []
                if last < first:
                    st.caption("⚠️ 'To' must be on or after 'From'.")
                elif len(days) > MAX_DAYS:
                    st.caption(f"⚠️ That's {len(days)} days; the limit is {MAX_DAYS}.")
            if source == WSPR_SOURCE:
                st.caption("Uses your callsign above as the WSPR transmitter. Reports take about 5 minutes to arrive "
                           "in full, so for a test you have just run, wait a few minutes before loading.")
                if days:
                    wspr_start = datetime.combine(days[0], time(0, 0))
                    wspr_end = min(datetime.combine(days[-1], time(23, 59, 59)),
                                   datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0))
        elif source == "Paste from RBN site":
            pasted = st.text_area("Paste spot rows here", height=150)

        load = st.button("Load spots", type="primary", width="stretch")

        # What the screen shows follows the data that is loaded, not the radio button, until you load again.
        kind = ss.kind if ss.raw is not None else ("WSPR" if source == WSPR_SOURCE else "RBN")

        st.divider()
        st.header("Compare mode")
        compare = st.checkbox("Compare two antennas or tests", value=False, key=f"compare_{kind}",
                              help="Splits your spots into an A side and a B side, for example two antennas, "
                                   "two power levels or two test transmissions, and says which is getting out better.")
        if kind == "WSPR":
            split, gap_khz = SPLIT_LOG, 0.5  # WSPR needs no choices here: you mark the antenna on each transmission
        else:
            split = SPLIT_FREQ  # RBN spots arrive over a minute or more after you send, so only frequency separates A and B well
            if compare:
                st.caption("RBN tells A and B apart by frequency: call CQ on one frequency with the first antenna, then on "
                           "a nearby frequency with the other, back to back (a minute or two apart).")
            gap_khz = 0.5  # skimmers report slightly different frequencies for one signal, so spots this close count as one

        st.divider()
        st.header("Filters")
        bands = ["All"] + list(BAND_COLORS)
        ss.setdefault("band_choice", bands[pick(bands, "band")])
        band_choice = st.selectbox("Band", bands, key="band_choice")
        lo_t, hi_t = time(0, 0), time(23, 59)
        if kind == "WSPR" and ss.raw is not None and not ss.raw.empty:
            # The slider covers just the span of the spots loaded rather than all day, and starts out showing all of it.
            tod = ss.raw["time"].dt.floor("min").dt.time
            if tod.min() < tod.max():
                lo_t, hi_t = tod.min(), tod.max()
        if kind == "WSPR" and compare:
            start_t, end_t = time.min, time.max  # comparing: you pick your test's cycles on the page instead
        else:
            start_t, end_t = st.slider(
                "UTC time window", min_value=lo_t, max_value=hi_t, value=(lo_t, hi_t), step=timedelta(minutes=1),
                format="HH:mm",
                help=("Only show spots between these times. It follows the spots you loaded."
                      if kind == "WSPR" else None))
        min_snr = -100 if kind == "WSPR" else st.slider("Minimum SNR (dB)", 0, 40, cfg.get("min_snr", 0))

        st.divider()
        st.header("Map")
        styles = list(TILE_STYLES)
        tiles = st.selectbox("Style", styles, index=pick(styles, "tiles"))
        show_all = st.checkbox("Show all skimmers", value=cfg.get("show_all", False) and kind == "RBN",
                               disabled=kind == "WSPR",
                               help="Adds a small grey dot for every RBN skimmer, including ones that didn't hear you. "
                                    "(Not available for WSPR, which has no list of every receiver.)")
        unit_opts = ["mi", "km"]
        units = st.radio("Distance units", unit_opts, index=pick(unit_opts, "units"), horizontal=True)

        st.caption(f"🛰️ {refresh_msg}")

    save_settings({"callsign": callsign, "grid": grid_override, "source": source, "span": span, "band": band_choice,
                   "min_snr": cfg.get("min_snr", 0) if kind == "WSPR" else min_snr,
                   "min_snr_wspr": cfg.get("min_snr_wspr", -30),
                   "tiles": tiles, "show_all": show_all if kind == "RBN" else cfg.get("show_all", False),
                   "units": units})

    if load:
        try:
            if not callsign:
                raise RuntimeError("Enter a callsign first.")
            if len(days) > MAX_DAYS:
                raise RuntimeError(f"Please pick {MAX_DAYS} days or fewer.")
            ss.home = resolve_home(callsign, grid_override, skimmers)
            locs, tx_call = None, None
            ss.notice = None
            notice = None
            if source == WSPR_SOURCE:
                if not days:
                    raise RuntimeError("The 'To' date must be on or after the 'From' date.")
                tx_call = clean_callsign(callsign)
                with st.spinner(f"Looking up {tx_call}'s WSPR spots…"):
                    df, truncated = load_wspr(tx_call, wspr_start, wspr_end)
                if truncated:
                    notice = "That much hit the spot limit, so the oldest spots are missing. Pick fewer days."
                locs = receiver_locations(df) if not df.empty else {}
                ss.file_date = f"{days[0]:%Y%m%d}" + (f"-{days[-1]:%Y%m%d}" if len(days) > 1 else "")
            elif source == "Paste from RBN site":
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
            ss.kind, ss.locs, ss.tx_call = ("WSPR" if source == WSPR_SOURCE else "RBN"), locs, tx_call
            ss.notice = notice
            ss.loaded_at = datetime.now(timezone.utc)
            if source == WSPR_SOURCE:
                st.rerun()  # redraw the sidebar so the time window follows the spots just loaded
        except Exception as e:
            ss.raw = None
            st.error(str(e))
    if ss.raw is None:
        st.info("👈 Enter your callsign and click **Load spots**. "
                "The map is centered on your callsign's registered location unless you enter a grid square.")
        return
    wspr = ss.kind == "WSPR"
    if ss.raw.empty:
        if wspr:
            st.warning(f"There are no WSPR spots from {ss.tx_call} in that time range. The callsign has to match what "
                       "your transmitter sends, and spots can take a few minutes to show up. Try a wider range.")
        else:
            st.warning(f"RBN has no spots of {ss.callsign} for that date. Check the callsign, or try another day.")
        return

    locs = ss.locs if wspr else skimmers
    scale = WSPR_SNR_SCALE if wspr else RBN_SNR_SCALE
    noun = "receiver" if wspr else "skimmer"
    spots = ss.raw
    spots = spots[(spots["time"].dt.time >= start_t) & (spots["time"].dt.time <= end_t) & (spots["snr"] >= min_snr)]
    all_bands = spots
    if band_choice != "All":
        spots = spots[spots["band"] == band_choice]
    if spots.empty:
        st.warning("No spots match the current filters.")
        return

    lat, lon, label = ss.home
    home = (lat, lon)
    who = ss.callsign
    if ss.notice:
        st.warning(ss.notice)
    if wspr and ss.loaded_at is not None:
        st.caption(f"🕒 Loaded at {ss.loaded_at:%H:%M} UTC. The newest transmission in it is at "
                   f"{ss.raw['time'].max():%H:%M} UTC. Sent something since? Click **Load spots** again.")
    if compare:
        st.caption(f"📍 {who} · {label}")
        compare_view(spots, locs, home, label, ss.callsign, ss.file_date, tiles, show_all and not wspr, units,
                     split, gap_khz, scale, noun, ss.kind, ss.raw)
        return
    tab_map, tab_report = st.tabs(["\U0001F5FA\ufe0f Map & direction", "\U0001F4C5 Session report"])
    with tab_map:
        stats = compute_stats(spots, locs, home)
        missing = {s for s in spots["spotter"].unique() if skimmer_location(s, locs) is None}

        k = KM_PER_MILE if units == "mi" else 1
        c = st.columns(5)
        c[0].metric("Spots", f"{stats['spots']:,}")
        c[1].metric(f"{noun.capitalize()}s", f"{stats['skimmers']:,}")
        c[2].metric(f"Farthest ({units})", f"{stats['max_km'] / k:,.0f}", help=stats["farthest"])
        c[3].metric("Best SNR", f"{stats['max_snr']:.0f} dB")
        c[4].metric("Average SNR", f"{stats['avg_snr']:.1f} dB")
        st.caption(f"📍 {who} · {label}"
                   + (f" · ⚠️ No location for {', '.join(sorted(missing)[:5])}"
                      f"{f' and {len(missing) - 5} more' if len(missing) > 5 else ''}"
                      f" (not in RBN's skimmer list), so not shown on the map" if missing else ""))

        m = build_map(spots, locs, home, label, ss.callsign, show_all and not wspr, tiles, units, stats["farthest"],
                      scale=scale, noun=noun)
        map_html = m.get_root().render()
        st.iframe(map_html, height=720)

        # The PDF is built when the button is clicked, outside this script run, where st.session_state is not available,
        # so everything it needs is read here and captured.
        pdf_call, pdf_date, pdf_kind, pdf_all_bands = ss.callsign, ss.file_date, ss.kind, all_bands
        left, middle, right = st.columns([1, 1, 3])
        left.download_button("⬇️ Download map", map_html, f"RBN_map_{ss.callsign}_{ss.file_date}.html",
                             "text/html", width="stretch")
        middle.download_button("\u2B07\ufe0f Download report (PDF)", width="stretch", mime="application/pdf",
                               data=lambda: single_report_pdf(spots, locs, home, label, pdf_call, pdf_date, pdf_kind, noun,
                                                              units, scale, pdf_all_bands),
                               file_name=f"RBN_report_{pdf_call}_{pdf_date}.pdf")
        st.subheader("Direction of your spots")
        chart_col, text_col = st.columns([2, 3])
        result = bearing_chart(spots, locs, home, scale)
        if result:
            fig, summary = result
            chart_col.pyplot(fig, width="stretch")
            text_col.markdown(summary)
        else:
            chart_col.caption(f"No located {noun}s to chart.")

        st.subheader("World view from your station")
        _azimuth_single_section(spots, locs, home, who, noun, units, scale)

        with st.expander("Spot table"):
            st.dataframe(spots.sort_values("time").assign(time=lambda d: d["time"].dt.strftime("%d %b %H:%M")),
                         width="stretch", hide_index=True)
    with tab_report:
        _session_report_view(all_bands, locs, home, units, noun, band_choice)

# ------------------------------------------------------------------------------------------- band conditions

_LOCATE_JS = """
export default function (component) {
  const { parentElement, setTriggerValue } = component
  const button = parentElement.querySelector("#locate")
  const note = parentElement.querySelector("#note")
  if (!button || !note) return
  button.onclick = () => {
    if (!navigator.geolocation) {
      note.textContent = "This browser can't share a location."
      setTriggerValue("error", "unsupported")
      return
    }
    note.textContent = "Asking your browser\u2026"
    navigator.geolocation.getCurrentPosition(
      (p) => {
        note.textContent = ""
        setTriggerValue("position", { lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy })
      },
      (e) => {
        note.textContent = e.code === 1 ? "Location was blocked. Allow it for this site, or use a callsign or grid."
                                        : "Couldn't get a location (" + e.message + ")."
        setTriggerValue("error", e.code)
      },
      { enableHighAccuracy: false, timeout: 20000, maximumAge: 300000 }
    )
  }
}
"""
_LOCATE_CSS = """
#locate { width: 100%; padding: 0.45rem 0.75rem; border-radius: 0.5rem; cursor: pointer; font: inherit;
          color: var(--st-text-color); background: var(--st-secondary-background-color);
          border: 1px solid var(--st-border-color); }
#locate:hover { border-color: var(--st-primary-color); color: var(--st-primary-color); }
#note { margin-top: 0.35rem; font-size: 0.85rem; color: var(--st-text-color); opacity: 0.75; }
"""
_LOCATE = st.components.v2.component(
    "use_my_location", html='<button id="locate" type="button">\U0001F4CD Use my location</button><div id="note"></div>',
    css=_LOCATE_CSS, js=_LOCATE_JS)

_STATUS = {"Open": "\U0001F7E2 Open", "Marginal": "\U0001F7E1 Marginal", "Quiet": "\u26AA Quiet"}
_TREND = {"opening": "\u2197 opening", "fading": "\u2198 fading", "steady": "\u2192 steady", "new": "\u2197 just started", "unclear": "– too few to tell"}


@st.cache_data(ttl=300, show_spinner=False)
def _nearby_spots(lat, lon, radius_km, end_iso, minutes):
    """Spots from the WSPR stations near a point, cached for five minutes so a change of mode or power costs nothing."""
    end = pd.Timestamp(end_iso).to_pydatetime()
    return cond.fetch_nearby(lat, lon, radius_km, end - timedelta(minutes=minutes), end)


@st.cache_data(ttl=300, show_spinner=False)
def _station_spots(call, end_iso, minutes):
    """Spots from one WSPR transmitter, cached for five minutes."""
    end = pd.Timestamp(end_iso).to_pydatetime()
    return cond.fetch_station(call, end - timedelta(minutes=minutes), end)


def _dx_badge(result, units, k):
    """A short label and tone for the top card: how many receivers outside North America heard the stations near you."""
    n = result["receivers"][result["receivers"]["region"].isin(cond.DX_REGIONS)]["rx"].nunique()
    if n < 5:
        return ("Thin DX evidence", "ta", f"Only {n} receiver{'s' if n != 1 else ''} outside the US and Canada heard these WSPR "
                "signals, so the DX figures rest on very little data. A wider area (up to about 900 miles) or a longer look-back "
                "can add some.")
    return ("DX evidence good", "tg", f"{n} receivers outside the US and Canada heard these WSPR signals.")


def _conditions_body(result, power, mode, margin_db, units, k, lat, lon, who, window=60, area=""):
    """The dashboard: the summary, a tile for each band, the sun and MUF, the regions, the map and the highlights,
    with the detailed tables in tabs underneath."""
    lines = cond.headline(result, power, mode, margin_db, units, window)
    bands, dx = result["bands"], result["dx"]
    data, est = _space_weather(), iono.estimate(lat, lon, _ionosondes())
    badge, badge_tone, badge_help = _dx_badge(result, units, k)
    setup = f"{power:g} W {mode} from {cond.latlon_to_maidenhead(lat, lon)}"
    if len(dx):
        far = dx.iloc[0]
        setup += f" \u00b7 farthest: {far['rx']}, {far['km'] / k:,.0f} {units} on {far['band']}"
    st.html(dash.CSS)
    st.html(dash.top_html(setup, f"{result['n_spots']:,} WSPR spots from {result['n_tx']} stations{area}, last {window} minutes",
                          badge, badge_tone, badge_help))
    st.html(dash.tiles_html(bands, result["series"]))
    st.html(dash.strip_html(sw.readings(data), est))

    left, right = st.columns([1.5, 1])
    with left.container(border=True):
        st.html(dash.grid_html(result["chance"], result["regions_n"]))
    with right.container(border=True):
        st.html(dash.highlights_html(cond.short_highlights(result, power, mode, margin_db, units, window)))

    good = result["receivers"][result["receivers"]["margin"] >= 0]
    with st.container(border=True):
        st.html('<div class="cd"><p class="cd-h">Map of receivers that would hear you</p>'
                f'<p class="cd-sub">{len(good)} receiver and band pairs with a good signal, with the number for each band beside it. Pick a band or two to show only those, or none to show all.</p></div>')
        if len(good):
            band_order = [b for b in dash.MAP_COLORS if b in set(good["band"])] + sorted(set(good["band"]) - set(dash.MAP_COLORS))
            per_band = good.groupby("band").size()
            st.html(dash.band_buttons_css("cond_map_bands", band_order))
            pick_col, view_col = st.columns([3, 1])
            picked = pick_col.pills("Bands", band_order, selection_mode="multi", key="cond_map_bands",
                                    format_func=lambda b: f"{b}  {per_band[b]}", label_visibility="collapsed") or band_order
            views = _views(units)
            view_choice = view_col.selectbox("How much of the world", list(views), index=1, key=f"cond_map_view_{units}",
                                             label_visibility="collapsed")
            night = st.checkbox("Day and night line", key="cond_map_night",
                                help="Shades the night side and draws the line between day and night (the gray line), "
                                     "which is where the low bands tend to do best.")
            shown = good[good["band"].isin(picked)]
            points = pd.DataFrame({"km": shown["km"].to_numpy(), "bearing": shown["bearing"].to_numpy(), "group": "single",
                                   "gap": float("nan"), "snr_a": shown["margin"].to_numpy(), "snr_b": float("nan"),
                                   "color": [dash.MAP_COLORS.get(b, "#8a8f98") for b in shown["band"]]})
            _, map_col, _ = st.columns([1, 4, 1])
            map_col.pyplot(azimuth_map_figure(lat, lon, points, {"A": "", "B": ""}, who, units, views[view_choice], "dots", True,
                                              (0, 20), when=datetime.now(timezone.utc).replace(tzinfo=None) if night else None),
                           width="stretch")
            beyond = int((points["km"] > views[view_choice]).sum())
            if beyond:
                map_col.caption(f"{beyond} receiver{'s' if beyond != 1 else ''} beyond this view.")
        else:
            st.caption("No receiver would hear you at this power and mode right now.")
    st.caption("Estimated from WSPR reports, not measured: good for comparing bands and directions, but the percentages run high "
               "for being spotted by a skimmer. The tabs below have the numbers behind this and how far to trust them.")

    t_band, t_margin, t_sun, t_far, t_how = st.tabs(["Band by band", "Margin and power needed", "Sun and MUF", "Farthest receivers",
                                                      "How this works"])
    with t_band:
        bands = result["bands"]
        summary = pd.DataFrame({
            "Status": [_STATUS[s] for s in bands["Status"]], "Trend": [_TREND[t] for t in bands["Trend"]],
            "Receivers hearing it": bands["Receivers"], "Good": bands["Good"], "Marginal": bands["Marginal"],
            f"Farthest good ({units})": [f"{km / k:,.0f}" if pd.notna(km) else "" for km in bands["Farthest good (km)"]],
            "Farthest receiver": [f"{c} ({r})" if c else "" for c, r in zip(bands["Farthest good receiver"],
                                                                             bands["Farthest good region"])],
        }, index=bands.index)
        st.dataframe(summary, width="stretch")
        st.caption("Good means the middle of that receiver's reports, restated for your power, beats the mode's threshold by your "
                   "safety margin. Marginal is above the threshold but by less than the margin. Trend compares the second half "
                   "of the look-back with the first.")

    with t_margin:
        st.html(dash.detail_grid_html(result["regions"], result["power_needed"], result["regions_n"], margin_db))
        st.caption(f"The top figure is dB above the {mode} threshold at a typical receiver in each region (the 75th percentile, so "
                   f"not the best one). Under it is the power that receiver would need to hear you with your {margin_db} dB margin; "
                   f"green means your {power:g} W is enough. A grey cell has only 1 or 2 receivers, so it has no figure: see "
                   "How this works.")

    with t_sun:
        if sw.interpret(data):
            _space_weather_card(boxed=False)
        if est:
            _muf_card(lat, lon, boxed=False)
    with t_far:
        dx = result["dx"]
        if dx.empty:
            st.caption("None at this power and mode right now.")
        else:
            st.dataframe(pd.DataFrame({
                "Band": dx["band"], "Receiver": dx["rx"], "Region": dx["region"], f"Distance ({units})": (dx["km"] / k).round(0).astype(int),
                "Margin (dB)": dx["margin"].round(0).astype(int)}), width="stretch", hide_index=True)

    with t_how:
        st.markdown(
            "Every WSPR report says: this station, at this power, was heard at this SNR. SNR moves dB for dB with power, so the "
            "report can be restated for your power: run 10 dB more and you would be 10 dB stronger there. Each mode needs a "
            "minimum SNR, and the gap between the two is the margin.\n\n"
            f"- **It is a guide, good to about {cond.SIGMA_DB:g} dB.** Checked against RBN readings from two stations' activations, "
            "predictions were off by about 6 dB within 1,000 miles and about 9 dB at 1,000 to 2,000 miles. I had no DX paths in "
            "that check, so DX is the least tested part. The chance figures allow for that error.\n"
            "- **It only counts receivers that heard the nearby WSPR stations.** With more power than they used, you would be "
            "heard in more places than shown.\n"
            "- **A region with only 1 or 2 receivers is a hint, not a result, and it flatters you.** Only receivers that heard "
            "something are counted, so when just one receiver in a region heard the stations near you, it is the lucky exception, "
            "and the others there heard nothing. Its figures look easy, but a contact there is usually hard. In a week of checks, "
            "such regions were really spotted only about 4 times in 10, so they show only a receiver count.\n"
            "- **The percentages are estimates, not measured success rates.** They are good for comparing bands and directions, "
            "but run high for being spotted by a skimmer. In a week of one station's CW activations, of the skimmers the figures "
            "said could hear him, about 1 in 10 spotted him below the usual RBN spotting level (about 2 dB above the CW figure), 4 in 10 at a margin of 0 to 5 dB over it, 5 in 10 at "
            "5 to 10 dB and about 7 in 10 at 15 dB or more.\n"
            "- **WSPR receivers are often quiet sites.** A station you hope to work may have more noise, so keep a margin.\n"
            "- **The thresholds are approximate**, and CW and voice depend on the operator's ear and the receiver.\n"
            "- **It assumes your antenna matches theirs** unless you enter a difference.\n"
            "- **Reports lag by a few minutes**, so this shows the last half hour or so, not this second.")





@st.cache_data(ttl=900, show_spinner=False)
def _space_weather():
    """NOAA's current space weather, cached for 15 minutes."""
    return sw.fetch()


def _space_weather_card(boxed=True):
    """The sun and the magnetic field right now, as a few numbers and a short plain reading. Shows nothing if NOAA can't be reached."""
    data = _space_weather()
    lines = sw.interpret(data)
    if not lines:
        return
    items = [(label, value, help_) for label, value, help_ in (
        ("Solar flux", None if data["flux"] is None else f"{data['flux']:.0f}",
         "The 10.7 cm radio flux, measured daily at noon in Penticton. Higher means better HF: about 70 is poor, 120 good, 150 or more very good."),
        ("Sunspots", None if data["ssn"] is None else f"{data['ssn']:.0f}",
         "The sunspot number of the latest complete day. More spots usually means a more active sun and better HF."),
        ("Kp index", None if data["kp"] is None else f"{data['kp']:.1f}",
         "How disturbed Earth's magnetic field is, 0 to 9, updated every 3 hours. Under 3 is quiet, 5 or more is a storm."),
        ("A index", None if data["a_index"] is None else f"{data['a_index']:.0f}",
         "The day's average disturbance of the magnetic field at middle latitudes (yesterday's). Under 8 is quiet, 30 or more is a storm."),
        ("X-ray", sw.xray_class(data["xray"]),
         "The flare level from the GOES satellite. A and B are quiet, C is minor, M can cause a brief radio fade-out, X a strong one."),
        ("Wind (km/s)", None if data["wind"] is None else f"{data['wind']:.0f}",
         "The speed of the wind reaching Earth. Around 400 is calm, 500 or more is elevated, 600 or more is fast."),
        ("Bz (nT)", None if data["bz"] is None else f"{data['bz']:+.0f}",
         "The north-south direction of the solar wind's magnetic field. Negative (south) lets the wind stir Earth's field."))
        if value is not None]
    with st.container(border=True) if boxed else nullcontext():
        if boxed:
            st.markdown("**\u2600\ufe0f Space weather right now**")
        for start in range(0, len(items), 4):
            row = items[start:start + 4]
            for col, (label, value, help_) in zip(st.columns(4)[:len(row)], row):
                col.metric(label, value, help=help_)
        st.markdown("\n".join(f"- {line}" for line in lines))
        flux_t, kp_t = sw.when_text(data["times"].get("flux"), "%d %b"), sw.when_text(data["times"].get("kp"), "%H:%M")
        st.caption("From NOAA's Space Weather Prediction Center. Solar flux is the daily noon reading"
                   + (f" (latest {flux_t})" if flux_t else "") + ", Kp is for the 3-hour period"
                   + (f" starting {kp_t} UTC" if kp_t else "") + ", and the rest update every few minutes.")


@st.cache_data(ttl=900, show_spinner=False)
def _ionosondes():
    """The working ionosondes with a fresh reading, cached for 15 minutes."""
    return iono.fetch()


def _muf_card(lat, lon, boxed=True):
    """The MUF and foF2 near the user, estimated from the nearest working ionosondes. Shows nothing if none is close enough."""
    est = iono.estimate(lat, lon, _ionosondes())
    if not est:
        return
    with st.container(border=True) if boxed else nullcontext():
        st.markdown("**\U0001F4E1 MUF near you** (MHz)" if boxed else "MHz:")
        shown = [(f"{d:,} km", f"{est['muf'][d]:.0f}", f"The highest frequency expected to work over a {d:,} km path. "
                  "Shorter paths cannot use as high a frequency, because the signal meets the ionosphere at a steeper angle.")
                 for d in (3000, 2000, 1000, 500)]
        shown.append(("NVIS (foF2)", f"{est['fof2']:.1f}", "The critical frequency: the highest frequency that still bounces straight "
                      "back for local contacts. Above it, signals pass up and out and skip over nearby stations."))
        for col, (label, value, help_) in zip(st.columns(len(shown)), shown):
            col.metric(label, value, help=help_)
        st.markdown("\n".join(f"- {line}" for line in iono.interpret(est)))
        st.caption("Measured by ionosondes (ionospheric radars) collected by GIRO and republished by Andrew KC2G at prop.kc2g.com. "
                   "Only a few dozen report at any time, so this is an estimate for your location, not a measurement.")


def conditions_page():
    """Which bands are open and where a signal like yours would be heard, from the WSPR stations near you."""
    st.header("\U0001F310 Band conditions now")
    st.markdown('<p class="subtitle">Which bands are open and where you would be heard, worked out from the WSPR stations near '
                'you in the last half hour, restated for your power and mode.</p>', unsafe_allow_html=True)
    skimmers, _ = skimmer_data()
    saved = st.session_state.get("cfg") or load_settings()
    with st.sidebar:
        st.header("Where")
        where = st.radio("Find me by", ["Callsign or grid", "My location"], horizontal=True, key="cond_where")
        callsign, grid = "", ""
        if where == "Callsign or grid":
            callsign = st.text_input("Callsign", value=st.session_state.get("callsign_in", saved.get("callsign", "")),
                                     key="cond_call").strip().upper()
            grid = st.text_input("Grid square (optional)", value=saved.get("grid", ""), key="cond_grid",
                                 placeholder="Looked up from your callsign",
                                 help="Enter the grid you are activating from, such as a park, to see conditions there.")
        else:
            fix = _LOCATE(key="cond_locate", on_position_change=lambda: None, on_error_change=lambda: None)
            if fix.position:
                st.session_state["cond_geo"] = (float(fix.position["lat"]), float(fix.position["lon"]))
            geo = st.session_state.get("cond_geo")
            if geo:
                st.caption(f"\U0001F4CD {geo[0]:.3f}, {geo[1]:.3f} \u00b7 grid {cond.latlon_to_maidenhead(*geo)}")
            st.caption("Your browser asks before sharing. The position is used only to find WSPR stations near you and is not "
                       "saved; only a box around it goes to wspr.live.")
        source = st.radio("Learn from", ["Stations near me", "One transmitter"], horizontal=True, key="cond_source",
                          help="Stations near me uses every WSPR transmitter inside the area below. One transmitter uses just "
                               "your own WSPR transmitter, or a friend's, and treats it as you: its spots, at its own power, "
                               "show where a station at that spot is being heard. It is the closest match to your own antenna. A single transmitter "
                               "sends each band only now and then, so a look-back of an hour or two gives it more to go on.")
        ref_call = ""
        if source == "One transmitter":
            ref_call = st.text_input("Transmitter callsign", value=st.session_state.get("callsign_in", saved.get("callsign", "")),
                                     key="cond_ref_call", help="The callsign it sends in WSPR.").strip().upper()
        units = st.radio("Distance units", ["mi", "km"], index=["mi", "km"].index(saved.get("units", "mi")), horizontal=True,
                         key="cond_units")
        if source == "One transmitter":
            radius_choice = 500.0  # not used: the transmitter's own spots are the data
        elif units == "mi":
            radius_choice = st.slider("Area around you (miles)", 60, 930, 300, 25, key="cond_radius_mi",
                                      help="WSPR stations inside this distance count as being near you. A wider area gives "
                                           "more data where few stations transmit.") * KM_PER_MILE
        else:
            radius_choice = float(st.slider("Area around you (km)", 100, 1500, 500, 50, key="cond_radius_km",
                                            help="WSPR stations inside this distance count as being near you. A wider area "
                                                 "gives more data where few stations transmit."))
        st.header("Your signal")
        power = st.number_input("Power (watts)", 0.1, 1500.0, 100.0, step=1.0, key="cond_power")
        mode = st.selectbox("Mode", list(cond.MODES), key="cond_mode",
                            help="Each mode needs a minimum signal-to-noise ratio. These are approximate, in a 2,500 Hz "
                                 + "bandwidth: " + ", ".join(f"{m} {v:+g} dB" for m, v in cond.MODES.items()) + ".")
        antenna_db = st.number_input("Your antenna compared with theirs (dB)", -20.0, 20.0, 0.0, step=1.0, key="cond_antenna",
                                     help="0 assumes your antenna is about as good as the WSPR stations'. Use a negative "
                                          "number for a weaker antenna (a compromise portable antenna, say). A relative-gain "
                                          "test on the My spots page can tell you the number.")
        margin_db = st.slider("Safety margin (dB)", 0, 10, 3, key="cond_margin",
                              help="Signals fade, so a path counts as good only when it beats the mode's threshold by this much.")
        window = st.radio("Look back", [30, 60, 120], index=0, horizontal=True, key="cond_window",
                          format_func=lambda m: f"{m} min" if m < 60 else f"{m // 60} h")
        if st.button("Refresh now", width="stretch", key="cond_refresh"):
            _nearby_spots.clear()
    k = KM_PER_MILE if units == "mi" else 1.0
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    end = now.replace(minute=now.minute - now.minute % 5)  # steady for five minutes so the cache can work
    if source == "One transmitter":
        try:
            if not ref_call:
                st.info("Enter the transmitter's callsign in the sidebar.")
                return
            with st.spinner(f"Asking wspr.live where {ref_call} was heard\u2026"):
                spots, cut = _station_spots(ref_call, end.isoformat(), int(window))
        except WsprError as exc:
            st.error(str(exc))
            return
        if spots.empty:
            st.warning(f"{ref_call} had no WSPR spots in the last {window} minutes. Check the callsign, or look back further.")
            return
        lat, lon = float(spots["tx_lat"].median()), float(spots["tx_lon"].median())
        home, label, radius, widened = (lat, lon), f"{ref_call}, grid {cond.latlon_to_maidenhead(lat, lon)}", 0.0, False
        st.caption(f"\U0001F4CD {label} \u00b7 spots up to {end:%H:%M} UTC (WSPR reports arrive a few minutes late)")
        if cut:
            st.warning("That was more data than the limit, so the oldest spots are missing. Try a shorter look-back.")
        result = cond.analyze(spots, home, power, mode, end, window, antenna_db, margin_db)
        if result["bands"].empty:
            st.warning("No receivers heard that transmitter in that time.")
            return
        st.caption(f"{ref_call}'s own antenna and location stand in for yours.")
        _conditions_body(result, power, mode, margin_db, units, k, lat, lon, ref_call, window)
        return
    try:
        if where == "My location":
            geo = st.session_state.get("cond_geo")
            if not geo:
                st.info("Click **Use my location** in the sidebar and allow your browser to share it. If it can't, switch to "
                        "**Callsign or grid**.")
                return
            lat, lon = geo
            label = f"your location, grid {cond.latlon_to_maidenhead(lat, lon)}"
        elif not (callsign or grid.strip()):
            st.info("Enter your callsign or a grid square in the sidebar, or choose **My location**.")
            return
        else:
            lat, lon, label = resolve_home(callsign, grid, skimmers)
    except (RuntimeError, ValueError) as exc:
        st.error(str(exc))
        return
    home = (lat, lon)
    radius, widened = radius_choice, False
    try:
        with st.spinner("Asking wspr.live which stations near you were heard\u2026"):
            while True:
                spots, cut = _nearby_spots(lat, lon, float(radius), end.isoformat(), int(window))
                if spots["tx"].nunique() >= 3 or radius >= 1490:
                    break
                radius, widened = min(1500.0, radius * 2), True
    except WsprError as exc:
        st.error(str(exc))
        return
    st.caption(f"\U0001F4CD {label} \u00b7 spots up to {end:%H:%M} UTC (WSPR reports arrive a few minutes late)")
    if spots.empty:
        st.warning(f"No WSPR station within {radius / k:,.0f} {units} of this spot was heard in the last {window} minutes, so there is "
                   "nothing to base conditions on. Widen the area, or try again in a few minutes.")
        return
    if widened:
        st.info(f"Only a few WSPR stations transmit near you, so I widened the area to {radius / k:,.0f} {units} to get enough data.")
    if cut:
        st.warning("That was more data than the limit, so the oldest spots are missing. Try a shorter look-back.")
    result = cond.analyze(spots, home, power, mode, end, window, antenna_db, margin_db)
    if result["bands"].empty:
        st.warning("No receivers heard the stations near you in that time.")
        return

    _conditions_body(result, power, mode, margin_db, units, k, lat, lon, callsign or "You", window,
                     area=f" within {radius / k:,.0f} {units}")

GITHUB_URL = "https://github.com/K5OHY/RBN_Map"


def help_page():
    """The README, shown in the app, with the links to the code and to Dale's site."""
    st.title("Help and README")
    st.markdown(f"This is the project's README, the same one that is on [GitHub]({GITHUB_URL}#readme), where the code lives "
                "and where you can report a problem or suggest something. The long-test method (relative gain at each "
                "receiver, linear averaging and long alternating blocks) follows the work of "
                "[Dale, N3HXZ](https://www.n3hxz.net/), published on his site. The quick paired test is this app's own.")
    path = Path(__file__).parent / "README.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        st.warning(f"The README file isn't here. It is on [GitHub]({GITHUB_URL}#readme).")
        return
    chunk = []

    def flush():
        if chunk:
            st.markdown("\n".join(chunk))
            chunk.clear()

    lines, i = text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("# ") or "online, nothing to install" in line:
            continue
        if line.startswith("## What it looks like"):  # the screenshots are for GitHub, not for the app itself
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            continue
        if line.strip() == "<details>":  # a folded section: the first line inside is its <summary>
            end = next((k for k in range(i, len(lines)) if lines[k].strip() == "</details>"), len(lines))
            inside = lines[i:end]
            i = end + 1
            flush()
            title = re.sub(r"<[^>]+>", "", inside[0]).strip() if inside else "More"
            with st.expander(title):
                st.markdown("\n".join(inside[1:]))
            continue
        chunk.append(line)
    flush()


def main():
    st.set_page_config(layout="wide", page_title="RBN Signal Mapper", page_icon="\U0001F4E1")
    st.markdown(CSS, unsafe_allow_html=True)
    pages = [st.Page(spots_page, title="My spots", icon="\U0001F4E1", url_path="spots", default=True),
             st.Page(conditions_page, title="Band conditions", icon="\U0001F310", url_path="conditions"),
             st.Page(help_page, title="Help and README", icon="\u2753", url_path="help")]
    st.navigation(pages).run()
    st.sidebar.divider()
    st.sidebar.caption(f"[README and code on GitHub]({GITHUB_URL}#readme) \u00b7 long-test method by [Dale, N3HXZ](https://www.n3hxz.net/)")


if __name__ == "__main__":
    main()
