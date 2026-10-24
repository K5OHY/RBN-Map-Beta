"""The session report: what a day (or a few days) of spots says about each band you used. Nothing here draws a page; the
functions take the loaded spots and return small tables, sentences and matplotlib figures for web.py to place.

A spot only exists when somebody heard you, so "time on a band" here means the time you were heard on it, not the time
you were transmitting. The labels say so.
"""
from functools import lru_cache

import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from compare_stats import locate, receiver_table
from rbn_data import country_continent, country_location
from world_map import REACH_CMAP, SNR_CMAP

BAND_ORDER = ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
BIN_MIN = 10           # spots are grouped into blocks of this many minutes to see when you were being heard
GAP_BINS = 3           # a pause of more than this many blocks ends one stretch of activity and starts the next
THIN = 10              # a band with fewer spots than this is flagged: its numbers are only a hint
GREY, INK = "#8a8f98", "#cbd2dc"

REGION_ORDER = ["US East", "US Central", "US West", "Canada", "Mexico, Caribbean & Central America", "South America",
                "Europe", "Africa", "Asia", "Oceania", "Other"]


REGION_SHORT = {"Mexico, Caribbean & Central America": "Mex/Car", "US Central": "US Mid", "South America": "S. Am"}  # for tables that are short on width


def band_list(spots):
    """The bands in `spots`, lowest frequency first (unknown bands last)."""
    present = set(spots["band"].unique())
    return [b for b in BAND_ORDER if b in present] + sorted(present - set(BAND_ORDER))


@lru_cache(maxsize=20000)
def _country(call):
    info = country_location(call.split("-")[0])
    return info[2] if info else None


def region_of(call, loc=None):
    """A reader-friendly region for a receiver: where in the US (from its position), or the country group it is in."""
    country = _country(call)
    if country is None:
        return "Other"
    if country in ("United States", "Alaska"):
        lon = loc[1] if loc is not None else None
        if country == "Alaska" or (lon is not None and lon < -104):
            return "US West"
        if lon is not None and lon < -87:
            return "US Central"
        return "US East"
    if country == "Canada":
        return "Canada"
    cont = country_continent(country)
    return {"NA": "Mexico, Caribbean & Central America", "SA": "South America", "EU": "Europe", "AF": "Africa", "AS": "Asia",
            "OC": "Oceania"}.get(cont, "Other") if country != "Greenland" else "Other"


def _prepare(spots, locs, home):
    """Spots with the distance of each receiver, its country and its region added."""
    d = spots.copy()
    table = receiver_table(d, locs, home)
    d["km"] = d["spotter"].map(table["km"])
    d["country"] = d["spotter"].map(_country)
    d["region"] = [region_of(s, locate(s, locs)) for s in d["spotter"]]
    return d


def _bins(times):
    return times.dt.floor(f"{BIN_MIN}min")


def _runs(bins):
    """[(start, end)] stretches of blocks that have spots, joined across pauses of up to GAP_BINS blocks."""
    runs = []
    for t in sorted(set(bins)):
        if runs and t - runs[-1][1] <= pd.Timedelta(minutes=BIN_MIN * GAP_BINS):
            runs[-1][1] = t
        else:
            runs.append([t, t])
    return [(s, e + pd.Timedelta(minutes=BIN_MIN)) for s, e in runs]


def _duration(minutes):
    h, m = divmod(int(round(minutes)), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


# ------------------------------------------------------------------------------------------------ the numbers

def band_scorecard(spots, locs, home, units="mi"):
    """One row per band: how many spots and receivers, when you were heard, how strong, and how far."""
    k = 1.609344 if units == "mi" else 1.0
    near, far = (1000, 2000) if units == "mi" else (2000, 4000)
    d = _prepare(spots, locs, home)
    rows = []
    for band in band_list(d):
        g = d[d["band"] == band]
        located = g.dropna(subset=["km"]).groupby("spotter")["km"].first()
        far_who = located.idxmax() if len(located) else None
        rows.append({
            "Band": band, "Spots": len(g), "Receivers": g["spotter"].nunique(),
            "Heard from": g["time"].min(), "Heard until": g["time"].max(),
            "Minutes with spots": _bins(g["time"]).nunique() * BIN_MIN,
            "Median SNR": g["snr"].median(), "Best SNR": g["snr"].max(),
            f"Farthest ({units})": located.max() / k if len(located) else np.nan,
            "Farthest receiver": far_who or "",
            f"Receivers beyond {near:,} {units}": int((located / k > near).sum()),
            f"Receivers beyond {far:,} {units}": int((located / k > far).sum()),
        })
    return pd.DataFrame(rows).set_index("Band")


def headline(spots, locs, home, units="mi", noun="receiver"):
    """The session in a few sentences, plus the numbers behind them."""
    k = 1.609344 if units == "mi" else 1.0
    d = _prepare(spots, locs, home)
    first, last = d["time"].min(), d["time"].max()
    bands = band_list(d)
    per = d.groupby("band").size().reindex(bands)
    same_day = first.date() == last.date()
    when = (f"{first:%H:%M} to {last:%H:%M} UTC" if same_day else f"{first:%d %b %H:%M} to {last:%d %b %H:%M} UTC")
    lines = [f"You were heard from **{when}** ({_duration((last - first).total_seconds() / 60)}), on "
             f"**{len(bands)} band{'s' if len(bands) != 1 else ''}** ({', '.join(bands)}), "
             f"with **{len(d):,} spots** from **{d['spotter'].nunique():,} {noun}s**."]
    if len(bands) > 1:
        lines.append(f"Most spots: **{per.idxmax()}** ({int(per.max()):,}).")
    located = d.dropna(subset=["km"])
    if len(located):
        row = located.loc[located["km"].idxmax()]
        lines.append(f"Farthest: **{row['km'] / k:,.0f} {units}** on **{row['band']}** ({row['spotter']}).")
    best = d.loc[d["snr"].idxmax()]
    lines.append(f"Strongest report: **{best['snr']:.0f} dB** on **{best['band']}** ({best['spotter']}).")
    busiest = d.groupby(d["time"].dt.floor("h")).size()
    if len(busiest) > 1:
        lines.append(f"Busiest hour: **{busiest.idxmax():%H}:00 UTC** ({int(busiest.max()):,} spots).")
    return lines


def region_table(spots, locs, home):
    """Receivers per band in each region, with an 'All bands' row that counts each receiver once. Columns left empty are dropped."""
    d = _prepare(spots, locs, home)
    by_band = d.groupby(["band", "region"])["spotter"].nunique().unstack(fill_value=0).reindex(band_list(d))
    total = d.groupby("region")["spotter"].nunique().rename("All bands")
    table = pd.concat([total.to_frame().T, by_band]).fillna(0).astype(int)
    cols = [c for c in REGION_ORDER if c in table.columns and table[c].sum() > 0]
    table = table[cols]
    table["Total"] = pd.concat([pd.Series({"All bands": d["spotter"].nunique()}),
                                d.groupby("band")["spotter"].nunique().reindex(band_list(d))])
    return table


def country_table(spots):
    """Every country heard: receivers, spots, and the bands it was heard on."""
    d = spots.assign(country=spots["spotter"].map(_country).fillna("Unknown"))
    rows = [{"Country": c, "Receivers": g["spotter"].nunique(), "Spots": len(g),
             "Bands": ", ".join(b for b in BAND_ORDER if b in set(g["band"])) or ", ".join(sorted(set(g["band"])))}
            for c, g in d.groupby("country")]
    return pd.DataFrame(rows).sort_values(["Receivers", "Spots"], ascending=False).reset_index(drop=True)


# ------------------------------------------------------------------------------------------------ the pictures

def _style(ax):
    ax.set_facecolor("none")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GREY)
        ax.spines[side].set_alpha(0.5)
    ax.tick_params(colors=GREY, labelsize=9)


def timeline_figure(spots, colors, day=None):
    """One row per band: a bar for each stretch you were being heard, a tick for every spot. `day` limits it to one date."""
    d = spots if day is None else spots[spots["time"].dt.date == day]
    bands = band_list(d)
    fig = Figure(figsize=(8, 0.6 * len(bands) + 1.0))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot(111)
    _style(ax)
    for i, band in enumerate(bands):
        g = d[d["band"] == band]
        for start, end in _runs(_bins(g["time"])):
            x0, x1 = mdates.date2num(start), mdates.date2num(end)
            ax.broken_barh([(x0, x1 - x0)], (i - 0.34, 0.68), facecolors=colors.get(band, GREY), alpha=0.55,
                           edgecolor=INK, linewidth=0.7)
        ax.plot(mdates.date2num(g["time"]), [i] * len(g), "|", color="white", alpha=0.85, markersize=9, markeredgewidth=0.8)
    ax.set_yticks(range(len(bands)))
    ax.set_yticklabels([f"{b}  ({int((d['band'] == b).sum())})" for b in bands])
    ax.set_ylim(len(bands) - 0.4, -0.6)
    lo, hi = d["time"].min().floor("h"), d["time"].max().ceil("h")
    ax.set_xlim(mdates.date2num(lo), mdates.date2num(max(hi, lo + pd.Timedelta(hours=1))))
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    if d["time"].dt.date.nunique() == 1:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.set_xlabel(f"UTC, {d['time'].min():%d %b %Y}", color=GREY, fontsize=9)
    else:  # a short date under the clock time where the day changes, so labels never run together
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, show_offset=False))
        ax.set_xlabel("UTC", color=GREY, fontsize=9)
    ax.grid(axis="x", color=GREY, alpha=0.2)
    fig.tight_layout()
    return fig


GRID_METRICS = {"Number of spots": "spots", "Farthest reach": "farthest", "Median signal strength": "snr"}


def hour_grid_figure(spots, locs, home, metric="spots", units="mi"):
    """Bands down the side, hours of the day (UTC) across the top, each cell filled by the chosen measure."""
    k = 1.609344 if units == "mi" else 1.0
    d = _prepare(spots, locs, home)
    bands = band_list(d)
    d["hour"] = d["time"].dt.hour
    if d["time"].dt.date.nunique() == 1:
        hours = list(range(d["hour"].min(), d["hour"].max() + 1))
        hours = hours if len(hours) >= 6 else list(range(max(0, d["hour"].min() - 1), min(24, d["hour"].min() + 6)))
    else:
        hours = list(range(24))
    grid = np.full((len(bands), len(hours)), np.nan)
    for i, band in enumerate(bands):
        g = d[d["band"] == band]
        for j, h in enumerate(hours):
            cell = g[g["hour"] == h]
            if cell.empty:
                continue
            grid[i, j] = (len(cell) if metric == "spots" else cell["snr"].median() if metric == "snr"
                          else cell["km"].max() / k if cell["km"].notna().any() else np.nan)
    cmap = SNR_CMAP if metric == "snr" else REACH_CMAP
    fig = Figure(figsize=(max(4.4, 0.34 * len(hours) + 1.8), 0.42 * len(bands) + 0.9))
    fig.patch.set_alpha(0)
    ax = fig.add_subplot(111)
    ax.set_facecolor("none")
    shown = np.ma.masked_invalid(grid)
    image = ax.imshow(shown, cmap=cmap, aspect="auto", vmin=np.nanmin(grid) if metric == "snr" else 0, vmax=np.nanmax(grid))
    key = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.02)  # without it the colours mean nothing at a glance
    key.set_label({"spots": "spots in the hour", "farthest": f"farthest ({units})", "snr": "median SNR (dB)"}[metric],
                  color=GREY, fontsize=8)
    key.ax.tick_params(colors=GREY, labelsize=7)
    key.outline.set_visible(False)
    top = np.nanmax(grid) if np.isfinite(grid).any() else 1
    low = np.nanmin(grid) if metric == "snr" else 0
    for i in range(len(bands)):
        for j in range(len(hours)):
            v = grid[i, j]
            if np.isnan(v):
                continue
            text = (f"{v:.0f}" if metric != "farthest" or v < 1000 else f"{v / 1000:.1f}k")
            rgb = cmap(float(np.clip((v - low) / max(top - low, 1e-9), 0, 1)))
            light = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] > 0.5
            ax.text(j, i, text, ha="center", va="center", fontsize=8, color="#111111" if light else "white")
    ax.set_xticks(range(len(hours)))
    ax.set_xticklabels([f"{h:02d}" for h in hours], fontsize=8)
    ax.set_yticks(range(len(bands)))
    ax.set_yticklabels(bands)
    ax.tick_params(colors=GREY, length=0)
    ax.set_xlabel("Hour of the day (UTC)", color=GREY, fontsize=9)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_xticks(np.arange(-0.5, len(hours), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(bands), 1), minor=True)
    ax.grid(which="minor", color=GREY, alpha=0.35, linewidth=0.8)
    ax.tick_params(which="minor", length=0)
    fig.tight_layout()
    return fig
