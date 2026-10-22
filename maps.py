"""The interactive map of who heard you (Folium): paths, dots, popups with each station's history, and the headline numbers."""
import re

import folium
import numpy as np
import pandas as pd
from geographiclib.geodesic import Geodesic

from compare_stats import locate as skimmer_location, receiver_table
from ui_common import BAND_COLORS, KM_PER_MILE, RBN_SNR_SCALE, TILE_STYLES, _ESRI_ATTR, snr_color, snr_radius


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
