"""The My spots page: the sidebar, loading spots, the map and the session report."""
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import streamlit as st

import session_report as sr
from charts import _scorecard_display, bearing_chart
from compare_stats import locate as skimmer_location
from data_sources import download_rbn_day, load_wspr, parse_pasted_data, resolve_home, skimmer_data
from maps import _single_points, build_map, compute_stats
from pdfs import single_report_pdf
from ui_common import (
    BAND_COLORS,
    KM_PER_MILE,
    MAX_DAYS,
    RBN_SNR_SCALE,
    RBN_SOURCES,
    SOURCE_CODES,
    SPLIT_FREQ,
    SPLIT_LOG,
    TILE_STYLES,
    WSPR_SNR_SCALE,
    WSPR_SOURCE,
    _views,
    load_settings,
    save_settings,
)
from views.compare_view import compare_view
from world_map import MIN_CELL, azimuth_map_figure
from wspr_data import clean_callsign, receiver_locations


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


def _parse_day(text):
    """A date from a link such as 2026-10-04, or None."""
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _fill_from_link(ss):
    """Once per visit, set the sidebar from the web address, so a shared link opens on the same view. Anything missing or
    unreadable is ignored. A link with go=1 also loads the spots straight away."""
    if ss.get("_link_read"):
        return
    ss["_link_read"] = True
    q = st.query_params
    if q.get("call"):
        ss["callsign_in"] = q["call"].strip().upper()[:12]
    if "grid" in q:
        ss["grid_in"] = q["grid"][:8]
    if q.get("src") in SOURCE_CODES:
        ss["src_in"] = SOURCE_CODES[q["src"]]
    if q.get("units") in ("mi", "km"):
        ss["units_in"] = q["units"]
    date = q.get("date", "")
    first, _, last = date.partition("..")
    if _parse_day(first) and _parse_day(last):
        ss["span_in"], ss["from_in"], ss["to_in"] = "Date range", _parse_day(first), _parse_day(last)
    elif _parse_day(first):
        ss["span_in"], ss["day_in"] = "Single day", _parse_day(first)
    if q.get("band") in ["All"] + list(BAND_COLORS):
        ss["band_choice"] = q["band"]
    if q.get("go") == "1" and ss.get("callsign_in"):
        ss["_autoload"] = True


def _link_for(ss, callsign, grid, source, span, days, units, band, compare):
    """The web address that reopens what is on screen: the sidebar choices, and go=1 when spots are loaded."""
    link = {"call": callsign, "src": next(k for k, v in SOURCE_CODES.items() if v == source), "units": units}
    if grid.strip():
        link["grid"] = grid.strip()
    if days and source != "Paste from RBN site":
        link["date"] = f"{days[0]:%Y-%m-%d}" + (f"..{days[-1]:%Y-%m-%d}" if span == "Date range" and len(days) > 1 else "")
    if band != "All":
        link["band"] = band
    if ss.get("raw") is not None and source != "Paste from RBN site":  # pasted spots can't be put in a link
        link["go"] = "1"
    return link


def _start_example(which):
    """The example buttons, each set up and loaded so the result shows straight away. 'quick' is K5OHY's WSPR test of 4 Oct
    2026: 12 transmissions alternating a 40m loop and a 20m dipole. 'long' is Dale Ostergaard N3HXZ's test of 26 Aug 2026: a
    POTA Performer against a Chameleon MPAS Lite system, alternating 30 minute blocks from 12:00 to 21:30 UTC, judged on stations with
    40 or more reports per antenna as in his write-up."""
    ss = st.session_state
    if which == "long":
        ss.update(callsign_in="N3HXZ", grid_in="", src_in=WSPR_SOURCE, span_in="Single day", day_in=date(2026, 8, 26),
                  band_choice="20m", compare_WSPR=True, cmp_name_a="Chameleon MPAS Lite (ground-mounted)", cmp_name_b="POTA Performer (elevated)",
                  test_mode="In long blocks", rx_min_reports_blk=40, _autoload=True, _example="long")
    else:
        ss.update(callsign_in="K5OHY", grid_in="", src_in=WSPR_SOURCE, span_in="Single day", day_in=date(2026, 10, 4),
                  band_choice="20m", compare_WSPR=True, cmp_name_a="40m loop, 20 ft", cmp_name_b="20m dipole, 40 ft",
                  test_mode="After every transmission", test_cycles=12, test_start="18:00", test_order="A, B, B, A \u2026",
                  _autoload=True, _example="quick")


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

    _fill_from_link(ss)
    sources = RBN_SOURCES + [WSPR_SOURCE]
    ss.setdefault("grid_in", cfg.get("grid", ""))
    ss.setdefault("src_in", sources[pick(sources, "source")])
    ss.setdefault("span_in", ["Single day", "Date range"][pick(["Single day", "Date range"], "span")])
    ss.setdefault("units_in", ["mi", "km"][pick(["mi", "km"], "units")])
    with st.sidebar:
        st.header("Your signal")
        ss.setdefault("callsign_in", cfg.get("callsign", ""))
        callsign = st.text_input(
            "Callsign", key="callsign_in", placeholder="Enter your callsign",
            on_change=lambda: ss.update(callsign_in=ss.callsign_in.strip().upper())).strip().upper()
        grid_override = st.text_input(
            "Grid square (optional)", key="grid_in", placeholder="Looked up from your callsign",
            help="Leave blank to use your callsign's registered address. "
                 "Enter a grid if you were portable or operating from elsewhere, or if the lookup fails.")

        source = st.radio("Where are the spots from?", sources, key="src_in",
                          help="RBN: stations that decoded your CW/RTTY CQ. WSPR: stations that decoded your WSPR "
                               "beacon, handy for antenna tests because the power is fixed and known.")
        wspr_start, wspr_end = None, None
        pasted, days, span = "", [], cfg.get("span")
        if source in ("Download by date", WSPR_SOURCE):
            # RBN publishes a day's file once the UTC day is over; WSPR spots are there up to a few minutes ago
            last_day = (datetime.now(timezone.utc) - timedelta(days=0 if source == WSPR_SOURCE else 1)).date()
            ss.setdefault("day_in", last_day)
            ss.setdefault("from_in", last_day - timedelta(days=MAX_DAYS - 1))
            ss.setdefault("to_in", last_day)
            for key in ("day_in", "from_in", "to_in"):  # a date from the other source, or a link, may be past the last day
                if ss[key] > last_day:
                    ss[key] = last_day
            span = st.radio("Period", ["Single day", "Date range"], horizontal=True, key="span_in")
            if span == "Single day":
                days = [st.date_input("Date (UTC)", key="day_in", max_value=last_day)]
            else:
                first = st.date_input("From (UTC)", key="from_in", max_value=last_day)
                last = st.date_input("To (UTC)", key="to_in", max_value=last_day,
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
        st.button("Try an example", width="stretch", on_click=_start_example, args=("quick",),
                  help="Loads a real WSPR antenna test, a 40m loop against a 20m dipole, so you can see what the results look like.")
        st.button("Try a long-test example", width="stretch", on_click=_start_example, args=("long",),
                  help="Loads Dale Ostergaard's long WSPR test of 26 August 2026: a POTA Performer against a Chameleon MPAS Lite, "
                       "switched every 30 minutes for nine and a half hours.")
        if ss.pop("_autoload", False):
            load = True
        elif load:
            ss["_example"] = False

        # What the screen shows follows the data that is loaded, not the radio button, until you load again.
        kind = ss.kind if ss.raw is not None else ("WSPR" if source == WSPR_SOURCE else "RBN")

        st.divider()
        st.header("Compare mode")
        compare = st.checkbox("Compare two antennas or tests", key=f"compare_{kind}",
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
        units = st.radio("Distance units", unit_opts, key="units_in", horizontal=True)

        st.caption(f"🛰️ {refresh_msg}")

    link = _link_for(ss, callsign, grid_override, source, span, days, units, band_choice, compare)
    if callsign and dict(st.query_params) != link:
        st.query_params.from_dict(link)
    if callsign and ss.raw is not None:
        with st.sidebar:
            st.caption("\U0001F517 Link to this view")
            st.code((st.context.url or "").split("?")[0] + "?" + urlencode(link), language=None)
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
                "The map is centered on your callsign's registered location unless you enter a grid square. "
                "Just looking around? Click **Try an example** to see a real antenna test.")
        return
    wspr = ss.kind == "WSPR"
    if ss.raw.empty:
        if wspr:
            st.warning(f"There are no WSPR spots from {ss.tx_call} in that time range. Next steps: check that the callsign is "
                       "exactly what your WSPR software sends, wait a few minutes if you have only just transmitted, or try a "
                       "wider range of dates. **Try an example** in the sidebar shows what a result looks like.")
        else:
            st.warning(f"RBN has no spots of {ss.callsign} for that date. Next steps: check the callsign, try another day, "
                       "or, for today, copy your spots from reversebeacon.net and choose **Paste from RBN site** (the daily "
                       "files are published after the UTC day ends).")
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
        st.warning("No spots match the current filters. Set **Band** to All, or widen the UTC time window in the sidebar.")
        return

    lat, lon, label = ss.home
    home = (lat, lon)
    who = ss.callsign
    if ss.notice:
        st.warning(ss.notice)
    if ss.get("_example") == "long":
        st.info("This is an example: a real long WSPR test by Dale Ostergaard, N3HXZ, on 26 August 2026, at 1 W on 20m. A POTA "
                "Performer elevated vertical (B) and a Chameleon MPAS Lite ground-mounted system with its matching unit (A) took turns in 30 minute blocks from "
                "12:00 to 21:30 UTC. As in his write-up, only stations with 40 or more reports on each antenna are used (the "
                "slider on the Receiver by receiver tab). Dale publishes the study on his site, [n3hxz.net](https://www.n3hxz.net/). "
                "To try your own, enter your callsign and a date and click Load spots.")
    elif ss.get("_example"):
        st.info("This is an example: a real WSPR test from K5OHY on 4 October 2026. A 40m loop (A) and a 20m dipole (B) "
                "took turns on 12 two-minute transmissions, and the result below says which got out better. To try your own, "
                "enter your callsign and a date and click Load spots.")
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
