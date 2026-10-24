"""The My spots page: the sidebar, loading spots, the map and the session report."""
import numpy as np
import pandas as pd
import streamlit as st

import session_report as sr
from charts import _scorecard_display, bearing_chart
from compare_stats import locate as skimmer_location
from data_sources import skimmer_data
from maps import _single_points, build_map, compute_stats
from pdfs import single_report_pdf
from ui_common import BAND_COLORS, KM_PER_MILE, RBN_SNR_SCALE, WSPR_SNR_SCALE, _views
from views import inputs
from world_map import MIN_CELL, azimuth_map_figure


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
    map_col.pyplot(azimuth_map_figure(home[0], home[1], pts, {"A": "", "B": ""}, callsign, units, radius, layer, show_dots, scale,
                                    label_min=3 if noun == "skimmer" else 5),
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
    st.title("\U0001F4E1 RBN Signal Mapper")
    st.markdown('<p class="subtitle">Map the Reverse Beacon Network and WSPR stations that heard you, and see how strong '
                'your signal was and in which directions. To find out which of two antennas does better, open '
                '<b>Compare antennas</b> in the menu.</p>', unsafe_allow_html=True)

    skimmers, refresh_msg = skimmer_data()
    ss, cfg, pick = inputs.begin()
    with st.sidebar:
        where = inputs.where_inputs(ss, cfg)
        load = st.button("Load spots", type="primary", width="stretch")
        st.button("Try an example", width="stretch", on_click=inputs.start_example, args=("map",),
                  help="Loads K5OHY's real Reverse Beacon Network spots from 23 October 2025, so you can see what the map and reports look like.")
        st.button("Start over", type="tertiary", on_click=inputs.start_over,
                  help="Clears the loaded spots and goes back to the latest date. Your callsign and grid stay.")
        where.load = inputs.take_autoload(ss, load)
        st.divider()
        view = inputs.view_controls(ss, cfg, pick, where.kind)
        st.caption(f"\U0001F6F0\ufe0f {refresh_msg}")
    inputs.remember(ss, cfg, where, view)
    inputs.load_spots(ss, skimmers, where)
    if ss.raw is None:
        st.info("\U0001F448 Enter your callsign and click **Load spots**. "
                "The map is centered on your callsign's registered location unless you enter a grid square. "
                "Just looking around? Click **Try an example** to see real spots.")
        st.button("Try an example", key="hero_map", type="primary", on_click=inputs.start_example, args=("map",),
                  help="Loads real RBN spots, so you can see what the map and reports look like.")
        return
    if inputs.nothing_found_message(ss):
        return
    band_choice, units, tiles, show_all = view.band, view.units, view.tiles, view.show_all

    wspr = ss.kind == "WSPR"
    locs = ss.locs if wspr else skimmers
    scale = WSPR_SNR_SCALE if wspr else RBN_SNR_SCALE
    noun = "receiver" if wspr else "skimmer"
    spots = ss.raw
    spots = spots[(spots["time"].dt.time >= view.start_t) & (spots["time"].dt.time <= view.end_t) & (spots["snr"] >= view.min_snr)]
    all_bands = spots
    if band_choice != "All":
        spots = spots[spots["band"] == band_choice]
    if spots.empty:
        st.warning("No spots match the current filters. Set **Band** to All, or widen the UTC time window in the sidebar.")
        return

    lat, lon, label = ss.home
    home = (lat, lon)
    who = ss.callsign
    if ss.notice:
        st.warning(ss.notice)
    inputs.example_banner(ss)
    if wspr and ss.loaded_at is not None:
        st.caption(f"\U0001F552 Loaded at {ss.loaded_at:%H:%M} UTC. The newest transmission in it is at "
                   f"{ss.raw['time'].max():%H:%M} UTC. Sent something since? Click **Load spots** again.")
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
        left, middle, csv_col, right = st.columns([1, 1, 1, 2])
        left.download_button("⬇️ Download map", map_html, f"RBN_map_{ss.callsign}_{ss.file_date}.html",
                             "text/html", width="stretch")
        csv_col.download_button("⬇️ Spots (CSV)", spots.sort_values("time").to_csv(index=False), 
                                f"RBN_spots_{ss.callsign}_{ss.file_date}.csv", "text/csv", width="stretch",
                                help="The spots on the map, with the band and time filters applied, for a spreadsheet.")
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
