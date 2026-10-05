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
    DIRECTION_EDGE_DB,
    SECTOR_NAMES,
    EQUAL_DB,
    SLOT_NOISE_DB,
    analyze,
    build_verdict,
    count_pairs,
    default_labels,
    db_from_watts,
    eligible_receivers,
    locate as skimmer_location,
    median_power_dbm,
    receiver_table,
    _farthest,
    _lead_text,
    round_consistency,
    split_by_labels,
)
from report_pdf import build_pdf
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


def sector_means(table):
    """Average per-skimmer SNR in each of the 8 compass directions (NaN where no skimmer heard you)."""
    if table.empty:
        return pd.Series(np.nan, index=range(8))
    sector = ((table["bearing"] + 22.5) % 360 // 45).astype(int)
    return table.groupby(sector)["snr"].mean().reindex(range(8))


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
    dist, col_a, col_b, col_d = f"Distance ({units})", "🔵 A SNR (dB)", "🟠 B SNR (dB)", "Stronger by (dB)"
    view = pd.DataFrame({
        dist: (shared["km_a"] / k).round(0).astype(int),
        col_a: shared["snr_a"], col_b: shared["snr_b"], col_d: shared["delta"].abs(),  # the size of the lead, never a sign
    }).sort_values(dist, ascending=False)
    view["Stronger"] = np.where(shared["delta"].reindex(view.index) > 0, "A",
                                np.where(shared["delta"].reindex(view.index) < 0, "B", "Tie"))
    view = view[[dist, col_a, col_b, "Stronger", col_d]]
    view.index.name = "Skimmer"

    def tint(row):
        style = [""] * len(row)
        for side, col, color in (("A", col_a, COLOR_A), ("B", col_b, COLOR_B)):
            if row["Stronger"] == side:
                style[view.columns.get_loc(col)] = f"background-color:{color}33;font-weight:600"
        return style
    return view.style.apply(tint, axis=1).format({col_a: "{:g}", col_b: "{:g}", col_d: "{:g}", dist: "{:,}"})


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


def paired_scatter(shared, names):
    """One dot per receiver that heard both: A's SNR across, B's up. Above the dashed line B was stronger."""
    fig = Figure(figsize=(4.4, 4.4))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot()
    _style_axes(ax)
    lo = float(min(shared["snr_a"].min(), shared["snr_b"].min())) - 2
    hi = float(max(shared["snr_a"].max(), shared["snr_b"].max())) + 2
    ax.plot([lo, hi], [lo, hi], color=COLOR_TIE, linestyle="--", linewidth=1)
    colors = np.where(shared["delta"] > 0, COLOR_A, np.where(shared["delta"] < 0, COLOR_B, COLOR_TIE))
    ax.scatter(shared["snr_a"], shared["snr_b"], c=colors, s=26, alpha=0.75, linewidths=0)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_aspect("equal")
    ax.set_xlabel(f"{names['A']} SNR (dB)", color=COLOR_A, fontsize=9)
    ax.set_ylabel(f"{names['B']} SNR (dB)", color=COLOR_B, fontsize=9)
    ax.text(0.04, 0.95, f"{names['B']} stronger", transform=ax.transAxes, color=COLOR_B, fontsize=8, va="top")
    ax.text(0.96, 0.05, f"{names['A']} stronger", transform=ax.transAxes, color=COLOR_A, fontsize=8, ha="right")
    return fig


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
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M"))
        fig.autofmt_xdate(rotation=0, ha="center")
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
              ("note", f"Each {noun} is counted once, however many times it spotted you.")]
    cols = ["", "A", "B", "Both", "Stronger by", "Better"]
    widths = [0.26, 0.09, 0.09, 0.09, 0.2, 0.27]
    if ctx["means_a"].notna().any() or ctx["means_b"].notna().any():
        blocks += [("heading", "Direction"), ("figure", direction_chart(ctx["means_a"], ctx["means_b"], names["A"], names["B"]), 3.5),
                   ("note", "Distance from the centre is the average SNR toward that direction."),
                   ("table", ["Direction"] + cols[1:], _breakdown_rows(an["direction"], names), widths)]
    if len(an["distance"]) and an["distance"][["a", "b"]].to_numpy().sum():
        blocks += [("heading", "Distance"), ("figure", distance_chart(an["distance"], names), 4.0),
                   ("table", ["Distance"] + cols[1:], _breakdown_rows(an["distance"], names), widths),
                   ("note", "Longer paths usually mean a lower take-off angle.")]
    if s_["n"]:
        every = an["shared_all"]
        k = KM_PER_MILE if units == "mi" else 1
        order = every.sort_values("km_a", ascending=False)
        rows = [[i, f"{row.km_a / k:,.0f}", f"{row.snr_a:g}", f"{row.snr_b:g}",
                 "A" if row.delta > 0 else "B" if row.delta < 0 else "Tie", f"{abs(row.delta):g}"]
                for i, row in zip(order.index, order.itertuples())]
        blocks += [("heading", f"{noun.capitalize()} by {noun}"), ("figure", paired_scatter(an["shared"], names), 3.8),
                   ("note", f"{len(every)} {noun}s heard both sides, shown farthest first (up to 40). Each SNR is the "
                            "median of that station's spots."),
                   ("table", [noun.capitalize(), f"Distance ({units})", "A SNR", "B SNR", "Stronger", "By (dB)"],
                    rows[:40], [0.28, 0.2, 0.13, 0.13, 0.13, 0.13])]
    if ctx["timeline"] is not None:
        blocks += [("heading", "Timeline"), ("figure", timeline_chart(*ctx["timeline"], names), 3.0)]
    meta = {"title": "Antenna comparison report", "callsign": ctx["callsign"], "subtitle": sub}
    return build_pdf(meta, blocks)


def single_report_pdf(spots, locs, home, label, callsign, file_date, kind, noun, units, scale):
    """One antenna / one set of spots as a PDF: key numbers, direction, by band, the stations heard."""
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
    result = bearing_chart(spots, locs, home, scale)
    if result:
        fig, summary = result
        blocks += [("heading", "Direction of your spots"), ("figure", fig, 4.4), ("paragraph", summary)]
    table = receiver_table(spots, locs, home)
    band_rows = []
    for band, g in spots.groupby("band"):
        t = receiver_table(g, locs, home)
        band_rows.append([band, f"{len(g):,}", f"{g['spotter'].nunique():,}",
                          f"{t['km'].max() / k:,.0f}" if len(t) else "–", f"{g['snr'].max():.0f}", f"{g['snr'].mean():.1f}"])
    blocks += [("heading", "By band"),
               ("table", ["Band", "Spots", f"{noun.capitalize()}s", f"Farthest ({units})", "Best SNR", "Average SNR"],
                band_rows, [0.15, 0.15, 0.18, 0.22, 0.15, 0.15])]
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
    freqs = ", ".join(f"{f:.1f}" for f, _ in groups)
    if len(groups) < 2:
        st.warning(f"Found only {len(groups)} frequency group ({freqs or 'none'} kHz). Compare mode needs spots on at "
                   f"least two frequencies. If your tests were close together, lower **Frequency gap** in the sidebar.")
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
    heard = spots.groupby("time")["spotter"].nunique()
    typical = heard[heard > GHOST_MAX].median()
    if pd.isna(typical) or typical < GHOST_MIN_TYPICAL:
        return spots
    ghosts = heard[heard <= GHOST_MAX].index
    if len(ghosts) == 0:
        return spots
    st.caption(f"Left out {len(ghosts)} cycle{'s' if len(ghosts) > 1 else ''} that {GHOST_MAX} or fewer receivers heard, "
               f"when typical cycles were heard by about {typical:.0f}. A cycle that thin says little about an antenna, and "
               "it is often one receiver with a wrong clock reporting your signal under the wrong time slot.")
    return spots[~spots["time"].isin(ghosts)]


def _separate_by_log(spots):
    """WSPR antenna test the way N4REE keeps it: each transmission is logged with the antenna that was connected. The
    user says how many cycles the test had and where it started (it opens on the latest cycles, which is where a test
    just run will be); the app lists those cycles with a first guess (the antenna alternating down the list), and the
    user corrects any row or clears it to leave it out."""
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
    top = st.columns([4, 2, 3])
    n_cycles = int(top[0].number_input(
        "Transmissions in your test", min_value=2, max_value=max(2, min(len(all_stamps), 80)), value=2, step=2,
        key="test_cycles",
        help="Count every 2-minute transmission, on either antenna: A then B is 2, A B B A is 4, A B B A twice is 8. "
             "More is steadier: 8 or more (4 pairs) is a solid test, 12 to 16 for differences under 1 dB. "
             "Use whole rounds (a multiple of 2, or of 4 for A B B A): an odd count leaves one transmission without a partner."))
    n_cycles = min(n_cycles, len(all_stamps))
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
    close_in_time = None
    if sequential and not sep.get("labeled"):
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
    tab_result, tab_where, tab_each, tab_maps = st.tabs(
        ["\U0001F4CA Result", "\U0001F9ED Direction & distance", "\U0001F4CB Receiver by receiver", "\U0001F5FA️ Maps"])

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
        pdf_ctx = dict(
            names=names, an=an, verdict=verdict, noun=noun, units=units, score_rows=score_rows, callsign=callsign, label=label,
            file_date=file_date, source="WSPR" if kind == "WSPR" else "Reverse Beacon Network",
            bands=", ".join(sorted(set(map_a["band"]) | set(map_b["band"]))),
            test=(f"Test: {n_a} transmission{'s' if n_a != 1 else ''} on A, {n_b} on B" if sep.get("labeled")
                  else "Test: A and B separated by " + ("frequency" if not sep.get("time_based") else "time")),
            time_based=bool(sep.get("time_based")), means_a=means_a, means_b=means_b,
            timeline=(map_a, map_b))
        st.download_button("\u2B07\ufe0f Download report (PDF)", data=lambda: compare_report_pdf(pdf_ctx),
                           file_name=f"RBN_compare_{callsign}_{file_date}.pdf", mime="application/pdf",
                           help="The verdict, scoreboard and every chart and table, as a PDF you can keep or share.")
        st.markdown(scoreboard_html(names["A"], names["B"], score_rows), unsafe_allow_html=True)

        st.markdown(f"##### Who heard you on each {'side' if sep.get('time_based') else 'frequency'}?")
        st.markdown(reach_bar_html(r["only_a"], r["both"], r["only_b"]), unsafe_allow_html=True)
        st.caption(f"Each {noun} is counted once, however many times it spotted you. A *spot* is one report, so "
                   "spots always outnumber stations.")

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
        if means_a.notna().any() or means_b.notna().any():
            st.subheader("Direction")
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
            chart_col, table_col = st.columns([1, 1])
            chart_col.pyplot(paired_scatter(an["shared"], names), width="stretch")
            every = an["shared_all"]
            same_place = (f" ({len(every)} {noun}s; ones at the same place are averaged into one for the result)"
                          if len(every) != s_["n"] else "")
            table_col.caption(f"{s_['n']} {noun}s heard both{same_place}. \U0001F535 A was stronger at {s_['wins_a']}, "
                              f"\U0001F7E0 B at {s_['wins_b']}, tied at {s_['ties']}. Each SNR is the median of that {noun}'s "
                              "spots. Click a column heading to sort.")
            table_col.dataframe(head_to_head_table(every, units), width="stretch",
                                height=min(38 * (len(every) + 1), 420))
        else:
            st.info(f"No {noun} heard both sides, so there is nothing to compare receiver by receiver.")

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


def main():
    st.set_page_config(layout="wide", page_title="RBN Signal Mapper", page_icon="📡")
    st.markdown(CSS, unsafe_allow_html=True)
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
        if kind == "WSPR" and ss.get(f"compare_{kind}", False):
            start_t, end_t = time.min, time.max  # comparing: you pick your test's cycles on the page instead
        else:
            start_t, end_t = st.slider(
                "UTC time window", min_value=lo_t, max_value=hi_t, value=(lo_t, hi_t), step=timedelta(minutes=1),
                format="HH:mm",
                help=("Only show spots between these times. It follows the spots you loaded."
                      if kind == "WSPR" else None))
        min_snr = -100 if kind == "WSPR" else st.slider("Minimum SNR (dB)", 0, 40, cfg.get("min_snr", 0))

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
            gap_khz = st.slider("Frequency gap (kHz)", 0.1, 5.0, 0.5, 0.1,
                                disabled=not (compare and split == SPLIT_FREQ),
                                help="Spots closer together than this count as the same frequency. Skimmers report slightly "
                                     "different frequencies for the same signal. Lower it if two tests are close together.")

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
    pdf_call, pdf_date, pdf_kind = ss.callsign, ss.file_date, ss.kind
    left, middle, right = st.columns([1, 1, 3])
    left.download_button("⬇️ Download map", map_html, f"RBN_map_{ss.callsign}_{ss.file_date}.html",
                         "text/html", width="stretch")
    middle.download_button("\u2B07\ufe0f Download report (PDF)", width="stretch", mime="application/pdf",
                           data=lambda: single_report_pdf(spots, locs, home, label, pdf_call, pdf_date, pdf_kind, noun,
                                                          units, scale),
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

    with st.expander("Spot table"):
        st.dataframe(spots.sort_values("time").assign(time=lambda d: d["time"].dt.strftime("%d %b %H:%M")),
                     width="stretch", hide_index=True)


if __name__ == "__main__":
    main()
