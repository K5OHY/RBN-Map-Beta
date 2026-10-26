"""The HTML pieces of the Band conditions dashboard: the summary card, one tile per band, the sun-and-MUF strip, the
region grid and the highlights. Each function takes the analysis (or readings) and returns a string for st.html, so the layout
lives here and web.py only places the pieces."""
import html
import math
import re

import pandas as pd

import conditions as cond

MHZ = {"160m": "1.8", "80m": "3.5", "60m": "5.3", "40m": "7", "30m": "10.1", "20m": "14", "17m": "18.1", "15m": "21",
       "12m": "24.9", "10m": "28", "6m": "50"}
# The colours of the band dots on the receiver map, brighter than the RBN website's so every band shows on its dark background.
MAP_COLORS = {"160m": "#ffe000", "80m": "#35d07f", "60m": "#aaaaaa", "40m": "#ffa500", "30m": "#ff4136", "20m": "#b45cff",
              "17m": "#4da3ff", "15m": "#d9d9d9", "12m": "#00e5ff", "10m": "#ff4fd8", "6m": "#ffc0cb"}
REGION_NAME = {"Mexico, Caribbean & Central America": "Mexico / Caribbean"}
TONE_CLASS = {"good": "tg", "ok": "tn", "warn": "ta", "bad": "tr", "quiet": "tm"}

CSS = """<style>
.cd { --g:#3fb950; --a:#d29922; --r:#f85149; --m:#8b949e; --line:rgba(128,128,128,.28); --card:rgba(128,128,128,.07); }
.cd .tg { color:var(--g); } .cd .ta { color:var(--a); } .cd .tr { color:var(--r); } .cd .tm { color:var(--m); } .cd .tn { opacity:.75; }
.cd-top { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; border:1px solid var(--line);
  background:var(--card); border-radius:12px; padding:10px 16px; margin:0 0 12px 0; }
.cd-top b { font-size:16px; } .cd-top small { display:block; opacity:.7; font-size:13px; }
.cd-badge { border:1px solid var(--line); border-radius:8px; padding:4px 10px; font-size:13px; white-space:nowrap; }
.cd-tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(118px,1fr)); gap:10px; margin:0 0 12px 0; }
.cd-tile { border:1px solid var(--line); background:var(--card); border-radius:12px; padding:12px 14px; }
.cd-tile.tg-b { border-color:var(--g); background:rgba(63,185,80,.09); }
.cd-tile.ta-b { border-color:var(--a); background:rgba(210,153,34,.10); }
.cd-tile.tm-b { border-color:var(--line); }
.cd-row { display:flex; justify-content:space-between; align-items:flex-start; gap:6px; flex-wrap:wrap; }
.cd-tile { overflow:hidden; }
.cd-band { font-size:26px; font-weight:600; line-height:1.1; } .cd-mhz { font-size:13px; opacity:.75; }
.cd-bars { display:flex; gap:2px; align-items:flex-end; height:20px; margin-top:4px; }
.cd-bars i { width:4px; border-radius:1px; background:currentColor; } .cd-bars i.off { opacity:.25; }
.cd-pill { display:inline-block; border:1px solid currentColor; border-radius:999px; padding:2px 10px; font-size:14px; margin:8px 0 4px; white-space:nowrap; }
.cd-spark { display:flex; gap:2px; align-items:flex-end; height:24px; } .cd-spark i { width:5px; border-radius:1px; background:currentColor; opacity:.8; }
.cd-conf { font-size:13px; opacity:.8; }
.cd-strip { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); border:1px solid var(--line);
  background:var(--card); border-radius:12px; margin:0 0 12px 0; }
.cd-chip { padding:10px 16px; border-left:1px solid var(--line); } .cd-chip:first-child { border-left:0; }
.cd-cl { font-size:13px; opacity:.75; } .cd-cv { font-size:22px; font-weight:600; line-height:1.25; }
.cd-cv small { font-size:13px; font-weight:400; opacity:.75; } .cd-cw { font-size:14px; } .cd-cn { font-size:12px; opacity:.7; }
.cd-h { font-size:18px; font-weight:600; margin:0; } .cd-sub { font-size:13px; opacity:.7; margin:0 0 8px 0; }
.cd-grid { width:100%; border-collapse:separate; border-spacing:2px; font-size:14px; }
.cd-grid th { font-weight:500; font-size:13px; opacity:.75; padding:4px 2px; text-align:center; }
.cd-grid th:first-child, .cd-grid td:first-child { text-align:left; opacity:1; white-space:nowrap; padding-right:8px; }
.cd-grid td { text-align:center; padding:7px 2px; border-radius:3px; }
.c0 { opacity:.55; } .c1 { background:rgba(248,81,73,.38); } .c2 { background:rgba(210,153,34,.45); }
.c3 { background:rgba(63,185,80,.42); } .c4 { background:rgba(63,185,80,.72); } .cth { background:rgba(138,143,152,.22); font-style:italic; }
.cd-w { display:block; font-size:12px; opacity:.85; }
.cd-key { font-size:12px; opacity:.75; margin-top:8px; }
.cd-key i { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 4px 0 10px; vertical-align:-1px; }
.cd-tile.sel { outline:2px solid #4da3ff; outline-offset:1px; } .cd-tile.dim { opacity:.45; }
.cd-grid .dim { opacity:.3; } .cd-grid th.f { opacity:1; font-weight:700; color:#4da3ff; } .cd-grid td.f { box-shadow:inset 0 0 0 2px #4da3ff; }
.cd-panel { border:1px solid var(--line); background:var(--card); border-radius:12px; padding:10px 14px; margin:0 0 10px 0; font-size:14px; line-height:1.45; }
.cd-panel .cd-band { font-size:22px; } .cd-panel p { margin:4px 0 0 0; }
.cd-item b { font-weight:600; } .cd-item { padding:9px 0; border-top:1px solid var(--line); font-size:14px; line-height:1.45; } .cd-item:first-of-type { border-top:0; }
</style>"""


def _e(text):
    return html.escape(str(text))


def _bold(text):
    """Escapes `text` and turns its **bold** marks into <b>."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(text))


def state(status, trend):
    """The word and tone shown for a band: 'Opening' for an open band that is rising, else its status with an arrow if it is
    moving."""
    rising, falling = trend in ("opening", "new"), trend == "fading"
    if status == "Open":
        return "Open" + (" \u2197" if rising else " \u2198" if falling else ""), "tg"
    if status == "Marginal":
        return "Marginal" + (" ↗" if rising else " ↘" if falling else ""), "ta"
    if status == "Local":
        return "Local only", "tm"
    return "Quiet", "tm"


def top_html(setup, spots_text, badge, badge_tone, badge_help):
    """The card at the top right: what the figures are for, how much data is behind them, and how far to trust the DX."""
    return (f'<div class="cd cd-top"><div><b>{_e(setup)}</b><small>{_e(spots_text)}</small></div>'
            f'<div class="cd-badge {badge_tone}" title="{_e(badge_help)}">{_e(badge)}</div></div>')


def _spark(values, tone):
    """A few small bars of how the band's strength moved over the look-back, oldest first. The scale is at least 6 dB tall, so
    noise stays small, and a step with no report has no bar."""
    pts = [v for v in values if v is not None and not math.isnan(v)]
    if len(pts) < 2:
        return ""
    lo, span = min(pts), max(max(pts) - min(pts), 6.0)
    bars = "".join('<i style="height:0"></i>' if v is None or math.isnan(v) else f'<i style="height:{4 + 18 * (v - lo) / span:.0f}px"></i>'
                   for v in values)
    return (f'<div class="cd-spark {tone}" title="Signal strength over the look-back, oldest to newest (taller is '
            f'stronger)">{bars}</div>')


def _confidence(receivers):
    return "High" if receivers >= 15 else "Moderate" if receivers >= 6 else "Low"


def tiles_html(bands, series, focus=(), units="mi"):
    """One tile per band: its name, state, how its strength moved, and how much data is behind it. The bands in `focus`
    are outlined and the others faded."""
    tiles = []
    for band in bands.index:
        shade = "" if not focus else " sel" if band in focus else " dim"
        row = bands.loc[band]
        word, tone = state(row["Status"], row["Trend"])
        lit = 4 if row["Status"] == "Open" else 2 if row["Status"] == "Marginal" else 1
        bars = "".join(f'<i class="{"" if i < lit else "off"}" style="height:{5 + 5 * i}px"></i>' for i in range(4))
        tip = (f"{int(row['Receivers'])} receivers heard WSPR stations near you on {band}; {int(row['Good'])} are good for your "
               f"signal and {int(row['Marginal'])} marginal.")
        if row["Status"] == "Local":
            tip += " They are all close by, within the distance where the ionosphere is not needed, so the band is not open yet."
        far_km = row.get("Farthest good (km)")
        reach = ""
        if far_km is not None and not math.isnan(far_km):
            k = 1.609344 if units == "mi" else 1.0
            reach = '<div class="cd-conf">to {:,.0f} {}</div>'.format(far_km / k, units)
            tip += f" The farthest good receiver was {far_km / k:,.0f} {units} away."
        tiles.append(
            f'<div class="cd-tile {tone}-b{shade}" title="{_e(tip)}"><div class="cd-row"><div><div class="cd-band">{_e(band)}</div>'
            f'<div class="cd-mhz">{MHZ.get(band, "")} MHz</div></div><div class="cd-bars {tone}">{bars}</div></div>'
            f'<div class="cd-row"><span class="cd-pill {tone}">{_e(word)}</span>{_spark(series.get(band, []), tone)}</div>'
            f'<div class="cd-conf">{_confidence(int(row["Receivers"]))} confidence</div>{reach}</div>')
    key = ('<div class="cd cd-key">\u2197 rising \u00b7 \u2198 fading \u00b7 the small bars are the band\'s signal strength at your '
           'power, oldest to newest</div>')
    return f'<div class="cd cd-tiles">{"".join(tiles)}</div>{key}'


def strip_html(readings, est=None):
    """The sun and the MUF in one strip, a number and a one-word reading each."""
    chips = [(r["label"], r["value"], r["unit"], r["word"], TONE_CLASS[r["tone"]], "", r["help"]) for r in readings]
    if est:
        import ionosphere as iono
        top = iono.top_band(est["muf"][3000])
        chips.append(("Est. MUF, 3,000 km", f"{est['muf'][3000]:.0f}", "MHz", "Model estimate", "tn",
                      f"NVIS ceiling {est['fof2']:.1f} MHz",
                      "The highest frequency expected to work over a 3,000 km path, estimated from the nearest ionosondes"
                      + (f" ({top} and below)" if top else "") + ". It is an estimate, not a limit on what WSPR shows is working: "
                      "shorter paths can use less, and openings are often seen above it. NVIS is the highest frequency that "
                      "still bounces straight back for local contacts."))
    if not chips:
        return ""
    cells = []
    for label, value, unit, word, tone, note, tip in chips:
        unit_html = f" <small>{_e(unit)}</small>" if unit else ""
        note_html = f'<div class="cd-cn">{_e(note)}</div>' if note else ""
        cells.append(f'<div class="cd-chip" title="{_e(tip)}"><div class="cd-cl">{_e(label)}</div><div class="cd-cv">{_e(value)}'
                     f'{unit_html}</div><div class="cd-cw {tone}">{_e(word)}</div>{note_html}</div>')
    cells = "".join(cells)
    return f'<div class="cd cd-strip">{cells}</div>'


def _cell(p, n):
    if pd.isna(p):
        return '<td class="c0">–</td>'
    tip = f'{n} receiver{"s" if n != 1 else ""}'
    if n < cond.THIN:
        return f'<td class="cth" title="Only {tip}, so too few for a percentage">{n} RX</td>'
    level = "c0" if p < 0.15 else "c1" if p < 0.4 else "c2" if p < 0.7 else "c3" if p < 0.85 else "c4"
    return f'<td class="{level}" title="{tip}">{p:.0%}</td>'


def _mark(cell, band, focus):
    """Adds the 'f' (picked) or 'dim' class to a table cell when bands are picked."""
    if not focus:
        return cell
    return cell.replace('class="', f'class="{"f" if band in focus else "dim"} ', 1)


def grid_html(chance, counts, focus=()):
    """Regions down the side, bands across the top: the share of receivers in each that would hear you. The columns of the
    bands in `focus` are outlined and the others faded."""
    chance, counts = chance.drop(columns=["Other"], errors="ignore"), counts.drop(columns=["Other"], errors="ignore")
    chance, counts = chance.rename(columns=REGION_NAME), counts.rename(columns=REGION_NAME)
    head = "".join(_mark(f"<th class=\"\">{_e(b)}</th>", b, focus) for b in chance.index)
    rows = "".join(f"<tr><td>{_e(region)}</td>" + "".join(_mark(_cell(chance.at[b, region], int(counts.at[b, region])), b, focus)
                                                           for b in chance.index) + "</tr>" for region in chance.columns)
    key = ('<div class="cd-key">Share of receivers that would hear you'
           '<i style="background:rgba(63,185,80,.72)"></i>85%+<i style="background:rgba(63,185,80,.42)"></i>70 to 85'
           '<i style="background:rgba(210,153,34,.45)"></i>40 to 70<i style="background:rgba(248,81,73,.38)"></i>under 40'
           '<i style="background:rgba(138,143,152,.3)"></i>1 RX or 2 RX: too few receivers for a percentage</div>')
    return (f'<div class="cd"><p class="cd-h">Where you would be heard</p><p class="cd-sub">Share of receivers in each region that '
            f'would hear you</p><table class="cd-grid"><tr><th>Region</th>{head}</tr>{rows}</table>{key}</div>')


def band_panel_html(band, row, chance_row, counts_row, need_row, units, k):
    """A short summary of one band: how it is doing, how many receivers heard it, how far a good signal reached, and
    which regions would hear you best and worst (only regions with enough receivers for a percentage)."""
    word, tone = state(row["Status"], row["Trend"])
    lines = [f'{int(row["Receivers"])} receivers heard WSPR stations near you on this band; {int(row["Good"])} are good for you '
             f'and {int(row["Marginal"])} marginal.']
    if pd.notna(row["Farthest good (km)"]):
        lines.append(f'Farthest good: {row["Farthest good (km)"] / k:,.0f} {units}, {_e(row["Farthest good receiver"])} '
                     f'({_e(row["Farthest good region"])}).')
    ok = chance_row.drop(labels=["Other"], errors="ignore").where(counts_row.drop(labels=["Other"], errors="ignore") >= cond.THIN).dropna()
    ok = ok.sort_values(ascending=False)
    ok.index = [REGION_NAME.get(r, r) for r in ok.index]
    if len(ok):
        lines.append("Best: " + ", ".join(f"{_e(r)} {p:.0%}" for r, p in ok.head(3).items()) + ".")
        worst = ok[ok < 0.4].tail(2)
        if len(worst):
            lines.append("Hard: " + ", ".join(f"{_e(r)} {p:.0%}" for r, p in worst.items()) + ".")
    else:
        lines.append("No region has enough receivers for a percentage yet.")
    return (f'<div class="cd"><div class="cd-panel"><div class="cd-row"><div><span class="cd-band">{_e(band)}</span> '
            f'<span class="cd-mhz">{MHZ.get(band, "")} MHz</span></div><span class="cd-pill {tone}">{_e(word)}</span></div>'
            + "".join(f"<p>{line}</p>" for line in lines) + "</div></div>")


def _watts(w):
    return "<0.1 W" if w < 0.1 else ">1500 W" if w > 1500 else f"{w:.1f} W" if w < 10 else f"{w:.0f} W"


def detail_grid_html(margin, need, counts, margin_db):
    """Regions down the side, bands across the top: the margin over the mode's threshold at a typical receiver, and under it the
    watts needed to reach that receiver with your margin to spare. A cell with fewer than THIN receivers shows only its count:
    the receivers that heard anything there are the lucky ones, so a figure from one or two would look far too easy."""
    margin, need, counts = (t.drop(columns=["Other"], errors="ignore").rename(columns=REGION_NAME) for t in (margin, need, counts))
    head = "".join(f"<th>{_e(b)}</th>" for b in margin.index)
    rows = []
    for region in margin.columns:
        cells = []
        for band in margin.index:
            m, n = margin.at[band, region], int(counts.at[band, region])
            if pd.isna(m):
                cells.append('<td class="c0">\u2013</td>')
            elif n < cond.THIN:
                cells.append(f'<td class="cth" title="Only {n} receiver{"s" if n != 1 else ""}, so too few for a figure">{n} RX</td>')
            else:
                level = "c3" if m >= margin_db else "c2" if m >= 0 else "c1"
                cells.append(f'<td class="{level}" title="{n} receivers"><b>{m:+.0f} dB</b>'
                             f'<span class="cd-w">{_watts(float(need.at[band, region]))}</span></td>')
        rows.append(f"<tr><td>{_e(region)}</td>{''.join(cells)}</tr>")
    key = ('<div class="cd-key">Margin over the threshold at a typical receiver, and the watts it would take'
           f'<i style="background:rgba(63,185,80,.42)"></i>clears your {margin_db} dB margin'
           '<i style="background:rgba(210,153,34,.45)"></i>above the threshold<i style="background:rgba(248,81,73,.38)"></i>below'
           '<i style="background:rgba(138,143,152,.3)"></i>1 RX or 2 RX: too few receivers, so no figure</div>')
    return f'<div class="cd"><table class="cd-grid"><tr><th>Region</th>{head}</tr>{"".join(rows)}</table>{key}</div>'


def highlights_html(items):
    """The highlights, each a short bold headline over one supporting sentence."""
    rows = "".join(f'<div class="cd-item"><b>{_e(head)}</b><br>{_e(support)}</div>' for head, support in items)
    return f'<div class="cd"><p class="cd-h">Highlights</p>{rows}</div>'


def band_buttons_css(key, bands):
    """A coloured dot in front of each band button (st.pills with this `key`, in this order), the same colour as that band's dots
    on the map."""
    rules = "".join(f'.st-key-{key} button:nth-of-type({i}):before {{ content:""; display:inline-block; width:12px; height:12px; '
                    f'border-radius:50%; background:{MAP_COLORS.get(band, "#8a8f98")}; margin-right:7px; }}'
                    for i, band in enumerate(bands, 1))
    return f"<style>.st-key-{key} button {{ font-size:15px; }}{rules}</style>"
