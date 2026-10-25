"""The PDF reports, laid out from the charts and tables."""
import re

import pandas as pd

import session_report as sr
from charts import (
    COLOR_A,
    COLOR_B,
    COLOR_TIE,
    _breakdown_rows,
    _scorecard_display,
    bearing_chart,
    direction_advantage_chart,
    direction_chart,
    distance_chart,
    paired_scatter,
    relative_gain_chart,
    timeline_chart,
)
from compare_stats import BIN_MIN_SHARED, receiver_table
from maps import _single_points, compute_stats
from report_pdf import INK, build_pdf
from ui_common import BAND_COLORS, KM_PER_MILE, _pretty_date
from world_map import MAX_KM, azimuth_map_figure


def comparison_headline(names, bands):
    """The report's title as coloured pieces, one list per line: 'Loop (A)' in A's colour, then 'vs Dipole (B) on 20 m' with B in
    its colour, so the colours decode A and B the same way they do in every chart."""
    short = lambda name: name if len(name) <= 40 else name[:39].rstrip() + "\u2026"  # noqa: E731
    on = "   on " + re.sub(r"(\d)m\b", r"\1 m", bands) if bands else ""
    if names["A"] == "A" and names["B"] == "B":
        return [[("A", COLOR_A), (" vs ", INK), ("B", COLOR_B), (on, INK)]]
    return [[(f"{short(names['A'])} (A)", COLOR_A)], [("vs ", INK), (f"{short(names['B'])} (B)", COLOR_B), (on, INK)]]


def compare_report_pdf(ctx):
    """A/B comparison as a PDF: the verdict, scoreboard, who heard you, direction and distance, receiver by receiver."""
    names, an, verdict, noun, units = ctx["names"], ctx["an"], ctx["verdict"], ctx["noun"], ctx["units"]
    colors = {"A": COLOR_A, "B": COLOR_B, None: COLOR_TIE}
    r, s_ = an["reach"], an["strength"]
    sub = [f"{ctx['callsign']} · {ctx['label']}", f"{ctx['source']} · {ctx['bands']} · {_pretty_date(ctx['file_date'])}", ctx["test"]]
    if names["A"] != "A" or names["B"] != "B":
        sub.append(f"A = {names['A']}    B = {names['B']}")
    setup = ctx.get("setup")
    blocks = ([("heading", "Test setup"), ("table", ["Setting", "Value"], [[k, v] for k, v in setup["rows"]], [0.3, 0.7])]
              + ([("heading", "Schedule"), ("table", *setup["table"])] if setup["table"] else [])
              + ([("note", setup["note"])] if setup["note"] else []) if setup else [])
    blocks += [("heading", "Result"), ("verdict", verdict, colors),
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
                                                      units, MAX_KM, "advantage", True, ctx["scale"],
                                                      label_min=3 if ctx["noun"] == "skimmer" else 5), 5.6),
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
        blocks += [("heading", f"{noun.capitalize()} by {noun}"),
                   *([("paragraph", ctx["settings"])] if ctx.get("settings") else []),
                   ("figure", paired_scatter(ctx.get("scatter_src", an["shared"]), names), 3.8),
                   ("note", f"{len(every)} {noun}s heard both sides, shown farthest first (up to 40). "
                            + ctx.get("rx_note", "Each SNR is the median of that station's spots.")),
                   ("table", [noun.capitalize(), f"Distance ({units})", "A SNR", "B SNR", "Stronger", "B \u2212 A (dB)"],
                    rows[:40], [0.28, 0.2, 0.13, 0.13, 0.13, 0.13]),
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
                      ("note", f"The average of B minus A at the {noun}s in each group, each {noun} counting once, with "
                               f"a 95% range. A group with fewer than {BIN_MIN_SHARED} {noun}s is a hint, not a result.")]
                     if ctx.get("gain_summary") else [])]
    if ctx["method"]:
        blocks += [("heading", "Linear average"), ("paragraph", ctx["method"]),
                   ("note", "The default compares the same receiver in neighbouring transmissions, so it suits a short alternating test. "
                            "The power average is a second measure for longer, well-balanced runs and needs many reports "
                            "per receiver. If they disagree a lot, find out why before drawing a conclusion.")]
    if ctx["timeline"] is not None:
        blocks += [("heading", "Timeline"), ("figure", timeline_chart(*ctx["timeline"], names), 3.0)]
    meta = {"title": "Antenna comparison report", "callsign": ctx["callsign"], "subtitle": sub,
            "headline": comparison_headline(names, ctx["bands"])}
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
