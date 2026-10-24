"""The sidebar controls for choosing and loading spots, and the loading itself, shared by the My spots and Compare pages."""
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

import example_data
from data_sources import download_rbn_day, load_wspr, parse_pasted_data, resolve_home
from ui_common import (
    BAND_COLORS,
    MAX_DAYS,
    RBN_SOURCES,
    SOURCE_CODES,
    TILE_STYLES,
    WSPR_SOURCE,
    load_settings,
    save_settings,
)
from wspr_data import clean_callsign, receiver_locations

DATA_KEYS = ("raw", "home", "callsign", "file_date", "kind", "locs", "tx_call", "notice", "loaded_at")
SHARED_WIDGET_KEYS = ("callsign_in", "grid_in", "src_in", "net_in", "how_in", "span_in", "day_in", "range_in", "from_in", "to_in", "units_in", "band_choice")


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
    day = q.get("date", "")
    first, _, last = day.partition("..")
    if _parse_day(first) and _parse_day(last):
        ss["span_in"], ss["from_in"], ss["to_in"] = "Date range", _parse_day(first), _parse_day(last)
    elif _parse_day(first):
        ss["span_in"], ss["day_in"] = "Single day", _parse_day(first)
    if q.get("band") in ["All"] + list(BAND_COLORS):
        ss["band_choice"] = q["band"]
    if q.get("go") == "1" and ss.get("callsign_in"):
        ss["_autoload"] = True


def _link_for(ss, callsign, grid, source, span, days, units, band, extra=None):
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
    link.update(extra or {})
    return link


def _load_saved(which):
    """Puts a saved example's real spots into the session, as if Load spots had just fetched them. False when the example's files
    are missing, and the caller then loads it from the internet instead."""
    saved = example_data.load(which)
    if saved is None:
        return False
    meta, spots = saved
    ss = st.session_state
    ss.raw, ss.callsign, ss.kind, ss.tx_call = spots, meta["callsign"], meta["kind"], meta["tx_call"]
    ss.locs = (receiver_locations(spots) if len(spots) else {}) if meta["kind"] == "WSPR" else None
    ss.file_date, ss.home, ss.notice, ss.loaded_at = meta["file_date"], tuple(meta["home"]), None, None
    ss["setup_ok"] = True
    return True


def start_example(which):
    """The example buttons, each set up and loaded so the result shows straight away. 'map' is K5OHY's Reverse Beacon Network
    spots of 23 Oct 2025 on the My spots page. On the Compare page, 'rbn' is a quick CW check by frequency (27 Sep 2026, loop on 14069.5 against dipole on 14070.5), 'quick' is the same day's test of 12 transmissions alternating a 40m loop and a
    20m dipole, and 'long' is Dale N3HXZ's test of 26 Aug 2026: a Performer (tuned elevated quarter-wave) against an untuned 17 ft whip on a 5:1 unun at ground level,
    alternating 30 minute blocks from 12:00 to 21:30 UTC, judged on stations with 40 or more reports per antenna as in his
    write-up."""
    ss = st.session_state
    if which == "long":
        ss.update(callsign_in="N3HXZ", grid_in="", src_in=WSPR_SOURCE, span_in="Single day", day_in=date(2026, 8, 26),
                  band_choice="20m", cmp_name_a="17 ft whip + 5:1 unun (ground)", cmp_name_b="Performer (tuned, elevated)",
                  test_mode="In long blocks", rx_min_reports_blk=40, _example="long")
    elif which == "rbn":
        ss.update(callsign_in="K5OHY", grid_in="", src_in="Download by date", span_in="Single day", day_in=date(2026, 9, 27),
                  band_choice="20m", cmp_name_a="40m loop, 20 ft", cmp_name_b="20m dipole, 40 ft", _example="rbn")
    elif which == "quick":
        ss.update(callsign_in="K5OHY", grid_in="", src_in=WSPR_SOURCE, span_in="Single day", day_in=date(2026, 10, 4),
                  band_choice="20m", cmp_name_a="40m loop, 20 ft", cmp_name_b="20m dipole, 40 ft",
                  test_mode="After every transmission", test_cycles=12, test_start="18:00", test_order="A, B, B, A …",
                  _example="quick")
    else:
        ss.update(callsign_in="K5OHY", grid_in="", src_in="Download by date", span_in="Single day", day_in=date(2025, 10, 23),
                  band_choice="All", _example="map")
    if not _load_saved(which):  # no saved copy: fetch it live, as a click on Load spots would
        ss["_autoload"] = True


_SETUP_KEYS = ("cmp_name_a", "cmp_name_b", "test_mode", "test_cycles", "test_start", "test_order", "avg_floor", "avg_weak",
               "setup_ok", "_setup_given", "_blk_pending", "_log_pending", "_log_base", "_example", "_autoload")
_SETUP_PREFIXES = ("blk_", "antenna_log_", "avg_near_", "rx_min_reports_")


def start_over(clear_callsign=False):
    """Back to a clean page: the loaded spots, the dates (to the latest), the band and a comparison's setup are cleared. Your
    callsign and grid stay unless `clear_callsign`. The source (RBN or WSPR), units and map settings stay."""
    ss = st.session_state
    for key in DATA_KEYS:
        ss[key] = None
    for key in list(ss.keys()):
        if key in _SETUP_KEYS or (isinstance(key, str) and key.startswith(_SETUP_PREFIXES)):
            del ss[key]
    for key in ("day_in", "from_in", "to_in", "range_in", "_range_seen"):  # the next run sets them to the latest day
        ss.pop(key, None)
    ss.update(span_in="Single day", band_choice="All")
    if clear_callsign:
        ss.update(callsign_in="", grid_in="")
    st.query_params.clear()


def example_banner(ss):
    """A note above the results explaining a loaded example, or nothing."""
    which = ss.get("_example")
    if which == "long":
        st.info("This is an example: a real long WSPR test by Dale, N3HXZ, on 26 August 2026, at 1 W on 20m. A "
                "Performer, a tuned elevated quarter-wave with two raised radials (B), and an untuned 17 ft whip on a 5:1 matching unit, ground-mounted "
                "with two radials (A), took "
                "turns in 30 minute blocks from 12:00 to 21:30 UTC. As in his write-up, only stations with 40 or more reports on "
                "each antenna are used (the slider on the Receiver by receiver tab). It was set up as 30 minute blocks from 12:00 UTC, "
                "A first. Dale publishes the study on his site, [n3hxz.net](https://www.n3hxz.net/). To try your own, enter your "
                "callsign and a date and click Load spots.")
    elif which == "quick":
        st.info("This is an example: a real WSPR test from K5OHY on 4 October 2026. A 40m loop (A) and a 20m dipole (B) "
                "took turns on 12 two-minute transmissions, and the result says which got out better. It was set up as 12 transmissions "
                "from 18:00 UTC in the order A, B, B, A. To try your own, enter your callsign and a date and click Load spots.")
    elif which == "rbn":
        st.info("This is an example: a quick CW check from K5OHY on 27 September 2026, on the Reverse Beacon Network. A 40m loop "
                "at 20 ft (A) called CQ on 14069.5 kHz and a 20m dipole at 40 ft (B) on 14070.5 kHz, back to back, and the "
                "skimmers that heard each are compared. RBN can only tell the two apart by frequency, so WSPR is the better test. "
                "To try your own, enter your callsign and a date and click Load spots.")
    elif which == "map":
        st.info("This is an example: the Reverse Beacon Network stations that heard K5OHY's CW calls on 23 October 2025. To see "
                "your own, enter your callsign and a date and click Load spots.")
    if which and example_data.available(which):
        st.caption("Opened from a saved copy of the real data, so it appears at once. Your own spots are downloaded live.")
    if which:
        st.button("Use my own data instead", key="banner_own", on_click=start_over, kwargs={"clear_callsign": True},
                  help="Clears the example and the callsign and dates it filled in, so you can enter your own.")


def begin(default_source=None):
    """Sets up the session for a page: the saved settings, a shared link, and what you chose on the other page. Returns
    (session_state, settings, pick) where pick(options, key) is the index of a saved choice in `options`."""
    ss = st.session_state
    for key in DATA_KEYS:
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

    for key in SHARED_WIDGET_KEYS:  # Streamlit forgets a widget's value when you leave its page; this keeps it
        if key in ss:
            ss[key] = ss[key]
    _fill_from_link(ss)
    sources = RBN_SOURCES + [WSPR_SOURCE]
    ss.setdefault("grid_in", cfg.get("grid", ""))
    ss.setdefault("src_in", cfg["source"] if cfg.get("source") in sources else (default_source or sources[0]))
    ss.setdefault("span_in", ["Single day", "Date range"][pick(["Single day", "Date range"], "span")])
    ss.setdefault("units_in", ["mi", "km"][pick(["mi", "km"], "units")])
    ss.setdefault("callsign_in", cfg.get("callsign", ""))
    return ss, cfg, pick


HOW_OPTIONS = ("Download a day", "Paste spots")


def _source_choice(ss):
    """'Spots from: RBN | WSPR', then for RBN 'Download a day | Paste spots'. The three-way choice the rest of the app uses
    stays in `src_in`; a link, an example or the other page can set it, and the two radios follow."""
    now = ss.get("src_in")
    if ss.get("_src_seen") != now:
        ss["net_in"] = "WSPR" if now == WSPR_SOURCE else "RBN"
        if now != WSPR_SOURCE:
            ss["how_in"] = HOW_OPTIONS[1] if now == "Paste from RBN site" else HOW_OPTIONS[0]
    net = st.radio("Spots from", ["RBN", "WSPR"], key="net_in", horizontal=True,
                   help="RBN: stations that decoded your CW/RTTY CQ. WSPR: stations that decoded your WSPR "
                        "beacon, handy for antenna tests because the power is fixed and known.")
    if net == "WSPR":
        source = WSPR_SOURCE
    else:
        ss.setdefault("how_in", ss.get("_how_last", HOW_OPTIONS[0]))
        how = st.radio("How to get them", HOW_OPTIONS, key="how_in", horizontal=True,
                       help="Download needs the day to be over (UTC). Paste works for today: copy the spots from reversebeacon.net.")
        ss["_how_last"] = how
        source = "Paste from RBN site" if how == HOW_OPTIONS[1] else "Download by date"
    ss["src_in"] = ss["_src_seen"] = source
    return source


def where_inputs(ss, cfg):
    """The sidebar's 'Your signal' controls: callsign, grid, where the spots come from and for which dates. Call it inside
    `with st.sidebar:`. Returns what was chosen; the Load button is the page's to draw."""
    st.header("Your signal")
    callsign = st.text_input(
        "Callsign", key="callsign_in", placeholder="Enter your callsign",
        on_change=lambda: ss.update(callsign_in=ss.callsign_in.strip().upper())).strip().upper()
    grid_override = st.text_input(
        "Grid square (optional)", key="grid_in", placeholder="Looked up from your callsign",
        help="Leave blank to use your callsign's registered address. "
             "Enter a grid if you were portable or operating from elsewhere, or if the lookup fails.")

    source = _source_choice(ss)
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
            # One calendar picks both ends: click the first day, then the last. A link or example sets from_in and to_in, and
            # the calendar follows them.
            if ss.get("_range_seen") != (ss["from_in"], ss["to_in"]):
                ss["range_in"] = (ss["from_in"], ss["to_in"])
            picked = st.date_input("Dates (UTC)", key="range_in", max_value=last_day,
                                   help=f"Click the first day, then the last. Up to {MAX_DAYS} days." + (
                                       " Each day is a separate download." if source != WSPR_SOURCE else ""))
            picked = tuple(picked) if isinstance(picked, (list, tuple)) else (picked,)
            first, last = picked[0], picked[-1]  # until the second click it is a single day
            ss["from_in"], ss["to_in"] = first, last
            ss["_range_seen"] = (first, last)
            days = [first + timedelta(n) for n in range((last - first).days + 1)]
            if len(days) > MAX_DAYS:
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
    # What the screen shows follows the data that is loaded, not the radio button, until you load again.
    kind = ss.kind if ss.raw is not None else ("WSPR" if source == WSPR_SOURCE else "RBN")
    return SimpleNamespace(callsign=callsign, grid=grid_override, source=source, span=span, days=days, pasted=pasted,
                           wspr_start=wspr_start, wspr_end=wspr_end, kind=kind, load=False)


def take_autoload(ss, load):
    """True when Load spots was clicked, or an example or shared link asked for the spots to load. Clicking Load yourself
    ends an example."""
    if ss.pop("_autoload", False):
        return True
    if load:
        ss["_example"] = False
    return load


def view_controls(ss, cfg, pick, kind, comparing=False):
    """The 'Filters' section of the sidebar and the 'More settings' box under it. Call it inside `with st.sidebar:`. When comparing WSPR spots you pick
    your test's cycles on the page, so the time window is not offered. Returns what was chosen."""
    st.header("Filters")
    bands = ["All"] + list(BAND_COLORS)
    ss.setdefault("band_choice", bands[pick(bands, "band")])
    band_choice = st.selectbox("Band", bands, key="band_choice",
                               help="A WSPR antenna test has to be judged one band at a time." if comparing else None)
    lo_t, hi_t = time(0, 0), time(23, 59)
    if kind == "WSPR" and ss.raw is not None and not ss.raw.empty:
        # The slider covers just the span of the spots loaded rather than all day, and starts out showing all of it.
        tod = ss.raw["time"].dt.floor("min").dt.time
        if tod.min() < tod.max():
            lo_t, hi_t = tod.min(), tod.max()
    if kind == "WSPR" and comparing:
        start_t, end_t = time.min, time.max
    else:
        start_t, end_t = st.slider(
            "UTC time window", min_value=lo_t, max_value=hi_t, value=(lo_t, hi_t), step=timedelta(minutes=1),
            format="HH:mm",
            help=("Only show spots between these times. It follows the spots you loaded." if kind == "WSPR" else None))
    min_snr = -100 if kind == "WSPR" else st.slider("Minimum SNR (dB)", 0, 40, cfg.get("min_snr", 0))

    with st.expander("More settings"):
        styles = list(TILE_STYLES)
        tiles = st.selectbox("Map style", styles, index=pick(styles, "tiles"))
        show_all = st.checkbox("Show all skimmers", value=cfg.get("show_all", False) and kind == "RBN", disabled=kind == "WSPR",
                               help="Adds a small grey dot for every RBN skimmer, including ones that didn't hear you. "
                                    "(Not available for WSPR, which has no list of every receiver.)")
        units = st.radio("Distance units", ["mi", "km"], key="units_in", horizontal=True)
    return SimpleNamespace(band=band_choice, start_t=start_t, end_t=end_t, min_snr=min_snr, tiles=tiles, show_all=show_all,
                           units=units)


def remember(ss, cfg, where, view, extra=None, slot=None):
    """Keeps the web address and the saved settings in step with the sidebar, and shows the link once spots are loaded.
    `extra` adds parameters to the link (a Compare test's setup); `slot` is the sidebar container to draw the link in, so a page
    that knows its extras only after drawing its results can call this last."""
    link = _link_for(ss, where.callsign, where.grid, where.source, where.span, where.days, view.units, view.band, extra)
    if where.callsign and dict(st.query_params) != link:
        st.query_params.from_dict(link)
    if where.callsign and ss.raw is not None:
        with (slot if slot is not None else st.sidebar):
            st.caption("\U0001F517 Link to this view")
            st.code((st.context.url or "").split("?")[0] + "?" + urlencode(link), language=None)
    save_settings({"callsign": where.callsign, "grid": where.grid, "source": where.source, "span": where.span, "band": view.band,
                   "min_snr": cfg.get("min_snr", 0) if where.kind == "WSPR" else view.min_snr,
                   "min_snr_wspr": cfg.get("min_snr_wspr", -30),
                   "tiles": view.tiles, "show_all": view.show_all if where.kind == "RBN" else cfg.get("show_all", False),
                   "units": view.units})


def load_spots(ss, skimmers, where):
    """Loads the spots for what the sidebar says when Load was clicked (or an example or link asked for it), into the session."""
    if not where.load:
        return
    try:
        if not where.callsign:
            raise RuntimeError("Enter a callsign first.")
        if len(where.days) > MAX_DAYS:
            raise RuntimeError(f"Please pick {MAX_DAYS} days or fewer.")
        ss.home = resolve_home(where.callsign, where.grid, skimmers)
        locs, tx_call = None, None
        ss.notice = None
        notice = None
        days = where.days
        if where.source == WSPR_SOURCE:
            if not days:
                raise RuntimeError("The 'To' date must be on or after the 'From' date.")
            tx_call = clean_callsign(where.callsign)
            with st.spinner(f"Looking up {tx_call}'s WSPR spots…"):
                df, truncated = load_wspr(tx_call, where.wspr_start, where.wspr_end)
            if truncated:
                notice = "That much hit the spot limit, so the oldest spots are missing. Pick fewer days."
            locs = receiver_locations(df) if not df.empty else {}
            ss.file_date = f"{days[0]:%Y%m%d}" + (f"-{days[-1]:%Y%m%d}" if len(days) > 1 else "")
        elif where.source == "Paste from RBN site":
            if not where.pasted.strip():
                raise RuntimeError("Paste some RBN spot rows, or switch to 'Download by date'.")
            df = parse_pasted_data(where.pasted)
            df = df[df["dx"] == where.callsign] if (df["dx"] == where.callsign).any() else df
            ss.file_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        else:
            if not days:
                raise RuntimeError("The 'To' date must be on or after the 'From' date.")
            frames, failed = [], []
            bar = st.progress(0.0, text="Downloading RBN data… (each day can take a minute)")
            for i, d in enumerate(days):
                bar.progress(i / len(days), text=f"Downloading {d:%Y-%m-%d} ({i + 1} of {len(days)})…")
                try:
                    frames.append(download_rbn_day(d.strftime("%Y%m%d"), where.callsign))
                except Exception as e:
                    failed.append(f"{d:%Y-%m-%d}: {e}")
            bar.empty()
            if not frames:
                raise RuntimeError("Download failed. " + " | ".join(failed))
            for msg in failed:
                st.warning(f"Skipped {msg}")
            df = pd.concat(frames, ignore_index=True)
            ss.file_date = f"{days[0]:%Y%m%d}" + (f"-{days[-1]:%Y%m%d}" if len(days) > 1 else "")
        ss.raw, ss.callsign = df, where.callsign
        ss.kind, ss.locs, ss.tx_call = ("WSPR" if where.source == WSPR_SOURCE else "RBN"), locs, tx_call
        ss.notice = notice
        ss.loaded_at = datetime.now(timezone.utc)
        # An example, or a link that carries the test's setup, needs no checking. Your own spots do: the page has guessed.
        ss["setup_ok"] = bool(ss.get("_example")) or bool(ss.pop("_setup_given", False))
        st.rerun()  # redraw the sidebar, so the time window and the link to this view follow the spots just loaded
    except Exception as e:
        ss.raw = None
        st.error(str(e))


def nothing_found_message(ss):
    """If the loaded spots are empty, says so with a next step and returns True."""
    if ss.raw is None or not ss.raw.empty:
        return False
    if ss.kind == "WSPR":
        st.warning(f"There are no WSPR spots from {ss.tx_call} in that time range. Next steps: check that the callsign is "
                   "exactly what your WSPR software sends, wait a few minutes if you have only just transmitted, or try a "
                   "wider range of dates. **Try an example** in the sidebar shows what a result looks like.")
    else:
        st.warning(f"RBN has no spots of {ss.callsign} for that date. Next steps: check the callsign, try another day, "
                   "or, for today, copy your spots from reversebeacon.net and choose **Paste from RBN site** (the daily "
                   "files are published after the UTC day ends).")
    return True
