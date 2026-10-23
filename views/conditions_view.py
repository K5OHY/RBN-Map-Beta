"""The Band conditions page: the dashboard built from nearby WSPR stations, space weather and the MUF."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

import conditions as cond
import dashboard as dash
import ionosphere as iono
import space_weather as sw
from data_sources import resolve_home, skimmer_data
from ui_common import KM_PER_MILE, _views, load_settings
from world_map import azimuth_map_figure
from wspr_data import WsprError


# ------------------------------------------------------------------------------------------- band conditions

_LOCATE_JS = """
export default function (component) {
  const { parentElement, setTriggerValue, data } = component
  const button = parentElement.querySelector("#locate")
  const note = parentElement.querySelector("#note")
  if (!button || !note) return
  const ask = () => {
    if (!navigator.geolocation) {
      note.textContent = "This browser can't share a location."
      setTriggerValue("error", "unsupported")
      return
    }
    note.textContent = "Asking your browser\u2026"
    navigator.geolocation.getCurrentPosition(
      (p) => {
        note.textContent = ""
        setTriggerValue("position", { lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy })
      },
      (e) => {
        note.textContent = e.code === 1 ? "Location was blocked. Allow it for this site, or use a callsign or grid."
                                        : "Couldn't get a location (" + e.message + ")."
        setTriggerValue("error", e.code)
      },
      { enableHighAccuracy: false, timeout: 20000, maximumAge: 300000 }
    )
  }
  button.onclick = ask
  // The page's own "Use my location" button sends a new ticket; each ticket asks the browser once.
  const ticket = data && data.ticket ? String(data.ticket) : ""
  if (ticket && window.__locateAsked !== ticket) {
    window.__locateAsked = ticket
    ask()
  }
}
"""


_LOCATE_CSS = """
#locate { width: 100%; padding: 0.45rem 0.75rem; border-radius: 0.5rem; cursor: pointer; font: inherit;
          color: var(--st-text-color); background: var(--st-secondary-background-color);
          border: 1px solid var(--st-border-color); }
#locate:hover { border-color: var(--st-primary-color); color: var(--st-primary-color); }
#note { margin-top: 0.35rem; font-size: 0.85rem; color: var(--st-text-color); opacity: 0.75; }
"""


_LOCATE = st.components.v2.component(
    "use_my_location", html='<button id="locate" type="button">\U0001F4CD Use my location</button><div id="note"></div>',
    css=_LOCATE_CSS, js=_LOCATE_JS)


_STATUS = {"Open": "\U0001F7E2 Open", "Marginal": "\U0001F7E1 Marginal", "Quiet": "\u26AA Quiet"}


_TREND = {"opening": "\u2197 opening", "fading": "\u2198 fading", "steady": "\u2192 steady", "new": "\u2197 just started", "unclear": "– too few to tell"}


@st.cache_data(ttl=300, show_spinner=False)
def _nearby_spots(lat, lon, radius_km, end_iso, minutes):
    """Spots from the WSPR stations near a point, cached for five minutes so a change of mode or power costs nothing."""
    end = pd.Timestamp(end_iso).to_pydatetime()
    return cond.fetch_nearby(lat, lon, radius_km, end - timedelta(minutes=minutes), end)


@st.cache_data(ttl=300, show_spinner=False)
def _station_spots(call, end_iso, minutes):
    """Spots from one WSPR transmitter, cached for five minutes."""
    end = pd.Timestamp(end_iso).to_pydatetime()
    return cond.fetch_station(call, end - timedelta(minutes=minutes), end)


def _dx_badge(result, units, k):
    """A short label and tone for the top card: how many receivers outside North America heard the stations near you."""
    n = result["receivers"][result["receivers"]["region"].isin(cond.DX_REGIONS)]["rx"].nunique()
    if n < 5:
        return ("Thin DX evidence", "ta", f"Only {n} receiver{'s' if n != 1 else ''} outside the US and Canada heard these WSPR "
                "signals, so the DX figures rest on very little data. A wider area (up to about 900 miles) or a longer look-back "
                "can add some.")
    return ("DX evidence good", "tg", f"{n} receivers outside the US and Canada heard these WSPR signals.")


def _conditions_body(result, power, mode, margin_db, units, k, lat, lon, who, window=60, area=""):
    """The dashboard: the summary, a tile for each band, the sun and MUF, the regions, the map and the highlights,
    with the detailed tables in tabs underneath."""
    lines = cond.headline(result, power, mode, margin_db, units, window)
    bands, dx = result["bands"], result["dx"]
    data, est = _space_weather(), iono.estimate(lat, lon, _ionosondes())
    badge, badge_tone, badge_help = _dx_badge(result, units, k)
    setup = f"{power:g} W {mode} from {cond.latlon_to_maidenhead(lat, lon)}"
    if len(dx):
        far = dx.iloc[0]
        setup += f" \u00b7 farthest: {far['rx']}, {far['km'] / k:,.0f} {units} on {far['band']}"
    st.html(dash.CSS)
    st.html(dash.top_html(setup, f"{result['n_spots']:,} WSPR spots from {result['n_tx']} stations{area}, last {window} minutes",
                          badge, badge_tone, badge_help))
    tiles_slot = st.container()  # drawn after the band buttons below, which decide which tiles to outline
    all_bands = list(bands.index)
    st.html(dash.band_buttons_css("cond_focus", all_bands))
    focus = st.pills("Look closer at a band", all_bands, selection_mode="multi", key="cond_focus",
                     help="Pick one or more bands to outline them in the tiles and the table, and to show only those on the "
                          "map, with a short summary of each.") or []
    with tiles_slot:
        st.html(dash.tiles_html(bands, result["series"], focus))
    st.html(dash.strip_html(sw.readings(data), est))

    left, right = st.columns([1.5, 1])
    with left.container(border=True):
        st.html(dash.grid_html(result["chance"], result["regions_n"], focus))
    with right.container(border=True):
        st.html(dash.highlights_html(cond.short_highlights(result, power, mode, margin_db, units, window)))

    good = result["receivers"][result["receivers"]["margin"] >= 0]
    with st.container(border=True):
        st.html('<div class="cd"><p class="cd-h">Map of receivers that would hear you</p>'
                f'<p class="cd-sub">{len(good)} receiver and band pairs with a good signal. Pick bands above to show only those.</p></div>')
        if len(good):
            views = _views(units)
            view_choice = st.selectbox("How much of the world", list(views), index=1, key=f"cond_map_view_{units}",
                                       label_visibility="collapsed")
            night = st.checkbox("Day and night line", key="cond_map_night",
                                help="Shades the night side and draws the line between day and night (the gray line), "
                                     "which is where the low bands tend to do best.")
            shown = good[good["band"].isin(focus)] if focus else good
            map_col, panel_col = st.columns([3, 1.3]) if focus else (st.container(), None)
            if len(shown):
                points = pd.DataFrame({"km": shown["km"].to_numpy(), "bearing": shown["bearing"].to_numpy(), "group": "single",
                                       "gap": float("nan"), "snr_a": shown["margin"].to_numpy(), "snr_b": float("nan"),
                                       "color": [dash.MAP_COLORS.get(b, "#8a8f98") for b in shown["band"]]})
                map_col.pyplot(azimuth_map_figure(lat, lon, points, {"A": "", "B": ""}, who, units, views[view_choice], "dots", True,
                                                  (0, 20), when=datetime.now(timezone.utc).replace(tzinfo=None) if night else None),
                               width="stretch")
                beyond = int((points["km"] > views[view_choice]).sum())
                if beyond:
                    map_col.caption(f"{beyond} receiver{'s' if beyond != 1 else ''} beyond this view.")
            else:
                map_col.caption("No receiver would hear you on the band you picked right now.")
            if panel_col is not None:
                for band in focus[:3]:
                    panel_col.html(dash.band_panel_html(band, bands.loc[band], result["chance"].loc[band], result["regions_n"].loc[band],
                                                        result["power_needed"].loc[band], units, k))
        else:
            st.caption("No receiver would hear you at this power and mode right now.")
    st.caption("Estimated from WSPR reports, not measured: good for comparing bands and directions, but the percentages run high "
               "for being spotted by a skimmer. The tabs below have the numbers behind this and how far to trust them.")

    t_band, t_margin, t_sun, t_far, t_how = st.tabs(["Band by band", "Margin and power needed", "Sun and MUF", "Farthest receivers",
                                                      "How this works"])
    with t_band:
        bands = result["bands"]
        summary = pd.DataFrame({
            "Status": [_STATUS[s] for s in bands["Status"]], "Trend": [_TREND[t] for t in bands["Trend"]],
            "Receivers hearing it": bands["Receivers"], "Good": bands["Good"], "Marginal": bands["Marginal"],
            f"Farthest good ({units})": [f"{km / k:,.0f}" if pd.notna(km) else "" for km in bands["Farthest good (km)"]],
            "Farthest receiver": [f"{c} ({r})" if c else "" for c, r in zip(bands["Farthest good receiver"],
                                                                             bands["Farthest good region"])],
        }, index=bands.index)
        st.dataframe(summary, width="stretch")
        st.caption("Good means the middle of that receiver's reports, restated for your power, beats the mode's threshold by your "
                   "safety margin. Marginal is above the threshold but by less than the margin. Trend compares the second half "
                   "of the look-back with the first.")

    with t_margin:
        st.html(dash.detail_grid_html(result["regions"], result["power_needed"], result["regions_n"], margin_db))
        st.caption(f"The top figure is dB above the {mode} threshold at a typical receiver in each region (the 75th percentile, so "
                   f"not the best one). Under it is the power that receiver would need to hear you with your {margin_db} dB margin; "
                   f"green means your {power:g} W is enough. A grey cell has only 1 or 2 receivers, so it has no figure: see "
                   "How this works.")

    with t_sun:
        if sw.interpret(data):
            _space_weather_card(boxed=False)
        if est:
            _muf_card(lat, lon, boxed=False)
    with t_far:
        dx = result["dx"]
        if dx.empty:
            st.caption("None at this power and mode right now.")
        else:
            st.dataframe(pd.DataFrame({
                "Band": dx["band"], "Receiver": dx["rx"], "Region": dx["region"], f"Distance ({units})": (dx["km"] / k).round(0).astype(int),
                "Margin (dB)": dx["margin"].round(0).astype(int)}), width="stretch", hide_index=True)

    with t_how:
        st.markdown(
            "Every WSPR report says: this station, at this power, was heard at this SNR. SNR moves dB for dB with power, so the "
            "report can be restated for your power: run 10 dB more and you would be 10 dB stronger there. Each mode needs a "
            "minimum SNR, and the gap between the two is the margin.\n\n"
            f"- **It is a guide, good to about {cond.SIGMA_DB:g} dB.** Checked against RBN readings from two stations' activations, "
            "predictions were off by about 6 dB within 1,000 miles and about 9 dB at 1,000 to 2,000 miles. I had no DX paths in "
            "that check, so DX is the least tested part. The chance figures allow for that error.\n"
            "- **It only counts receivers that heard the nearby WSPR stations.** With more power than they used, you would be "
            "heard in more places than shown.\n"
            "- **A region with only 1 or 2 receivers is a hint, not a result, and it flatters you.** Only receivers that heard "
            "something are counted, so when just one receiver in a region heard the stations near you, it is the lucky exception, "
            "and the others there heard nothing. Its figures look easy, but a contact there is usually hard. In a week of checks, "
            "such regions were really spotted only about 4 times in 10, so they show only a receiver count.\n"
            "- **The percentages are estimates, not measured success rates.** They are good for comparing bands and directions, "
            "but run high for being spotted by a skimmer. In a week of one station's CW activations, of the skimmers the figures "
            "said could hear him, about 1 in 10 spotted him below the usual RBN spotting level (about 2 dB above the CW figure), 4 in 10 at a margin of 0 to 5 dB over it, 5 in 10 at "
            "5 to 10 dB and about 7 in 10 at 15 dB or more.\n"
            "- **WSPR receivers are often quiet sites.** A station you hope to work may have more noise, so keep a margin.\n"
            "- **The thresholds are approximate**, and CW and voice depend on the operator's ear and the receiver.\n"
            "- **It assumes your antenna matches theirs** unless you enter a difference.\n"
            "- **Reports lag by a few minutes**, so this shows the last half hour or so, not this second.")


@st.cache_data(ttl=900, show_spinner=False)
def _space_weather():
    """NOAA's current space weather, cached for 15 minutes."""
    return sw.fetch()


def _space_weather_card(boxed=True):
    """The sun and the magnetic field right now, as a few numbers and a short plain reading. Shows nothing if NOAA can't be reached."""
    data = _space_weather()
    lines = sw.interpret(data)
    if not lines:
        return
    items = [(label, value, help_) for label, value, help_ in (
        ("Solar flux", None if data["flux"] is None else f"{data['flux']:.0f}",
         "The 10.7 cm radio flux, measured daily at noon in Penticton. Higher means better HF: about 70 is poor, 120 good, 150 or more very good."),
        ("Sunspots", None if data["ssn"] is None else f"{data['ssn']:.0f}",
         "The sunspot number of the latest complete day. More spots usually means a more active sun and better HF."),
        ("Kp index", None if data["kp"] is None else f"{data['kp']:.1f}",
         "How disturbed Earth's magnetic field is, 0 to 9, updated every 3 hours. Under 3 is quiet, 5 or more is a storm."),
        ("A index", None if data["a_index"] is None else f"{data['a_index']:.0f}",
         "The day's average disturbance of the magnetic field at middle latitudes (yesterday's). Under 8 is quiet, 30 or more is a storm."),
        ("X-ray", sw.xray_class(data["xray"]),
         "The flare level from the GOES satellite. A and B are quiet, C is minor, M can cause a brief radio fade-out, X a strong one."),
        ("Wind (km/s)", None if data["wind"] is None else f"{data['wind']:.0f}",
         "The speed of the wind reaching Earth. Around 400 is calm, 500 or more is elevated, 600 or more is fast."),
        ("Bz (nT)", None if data["bz"] is None else f"{data['bz']:+.0f}",
         "The north-south direction of the solar wind's magnetic field. Negative (south) lets the wind stir Earth's field."))
        if value is not None]
    with st.container(border=True) if boxed else nullcontext():
        if boxed:
            st.markdown("**\u2600\ufe0f Space weather right now**")
        for start in range(0, len(items), 4):
            row = items[start:start + 4]
            for col, (label, value, help_) in zip(st.columns(4)[:len(row)], row):
                col.metric(label, value, help=help_)
        st.markdown("\n".join(f"- {line}" for line in lines))
        flux_t, kp_t = sw.when_text(data["times"].get("flux"), "%d %b"), sw.when_text(data["times"].get("kp"), "%H:%M")
        st.caption("From NOAA's Space Weather Prediction Center. Solar flux is the daily noon reading"
                   + (f" (latest {flux_t})" if flux_t else "") + ", Kp is for the 3-hour period"
                   + (f" starting {kp_t} UTC" if kp_t else "") + ", and the rest update every few minutes.")


@st.cache_data(ttl=900, show_spinner=False)
def _ionosondes():
    """The working ionosondes with a fresh reading, cached for 15 minutes."""
    return iono.fetch()


def _muf_card(lat, lon, boxed=True):
    """The MUF and foF2 near the user, estimated from the nearest working ionosondes. Shows nothing if none is close enough."""
    est = iono.estimate(lat, lon, _ionosondes())
    if not est:
        return
    with st.container(border=True) if boxed else nullcontext():
        st.markdown("**\U0001F4E1 MUF near you** (MHz)" if boxed else "MHz:")
        shown = [(f"{d:,} km", f"{est['muf'][d]:.0f}", f"The highest frequency expected to work over a {d:,} km path. "
                  "Shorter paths cannot use as high a frequency, because the signal meets the ionosphere at a steeper angle.")
                 for d in (3000, 2000, 1000, 500)]
        shown.append(("NVIS (foF2)", f"{est['fof2']:.1f}", "The critical frequency: the highest frequency that still bounces straight "
                      "back for local contacts. Above it, signals pass up and out and skip over nearby stations."))
        for col, (label, value, help_) in zip(st.columns(len(shown)), shown):
            col.metric(label, value, help=help_)
        st.markdown("\n".join(f"- {line}" for line in iono.interpret(est)))
        st.caption("Measured by ionosondes (ionospheric radars) collected by GIRO and republished by Andrew KC2G at prop.kc2g.com. "
                   "Only a few dozen report at any time, so this is an estimate for your location, not a measurement.")


def _use_my_location():
    """The page's own button: switch to My location and have the browser asked for it straight away."""
    ss = st.session_state
    ss.update(cond_where="My location", _locate_ticket=ss.get("_locate_ticket", 0) + 1)


def conditions_page():
    """Which bands are open and where a signal like yours would be heard, from the WSPR stations near you."""
    st.header("\U0001F310 Band conditions now")
    st.markdown('<p class="subtitle">Which bands are open and where you would be heard, worked out from the WSPR stations near '
                'you in the last half hour, restated for your power and mode.</p>', unsafe_allow_html=True)
    skimmers, _ = skimmer_data()
    saved = st.session_state.get("cfg") or load_settings()
    ss = st.session_state
    if not ss.get("_cond_link_read"):  # a shared link sets the choices once per visit
        ss["_cond_link_read"] = True
        q = st.query_params
        if q.get("call"):
            ss["cond_call"] = q["call"].strip().upper()[:12]
        if "grid" in q:
            ss["cond_grid"] = q["grid"][:8]
        if q.get("units") in ("mi", "km"):
            ss["cond_units"] = q["units"]
        try:
            if 0.1 <= float(q.get("power", 0)) <= 1500:
                ss["cond_power"] = float(q["power"])
        except ValueError:
            pass
        if q.get("mode") in cond.MODES:
            ss["cond_mode"] = q["mode"]
        if q.get("window", "").isdigit() and int(q["window"]) in (30, 60, 120):
            ss["cond_window"] = int(q["window"])
    ss.setdefault("cond_call", ss.get("callsign_in", saved.get("callsign", "")))
    ss.setdefault("cond_grid", saved.get("grid", ""))
    ss.setdefault("cond_units", saved.get("units", "mi"))
    ss.setdefault("cond_power", 100.0)
    ss.setdefault("cond_window", 30)
    with st.sidebar:
        st.header("Where")
        where = st.radio("Find me by", ["Callsign or grid", "My location"], horizontal=True, key="cond_where")
        callsign, grid = "", ""
        if where == "Callsign or grid":
            callsign = st.text_input("Callsign", key="cond_call").strip().upper()
            grid = st.text_input("Grid square (optional)", key="cond_grid",
                                 placeholder="Looked up from your callsign",
                                 help="Enter the grid you are activating from, such as a park, to see conditions there.")
        else:
            fix = _LOCATE(key="cond_locate", data={"ticket": ss.get("_locate_ticket", 0)}, on_position_change=lambda: None, on_error_change=lambda: None)
            if fix.position:
                st.session_state["cond_geo"] = (float(fix.position["lat"]), float(fix.position["lon"]))
            geo = st.session_state.get("cond_geo")
            if geo:
                st.caption(f"\U0001F4CD {geo[0]:.3f}, {geo[1]:.3f} \u00b7 grid {cond.latlon_to_maidenhead(*geo)}")
            st.caption("Your browser asks before sharing. The position is used only to find WSPR stations near you and is not "
                       "saved; only a box around it goes to wspr.live.")
        source = st.radio("Learn from", ["Stations near me", "One transmitter"], horizontal=True, key="cond_source",
                          help="Stations near me uses every WSPR transmitter inside the area below. One transmitter uses just "
                               "your own WSPR transmitter, or a friend's, and treats it as you: its spots, at its own power, "
                               "show where a station at that spot is being heard. It is the closest match to your own antenna. A single transmitter "
                               "sends each band only now and then, so a look-back of an hour or two gives it more to go on.")
        ref_call = ""
        if source == "One transmitter":
            ref_call = st.text_input("Transmitter callsign", value=st.session_state.get("callsign_in", saved.get("callsign", "")),
                                     key="cond_ref_call", help="The callsign it sends in WSPR.").strip().upper()
        units = st.radio("Distance units", ["mi", "km"], horizontal=True, key="cond_units")
        if source == "One transmitter":
            radius_choice = 500.0  # not used: the transmitter's own spots are the data
        elif units == "mi":
            radius_choice = st.slider("Area around you (miles)", 60, 930, 300, 25, key="cond_radius_mi",
                                      help="WSPR stations inside this distance count as being near you. A wider area gives "
                                           "more data where few stations transmit.") * KM_PER_MILE
        else:
            radius_choice = float(st.slider("Area around you (km)", 100, 1500, 500, 50, key="cond_radius_km",
                                            help="WSPR stations inside this distance count as being near you. A wider area "
                                                 "gives more data where few stations transmit."))
        st.header("Your signal")
        power = st.number_input("Power (watts)", 0.1, 1500.0, step=1.0, key="cond_power")
        mode = st.selectbox("Mode", list(cond.MODES), key="cond_mode",
                            help="Each mode needs a minimum signal-to-noise ratio. These are approximate, in a 2,500 Hz "
                                 + "bandwidth: " + ", ".join(f"{m} {v:+g} dB" for m, v in cond.MODES.items()) + ".")
        antenna_db = st.number_input("Your antenna compared with theirs (dB)", -20.0, 20.0, 0.0, step=1.0, key="cond_antenna",
                                     help="0 assumes your antenna is about as good as the WSPR stations'. Use a negative "
                                          "number for a weaker antenna (a compromise portable antenna, say). A relative-gain "
                                          "test on the Compare antennas page can tell you the number.")
        margin_db = st.slider("Safety margin (dB)", 0, 10, 3, key="cond_margin",
                              help="Signals fade, so a path counts as good only when it beats the mode's threshold by this much.")
        window = st.radio("Look back", [30, 60, 120], horizontal=True, key="cond_window",
                          format_func=lambda m: f"{m} min" if m < 60 else f"{m // 60} h")
        if st.button("Refresh now", width="stretch", key="cond_refresh"):
            _nearby_spots.clear()
    link = {"call": callsign, "units": units, "power": f"{power:g}", "mode": mode, "window": str(window)}
    if grid.strip():
        link["grid"] = grid.strip()
    if callsign and dict(st.query_params) != link:
        st.query_params.from_dict(link)
    k = KM_PER_MILE if units == "mi" else 1.0
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    end = now.replace(minute=now.minute - now.minute % 5)  # steady for five minutes so the cache can work
    if source == "One transmitter":
        try:
            if not ref_call:
                st.info("Enter the transmitter's callsign in the sidebar.")
                return
            with st.spinner(f"Asking wspr.live where {ref_call} was heard\u2026"):
                spots, cut = _station_spots(ref_call, end.isoformat(), int(window))
        except WsprError as exc:
            st.error(str(exc))
            return
        if spots.empty:
            st.warning(f"{ref_call} had no WSPR spots in the last {window} minutes. Check the callsign, or look back further.")
            return
        lat, lon = float(spots["tx_lat"].median()), float(spots["tx_lon"].median())
        home, label, radius, widened = (lat, lon), f"{ref_call}, grid {cond.latlon_to_maidenhead(lat, lon)}", 0.0, False
        st.caption(f"\U0001F4CD {label} \u00b7 spots up to {end:%H:%M} UTC (WSPR reports arrive a few minutes late)")
        if cut:
            st.warning("That was more data than the limit, so the oldest spots are missing. Try a shorter look-back.")
        result = cond.analyze(spots, home, power, mode, end, window, antenna_db, margin_db)
        if result["bands"].empty:
            st.warning("No receivers heard that transmitter in that time.")
            return
        st.caption(f"{ref_call}'s own antenna and location stand in for yours.")
        _conditions_body(result, power, mode, margin_db, units, k, lat, lon, ref_call, window)
        return
    try:
        if where == "My location":
            geo = st.session_state.get("cond_geo")
            if not geo:
                st.info("Allow your browser to share your location when it asks. If it can't, or you would rather not, switch "
                        "to **Callsign or grid** in the sidebar.")
                return
            lat, lon = geo
            label = f"your location, grid {cond.latlon_to_maidenhead(lat, lon)}"
        elif not (callsign or grid.strip()):
            st.info("Enter your callsign or a grid square in the sidebar, or choose **My location**.")
            st.button("Use my location", key="hero_cond", type="primary", on_click=_use_my_location,
                      help="Your browser asks before sharing your location. It is used only to find the WSPR stations near "
                           "you and is not saved.")
            return
        else:
            lat, lon, label = resolve_home(callsign, grid, skimmers)
    except (RuntimeError, ValueError) as exc:
        st.error(str(exc))
        return
    home = (lat, lon)
    radius, widened = radius_choice, False
    try:
        with st.spinner("Asking wspr.live which stations near you were heard\u2026"):
            while True:
                spots, cut = _nearby_spots(lat, lon, float(radius), end.isoformat(), int(window))
                if spots["tx"].nunique() >= 3 or radius >= 1490:
                    break
                radius, widened = min(1500.0, radius * 2), True
    except WsprError as exc:
        st.error(str(exc))
        return
    st.caption(f"\U0001F4CD {label} \u00b7 spots up to {end:%H:%M} UTC (WSPR reports arrive a few minutes late)")
    if spots.empty:
        st.warning(f"No WSPR station within {radius / k:,.0f} {units} of this spot was heard in the last {window} minutes, so there is "
                   "nothing to base conditions on. Widen the area, or try again in a few minutes.")
        return
    if widened:
        st.info(f"Only a few WSPR stations transmit near you, so I widened the area to {radius / k:,.0f} {units} to get enough data.")
    if cut:
        st.warning("That was more data than the limit, so the oldest spots are missing. Try a shorter look-back.")
    result = cond.analyze(spots, home, power, mode, end, window, antenna_db, margin_db)
    if result["bands"].empty:
        st.warning("No receivers heard the stations near you in that time.")
        return

    _conditions_body(result, power, mode, margin_db, units, k, lat, lon, callsign or "You", window,
                     area=f" within {radius / k:,.0f} {units}")
