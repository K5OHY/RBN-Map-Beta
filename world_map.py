"""An azimuthal equidistant map of the world centred on your station: a receiver's distance from the centre is its true
distance from you, and its angle is its true compass bearing, so a straight line from the centre is a great-circle path.
That shows at a glance whether a signal is getting out toward Europe, Asia, New Zealand and so on.

The land comes from world_land.npz, a land/ocean grid built from Natural Earth outlines (public domain) by
build_world_mask.py. Each map is drawn by asking, for every pixel of the disk, which latitude and longitude it is and whether
that is land, so there are no polygon seams or cut-off edges. Plain numpy and matplotlib; nothing else to install.
"""
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure
from matplotlib.colors import LinearSegmentedColormap, to_rgb
from matplotlib.lines import Line2D
from matplotlib.patches import Wedge

EARTH_KM = 6371.0
MAX_KM = math.pi * EARTH_KM  # the far side of the world is at this distance
OCEAN, LAND, COAST = np.array([11, 17, 25]) / 255, np.array([32, 43, 58]) / 255, np.array([86, 106, 132]) / 255
INK, DIM = "#cbd2dc", "#7b8596"
CELL_EDGES_KM = [0, 1000, 2000, 3500, 5500, 8000, 11000, 15000, 20100]  # narrow near the station, where most receivers are
MIN_CELL = 3  # a cell needs at least this many receivers to be drawn
COL_A, COL_B = "#4f8cff", "#ff9a3c"
SNR_COLORS = ["#22b14c", "#ffd60a", "#8b0000"]  # weak to strong: green, yellow, dark red, as on the main map
SNR_CMAP = LinearSegmentedColormap.from_list("snr", SNR_COLORS)
REACH_CMAP = LinearSegmentedColormap.from_list("reach", ["#bcd4f6", "#6a9be8", "#2563eb", "#0b2e7a"])  # counts and distances: soft to deep blue
REGIONS = [("North America", 46, -102), ("South America", -15, -60), ("Europe", 51, 14), ("Africa", 5, 21), ("Asia", 52, 92),
           ("India", 22, 79), ("Japan", 37, 138), ("SE Asia", 8, 108), ("Australia", -25, 134), ("New Zealand", -42, 172),
           ("Antarctica", -80, 30), ("Greenland", 73, -42), ("Middle East", 28, 45), ("Pacific", 5, -150), ("Atlantic", 10, -35),
           ("Indian Ocean", -22, 78)]


@lru_cache(maxsize=1)
def _land_grid():
    z = np.load(Path(__file__).with_name("world_land.npz"))
    h, w = (int(v) for v in z["shape"])
    return np.unpackbits(z["bits"])[: h * w].reshape(h, w).astype(np.float32), float(z["res"])


def _forward(lat0, lon0, lat, lon):
    """(distance km, bearing radians) from the station to a latitude / longitude, on a sphere."""
    p0, p, dl = math.radians(lat0), np.radians(lat), np.radians(np.asarray(lon, dtype=float) - lon0)
    c = np.arccos(np.clip(math.sin(p0) * np.sin(p) + math.cos(p0) * np.cos(p) * np.cos(dl), -1, 1))
    az = np.arctan2(np.sin(dl) * np.cos(p), math.cos(p0) * np.sin(p) - math.sin(p0) * np.cos(p) * np.cos(dl))
    return c * EARTH_KM, az


@lru_cache(maxsize=12)
def _map_image(lat0, lon0, radius_km, px=900, supersample=2):
    """RGBA picture of the land and ocean inside `radius_km` of the station, north up. Pixels outside the disk are clear."""
    grid, res = _land_grid()
    n = px * supersample
    x = np.linspace(-radius_km, radius_km, n, dtype=np.float32)
    xx, yy = np.meshgrid(x, -x)  # north is up
    rr = np.hypot(xx, yy)
    c = rr / EARTH_KM
    az = np.arctan2(xx, yy)
    p0 = math.radians(lat0)
    sin_lat = math.sin(p0) * np.cos(c) + math.cos(p0) * np.sin(c) * np.cos(az)
    lat = np.degrees(np.arcsin(np.clip(sin_lat, -1, 1)))
    lon = lon0 + np.degrees(np.arctan2(np.sin(az) * np.sin(c), math.cos(p0) * np.cos(c) - math.sin(p0) * np.sin(c) * np.cos(az)))
    lon = (lon + 180) % 360 - 180
    rows = np.clip(((90 - lat) / res).astype(np.int32), 0, grid.shape[0] - 1)
    cols = np.clip(((lon + 180) / res).astype(np.int32), 0, grid.shape[1] - 1)
    land = grid[rows, cols]
    inside = (rr <= min(radius_km, MAX_KM)).astype(np.float32)
    del xx, yy, c, az, lat, lon, rows, cols
    # average the supersampled pixels, so coastlines are smooth
    land = land.reshape(px, supersample, px, supersample).mean(axis=(1, 3))
    inside = inside.reshape(px, supersample, px, supersample).mean(axis=(1, 3))
    gy, gx = np.gradient(land)
    edge = np.clip(np.hypot(gx, gy) * 5.0, 0, 1)[..., None]
    rgb = OCEAN + (LAND - OCEAN) * land[..., None]
    rgb = rgb + (COAST - rgb) * edge * 0.85
    return (np.dstack([rgb, inside]) * 255).astype(np.uint8)  # 8-bit keeps each cached map small


def _sun_position(when):
    """(declination, longitude) in degrees of the point on Earth where the sun is overhead at `when` (naive UTC). Good to about
    a degree, which is plenty for a day and night line."""
    day = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60
    b = 2 * math.pi * (day - 81) / 364
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)  # the equation of time, in minutes
    return -23.44 * math.cos(2 * math.pi * (day + 10) / 365), -15 * (hours - 12 + eot / 60)


def _daylight(lat0, lon0, radius_km, when, n=240):
    """The sine of the sun's height above the horizon over the disk of the map (NaN outside it), on an n by n grid, north up,
    with the x and y positions in km, for shading the night side and drawing the line between day and night."""
    x = np.linspace(-radius_km, radius_km, n, dtype=np.float64)
    xx, yy = np.meshgrid(x, -x)
    rr = np.hypot(xx, yy)
    c, az, p0 = rr / EARTH_KM, np.arctan2(xx, yy), math.radians(lat0)
    lat = np.arcsin(np.clip(math.sin(p0) * np.cos(c) + math.cos(p0) * np.sin(c) * np.cos(az), -1, 1))
    lon = math.radians(lon0) + np.arctan2(np.sin(az) * np.sin(c), math.cos(p0) * np.cos(c) - math.sin(p0) * np.sin(c) * np.cos(az))
    decl, sun_lon = _sun_position(when)
    sin_h = np.sin(lat) * math.sin(math.radians(decl)) + np.cos(lat) * math.cos(math.radians(decl)) * np.cos(lon - math.radians(sun_lon))
    return xx, yy, np.where(rr <= radius_km, sin_h, np.nan)


def _terminator(lat0, lon0, radius_km, when):
    """x and y (km from the station, north up) of the line between day and night: the circle 90 degrees from the point where the
    sun is overhead. Points outside the map are NaN, so the line breaks there."""
    decl, sun_lon = _sun_position(when)
    th = np.linspace(0, 2 * np.pi, 721)
    cos_d, sin_d = math.cos(math.radians(decl)), math.sin(math.radians(decl))
    lat = np.arcsin(cos_d * np.cos(th))
    lon = sun_lon + np.degrees(np.arctan2(np.sin(th) * cos_d, -sin_d * np.sin(lat)))
    d, az = _forward(lat0, lon0, np.degrees(lat), lon)
    x, y = d * np.sin(az), d * np.cos(az)
    outside = d > radius_km
    return np.where(outside, np.nan, x), np.where(outside, np.nan, y)


def _ring_step(radius_units):
    for step in (250, 500, 1000, 2000, 2500, 5000):
        if radius_units / step <= 6:
            return step
    return 5000


def text_color_on(rgb, alpha=1.0, backdrop=(0.06, 0.08, 0.11)):
    """Dark or white lettering for a cell of colour `rgb` drawn with `alpha` over the dark map, whichever reads better."""
    blended = [alpha * c + (1 - alpha) * b for c, b in zip(rgb[:3], backdrop)]
    luminance = 0.2126 * blended[0] + 0.7152 * blended[1] + 0.0722 * blended[2]
    return "#111111" if luminance > 0.45 else "white"


def _cells(points, layer, radius_km, sector):
    """Direction-and-distance cells: for each sector and distance band, the number of receivers and their average value.
    `layer` says what is averaged: 'advantage' (A minus B, at receivers that heard both), 'snr_a', 'snr_b', 'snr' (one antenna)
    or 'reach' (just the count). Cells with fewer than MIN_CELL receivers are left out."""
    if layer == "advantage":
        pts, col = points[(points["group"] == "both")].dropna(subset=["gap"]), "gap"
    elif layer == "snr_a":
        pts, col = points[points["group"].isin(["both", "only_a"])].dropna(subset=["snr_a"]), "snr_a"
    elif layer == "snr_b":
        pts, col = points[points["group"].isin(["both", "only_b"])].dropna(subset=["snr_b"]), "snr_b"
    elif layer == "snr":
        pts, col = points.dropna(subset=["snr_a"]), "snr_a"
    else:
        pts, col = points, None
    edges = [e for e in CELL_EDGES_KM if e < radius_km] + [radius_km]
    cells = []
    for b0 in range(0, 360, sector):
        for r0, r1 in zip(edges[:-1], edges[1:]):
            m = pts[(pts["bearing"] % 360 >= b0) & (pts["bearing"] % 360 < b0 + sector) & (pts["km"] >= r0) & (pts["km"] < r1)]
            if len(m) >= MIN_CELL:
                cells.append({"b0": b0, "b1": b0 + sector, "r0": r0, "r1": r1, "n": len(m),
                              "value": float(m[col].mean()) if col else float(len(m))})
    return cells


def azimuth_map_figure(lat0, lon0, points, names, callsign, units="mi", radius_km=MAX_KM, layer="dots", show_dots=True,
                       snr_range=(-28, 2), size=7.2, when=None, label_min=5):
    """The map. `points` is a DataFrame with `km`, `bearing` (degrees), `group` ('both', 'only_a', 'only_b' or 'single'), `gap`
    (A's SNR minus B's, for stations that heard both), `snr_a` and `snr_b` (each antenna's SNR at the station; for one antenna,
    `snr_a`) and optionally `color`. `layer` colours direction-and-distance cells: 'advantage', 'snr_a', 'snr_b', 'snr',
    'reach', or 'dots' for none. With `when` (naive UTC) the night side is shaded and the line between day and night drawn.
    A cell's number is written in it from `label_min` receivers up (cells are drawn from MIN_CELL).
    Returns a matplotlib Figure."""
    radius_km = min(float(radius_km), MAX_KM)
    km_per_unit = 1.609344 if units == "mi" else 1.0
    single = len(points) > 0 and bool((points["group"] == "single").all())
    fig = Figure(figsize=(size, size))
    fig.patch.set_alpha(0)
    ax = fig.add_axes([0.02, 0.13, 0.96, 0.85])
    ax.set_aspect("equal")
    ax.axis("off")
    R = radius_km
    ax.imshow(_map_image(round(lat0, 1), round(lon0, 1), round(R)), extent=[-R, R, -R, R], origin="upper", interpolation="bilinear",
              zorder=1)
    theta = np.linspace(0, 2 * np.pi, 400)
    if when is not None:  # the night side, and the gray line between night and day
        xx, yy, sin_h = _daylight(lat0, lon0, R, when)
        shade = np.zeros(sin_h.shape + (4,))
        shade[..., :3] = (4 / 255, 7 / 255, 18 / 255)
        shade[..., 3] = np.where(np.isnan(sin_h), 0.0, np.clip(-sin_h / 0.12, 0, 1) * 0.5)
        ax.imshow(shade, extent=[-R, R, -R, R], origin="upper", interpolation="bilinear", zorder=2)
        gx, gy = _terminator(lat0, lon0, R, when)
        ax.plot(gx, gy, color="#ffd166", linewidth=1.2, alpha=0.9, zorder=4)
    # the cells, drawn first so the rings and dots sit on top
    cells = _cells(points, layer, R, 30) if layer != "dots" and len(points) else []
    cbar = None
    if cells:
        values = np.array([c["value"] for c in cells])
        if layer == "advantage":
            scale = max(4.0, float(np.ceil(np.abs(values).max() / 2) * 2))
            cbar = ("advantage", -scale, scale)
        elif layer == "reach":
            cbar = ("reach", 0.0, float(values.max()))
        else:
            lo, hi = snr_range
            cbar = ("snr", float(lo), float(hi))
        for c in cells:
            v = c["value"]
            if layer == "advantage":
                base = np.array(to_rgb(COL_A if v > 0 else COL_B))
                t = min(abs(v) / cbar[2], 1.0)
                color = tuple(np.array(to_rgb("#202b3a")) + (base - np.array(to_rgb("#202b3a"))) * (0.3 + 0.7 * t))
                alpha = 0.3 + 0.55 * min(c["n"] / 10, 1.0)
            else:
                cmap = REACH_CMAP if layer == "reach" else SNR_CMAP
                color = cmap(float(np.clip((v - cbar[1]) / max(cbar[2] - cbar[1], 1e-9), 0, 1)))[:3]
                alpha = 0.35 + 0.5 * min(c["n"] / 10, 1.0)
            ax.add_patch(Wedge((0, 0), c["r1"], 90 - c["b1"], 90 - c["b0"], width=c["r1"] - c["r0"], facecolor=color,
                               edgecolor="#0b1119", linewidth=0.5, alpha=alpha, zorder=3))
            mid_r, ang = (c["r0"] + c["r1"]) / 2, math.radians((c["b0"] + c["b1"]) / 2)
            if c["n"] >= label_min and (c["r1"] - c["r0"]) > R * 0.07:
                label = f"{abs(v):.1f}" if layer == "advantage" else f"{v:.0f}"
                ax.text(mid_r * math.sin(ang), mid_r * math.cos(ang), label, fontsize=6.5,
                        color=text_color_on(color, alpha) if layer != "advantage" else "white", ha="center", va="center",
                        zorder=6, fontweight="bold")
    # distance rings, labelled in the user's units
    step = _ring_step(R / km_per_unit)
    for d in np.arange(step, R / km_per_unit * 0.999, step):
        r = d * km_per_unit
        ax.plot(r * np.sin(theta), r * np.cos(theta), color=INK, linewidth=0.6, linestyle=(0, (3, 4)), alpha=0.35, zorder=4)
        ax.text(r * 0.03, r + R * 0.012, f"{d:,.0f} {units}", fontsize=7, color=INK, alpha=0.8, ha="left", va="bottom", zorder=7)
    ax.plot(R * np.sin(theta), R * np.cos(theta), color=DIM, linewidth=1.2, zorder=4)
    for deg in range(0, 360, 30):
        a = math.radians(deg)
        ax.plot([0, R * math.sin(a)], [0, R * math.cos(a)], color=INK, linewidth=0.5, alpha=0.18, zorder=4)
    for deg in range(0, 360, 10):
        a, tick = math.radians(deg), (0.025 if deg % 30 == 0 else 0.012)
        ax.plot([R * math.sin(a), R * (1 + tick) * math.sin(a)], [R * math.cos(a), R * (1 + tick) * math.cos(a)], color=DIM,
                linewidth=0.8, zorder=4)
    for deg, text in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        a = math.radians(deg)
        ax.text(R * 1.075 * math.sin(a), R * 1.075 * math.cos(a), text, ha="center", va="center", fontsize=12, color="white",
                fontweight="bold")
    for deg in range(30, 360, 30):
        if deg % 90:
            a = math.radians(deg)
            ax.text(R * 1.07 * math.sin(a), R * 1.07 * math.cos(a), f"{deg}\u00b0", ha="center", va="center", fontsize=7.5, color=DIM)
    for label, la, lo in REGIONS:
        d, az = _forward(lat0, lon0, la, lo)
        if 0.05 * R < d < 0.94 * R:
            ax.text(float(d * np.sin(az)), float(d * np.cos(az)), label, fontsize=7.5, color=INK, alpha=0.5, ha="center", va="center",
                    style="italic", zorder=5)
    # the receivers
    shown = points[points["km"] <= R] if len(points) else points
    zoom = float(np.clip(1.4 - 0.9 * R / MAX_KM, 0.5, 1.4))
    faint = 0.55 if cells else 1.0
    if show_dots and len(shown):
        a = np.radians(shown["bearing"].to_numpy(float))
        px, py = shown["km"].to_numpy(float) * np.sin(a), shown["km"].to_numpy(float) * np.cos(a)
        if single:
            if "color" in shown:
                cols = shown["color"].to_list()
            else:
                lo, hi = snr_range
                cols = [SNR_CMAP(float(np.clip((v - lo) / max(hi - lo, 1e-9), 0, 1))) for v in shown["snr_a"].to_numpy(float)]
            ax.scatter(px, py, s=40 * zoom * 2.4, c=cols, alpha=0.14 * faint, linewidths=0, zorder=8)
            ax.scatter(px, py, s=40 * zoom, c=cols, alpha=0.9 * faint, linewidths=0.4, edgecolors="#0b1119", zorder=9)
        else:
            is_both = (shown["group"] == "both").to_numpy()
            is_a, is_b = (shown["group"] == "only_a").to_numpy(), (shown["group"] == "only_b").to_numpy()
            for mask, color in ((is_a, COL_A), (is_b, COL_B)):
                if mask.any():
                    ax.scatter(px[mask], py[mask], s=34 * zoom, facecolors="none", edgecolors=color, linewidths=1.4, alpha=faint,
                               zorder=9)
            if is_both.any():
                gap = shown["gap"].to_numpy(float)
                colors = np.where(gap > 0, COL_A, np.where(gap < 0, COL_B, "#9aa4b2"))
                size = (14 + 9 * np.clip(np.abs(np.nan_to_num(gap)), 0, 8))[is_both] * zoom
                ax.scatter(px[is_both], py[is_both], s=size * 2.6, c=colors[is_both], alpha=0.16 * faint, linewidths=0, zorder=8)
                ax.scatter(px[is_both], py[is_both], s=size, c=colors[is_both], alpha=0.92 * faint, linewidths=0.4,
                           edgecolors="#0b1119", zorder=9)
    ax.scatter([0], [0], marker="*", s=220, color="white", edgecolors="#0b1119", linewidths=0.8, zorder=10)
    ax.text(0, -R * 0.045, callsign, ha="center", va="top", fontsize=8.5, color="white", fontweight="bold", zorder=10)
    # a scale bar for the cells, and a key for the dots
    if cbar:
        kind, lo, hi = cbar
        cax = fig.add_axes([0.28, 0.085, 0.44, 0.014])
        grad = np.linspace(0, 1, 256)[None, :]
        if kind == "advantage":
            left, right = np.array(to_rgb(COL_A)), np.array(to_rgb(COL_B))
            mid = np.array(to_rgb("#202b3a"))
            ramp = np.array([mid + (left - mid) * (1 - 2 * t) if t < 0.5 else mid + (right - mid) * (2 * t - 1) for t in grad[0]])
            cax.imshow(ramp[None, :, :], aspect="auto")
            text_l, text_r, text_m = f"{names['A']} stronger", f"{names['B']} stronger", "0"
        else:
            cax.imshow(grad, aspect="auto", cmap=REACH_CMAP if kind == "reach" else SNR_CMAP)
            text_l, text_r, text_m = (("few", "many", "") if kind == "reach" else (f"{lo:.0f} dB", f"{hi:.0f} dB", ""))
        cax.set_axis_off()
        fig.text(0.27, 0.105, text_l, ha="right", va="center", fontsize=7.5, color=INK)
        fig.text(0.73, 0.105, text_r, ha="left", va="center", fontsize=7.5, color=INK)
        fig.text(0.5, 0.062, {"advantage": f"dB stronger, up to {hi:g}", "reach": "stations heard in the cell",
                              "snr": "average SNR in the cell"}[kind], ha="center", va="top", fontsize=7, color=DIM)
    if show_dots and not single:
        handles = [Line2D([], [], marker="o", linestyle="", color=COL_A, markersize=6, label=f"Both heard it, {names['A']} stronger"),
                   Line2D([], [], marker="o", linestyle="", color=COL_B, markersize=6, label=f"Both heard it, {names['B']} stronger"),
                   Line2D([], [], marker="o", linestyle="", markerfacecolor="none", markeredgecolor=COL_A, markersize=6,
                          label=f"Only {names['A']} heard it"),
                   Line2D([], [], marker="o", linestyle="", markerfacecolor="none", markeredgecolor=COL_B, markersize=6,
                          label=f"Only {names['B']} heard it")]
        leg = fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False, fontsize=7.5, bbox_to_anchor=(0.5, -0.01))
        for text in leg.get_texts():
            text.set_color(INK)
    ax.set_xlim(-R * 1.12, R * 1.12)
    ax.set_ylim(-R * 1.12, R * 1.12)
    return fig
