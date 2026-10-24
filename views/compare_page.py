"""The Compare antennas page: load one station's spots, say how the antennas were switched, and read which got out better."""
import streamlit as st

from data_sources import skimmer_data
from ui_common import RBN_SNR_SCALE, SPLIT_FREQ, SPLIT_LOG, WSPR_SNR_SCALE, WSPR_SOURCE
from views import inputs, test_link
from views.compare_view import compare_view

STEPS_CSS = """<style>
.cs { display:flex; flex-wrap:wrap; gap:10px; margin:4px 0 14px 0; }
.cs div { flex:1 1 200px; border:1px solid rgba(128,128,128,.3); background:rgba(128,128,128,.07); border-radius:12px;
  padding:10px 14px; display:flex; gap:12px; align-items:center; }
.cs b { width:30px; height:30px; border-radius:50%; border:1.5px solid currentColor; display:flex; align-items:center;
  justify-content:center; flex:none; font-size:15px; }
.cs span { font-size:15px; font-weight:600; display:block; } .cs small { opacity:.7; font-size:13px; display:block; }
.cs .done { border-color:#3fb950; } .cs .done b { background:#3fb950; border-color:#3fb950; color:#0d1117; }
.cs .now { border-color:#4da3ff; background:rgba(77,163,255,.10); } .cs .now b { background:#4da3ff; border-color:#4da3ff; color:#0d1117; }
.cs .next { opacity:.6; }
</style>"""


def _steps(loaded, comparing_rbn):
    """The three steps of a comparison as a row, with where you are marked. Shown until the setup is confirmed."""
    states = ["done", "now", "next"] if loaded else ["now", "next", "next"]
    labels = [("Load your spots", "in the sidebar (the » at the top left on a phone)"),
              ("Say how you switched", "I guessed it: check it below, then confirm" if loaded
               else "which frequency was which" if comparing_rbn else "which antenna, and when"),
              ("Read the result", "the verdict and the evidence")]
    cells = "".join(f'<div class="{state}"><b>{"&#10003;" if state == "done" else n}</b><div style="border:0;background:none;padding:0;'
                    f'display:block"><span>{head}</span><small>{sub}</small></div></div>'
                    for n, (state, (head, sub)) in enumerate(zip(states, labels), 1))
    st.html(STEPS_CSS + f'<div class="cs">{cells}</div>')


def _how_to_test():
    """What to do before there is anything to compare, in plain steps."""
    left, right = st.columns(2)
    left.markdown(
        "#### With WSPR, the best way\n"
        "1. Connect your WSPR transmitter to an A/B coax switch, antenna A on one side and B on the other. Keep the band, power and settings the same.\n"
        "2. Decide how long each antenna stays on before you switch: **one transmission** (2 minutes), or a **set time**, for example 10 to 30 minutes. Write down the times.\n"
        "3. Switch in this order, **always starting with A**:\n"
        "   - **A, B, B, A, A, B, B, A …** is usually best. Each antenna gets the same mix of earlier and later turns, so a steady change in the band cancels out. Each antenna stays on for two turns in a row, so use shorter turns (for example 15 minutes instead of 30).\n"
        "   - **A, B, A, B …** is simpler, but B always goes second.\n"
        "4. **How long?** A test can be as short as 4 transmissions (about 8 minutes) or run for hours. More data is better: it narrows the uncertainty, and a long test also shows how changing band conditions affect the result.\n"
        "5. Wait about five minutes after the last transmission. Enter your callsign and the date in the sidebar, choose the band, and click **Load spots**.\n"
        "6. Tell the page how you switched. It works out which antenna got out better, by how much, and in which directions.")
    right.markdown(
        "#### With RBN, for a quick CW check\n"
        "1. Call CQ on one frequency with antenna A, then on a nearby frequency with antenna B, a minute or two apart, "
        "at the same speed and power.\n"
        "2. Pick **Download a day** (for yesterday or earlier) or copy the spots from reversebeacon.net and pick "
        "**Paste spots**.\n"
        "3. Load the spots and confirm which frequency was A and which was B.\n\n"
        "RBN is a rough check: skimmers report a CQ a minute or more after it was sent, so the two antennas can only be told "
        "apart by frequency. WSPR is better for a careful test.")


def compare_page():
    st.title("⚖️ Compare antennas")
    st.markdown('<p class="subtitle">Which of two antennas, power levels or tests gets out better? Load the spots from one '
                'station, say how you switched, and read the verdict, with the evidence behind it.</p>', unsafe_allow_html=True)

    skimmers, refresh_msg = skimmer_data()
    ss, cfg, pick = inputs.begin(default_source=WSPR_SOURCE)
    test_link.read(ss, st.query_params)
    with st.sidebar:
        where = inputs.where_inputs(ss, cfg, second_call=True)
        load = st.button("Load spots", type="primary", width="stretch")
        st.button("Try an example", width="stretch", on_click=inputs.start_example, args=("quick",),
                  help="Loads a real WSPR antenna test, a 40m loop against a 20m dipole, so you can see what the results look like.")
        st.button("Try an RBN example", width="stretch", on_click=inputs.start_example, args=("rbn",),
                  help="Loads a quick CW check on the Reverse Beacon Network: a 40m loop on 14069.5 kHz against a 20m dipole "
                       "on 14070.5 kHz, told apart by frequency.")
        st.button("Try a long-test example", width="stretch", on_click=inputs.start_example, args=("long",),
                  help="Loads Dale's long WSPR test of 26 August 2026: a Performer (tuned elevated quarter-wave) against a 17 ft whip on a 5:1 unun, "
                       "switched every 30 minutes for nine and a half hours.")
        st.button("Start over", type="tertiary", on_click=inputs.start_over,
                  help="Clears the loaded spots, the dates and the test setup. Your callsign and grid stay.")
        where.load = inputs.take_autoload(ss, load)
        st.divider()
        view = inputs.view_controls(ss, cfg, pick, where.kind, comparing=True)
        link_slot = st.container()  # the link is drawn last, once the test's setup widgets exist
        st.caption(f"\U0001F6F0️ {refresh_msg}")
    inputs.load_spots(ss, skimmers, where)
    _result(ss, skimmers, where, view)
    inputs.remember(ss, cfg, where, view, extra=test_link.params(ss, view.units), slot=link_slot)


def _result(ss, skimmers, where, view):
    """Everything under the title once spots are (or are not yet) loaded."""
    loaded = ss.raw is not None and not ss.raw.empty
    if not (loaded and ss.get("setup_ok")):  # once the setup is confirmed the strip has done its job; the result gets the room
        _steps(loaded, comparing_rbn=where.kind == "RBN")
    if ss.raw is None:
        quick, rbn, long_ = st.columns(3)
        quick.button("Try the quick example", key="hero_quick", type="primary", width="stretch", on_click=inputs.start_example,
                     args=("quick",), help="A real WSPR test: a 40m loop against a 20m dipole, switched after every transmission.")
        rbn.button("Try the RBN example", key="hero_rbn", width="stretch", on_click=inputs.start_example, args=("rbn",),
                   help="A quick CW check on the Reverse Beacon Network: the same loop and dipole, on two frequencies.")
        long_.button("Try the long-test example", key="hero_long", width="stretch", on_click=inputs.start_example, args=("long",),
                     help="Dale's long WSPR test: a Performer (tuned elevated quarter-wave) against a 17 ft whip on a 5:1 unun, switched every 30 minutes.")
        _how_to_test()
        return
    if inputs.nothing_found_message(ss):
        return

    wspr = ss.kind == "WSPR"
    locs = ss.locs if wspr else skimmers
    scale = WSPR_SNR_SCALE if wspr else RBN_SNR_SCALE
    noun = "receiver" if wspr else "skimmer"
    spots = ss.raw
    spots = spots[(spots["time"].dt.time >= view.start_t) & (spots["time"].dt.time <= view.end_t) & (spots["snr"] >= view.min_snr)]
    if view.band != "All":
        spots = spots[spots["band"] == view.band]
    if spots.empty:
        st.warning("No spots match the current filters. Set **Band** to All, or widen the UTC time window in the sidebar.")
        return

    lat, lon, label = ss.home
    if ss.notice:
        st.warning(ss.notice)
    inputs.example_banner(ss)
    if wspr and ss.loaded_at is not None:
        st.caption(f"\U0001F552 Loaded at {ss.loaded_at:%H:%M} UTC. The newest transmission in it is at "
                   f"{ss.raw['time'].max():%H:%M} UTC. Sent something since? Click **Load spots** again.")
    st.caption(f"\U0001F4CD {ss.callsign}" + (f" (A) and {ss.tx_call_b} (B)" if ss.get("tx_call_b") else "") + f" · {label}")
    if not wspr:
        st.caption("RBN tells A and B apart by frequency: call CQ on one frequency with the first antenna, then on a nearby "
                   "frequency with the other, back to back (a minute or two apart).")
    compare_view(spots, locs, (lat, lon), label, ss.callsign, ss.file_date, view.tiles, view.show_all and not wspr, view.units,
                 SPLIT_LOG if wspr else SPLIT_FREQ, 0.5, scale, noun, ss.kind, ss.raw)
