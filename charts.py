"""The charts and tables for a comparison and for the session: direction, distance, relative gain, timeline, scoreboard."""
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from geographiclib.geodesic import Geodesic
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from compare_stats import BIN_MIN_SHARED, SECTOR_NAMES, locate as skimmer_location, t_crit
from ui_common import KM_PER_MILE, RBN_SNR_SCALE, _bold, snr_color


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


COLOR_A, COLOR_B, COLOR_TIE = "#2563eb", "#f97316", "#8a8f98"


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


def verdict_card_html(v):
    """The overall answer in a card tinted for the winner (blue = A, orange = B, grey = neither)."""
    color = {"A": COLOR_A, "B": COLOR_B}.get(v["winner"], COLOR_TIE)
    points = "".join(f'<li style="margin:7px 0">{icon} {_bold(text)}</li>' for icon, text in v["points"])
    caveats = "".join(f'<div style="margin-top:6px">⚠️ {_bold(c)}</div>' for c in v["caveats"])
    return (f'<div style="border-left:6px solid {color};background:{color}1c;border-radius:10px;padding:16px 22px;'
            f'margin:4px 0 16px 0"><div style="font-size:1.3rem;line-height:1.45">{_bold(v["headline"])}</div>'
            f'<ul style="list-style:none;padding:0;margin:12px 0 0 0;font-size:.95rem">{points}</ul>'
            f'<div style="font-size:.85rem;opacity:.8;margin-top:8px">{caveats}</div></div>')


def _breakdown_rows(df, names):
    """Rows for the distance / direction tables in a report: label, A count, B count, both, who by how much, better."""
    rows = []
    for _, row in df.iterrows():
        lead = "–" if pd.isna(row["delta"]) or abs(row["delta"]) < 0.05 else f"{'A' if row['delta'] > 0 else 'B'} +{abs(row['delta']):.1f} dB"
        better = names.get(row["call"], "–") if row["call"] else "–"
        rows.append([row["label"], str(int(row["a"])), str(int(row["b"])), str(int(row["shared"])), lead, better])
    return rows


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
