"""Comparing two antennas: separating the A and B sides, setting up the test, and showing the verdict and its evidence."""
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import streamlit as st

from charts import (
    COLOR_A,
    COLOR_B,
    COLOR_TIE,
    COMPASS_16,
    _gain_interval,
    _linear_db,
    best_direction,
    breakdown_table,
    direction_advantage_chart,
    direction_chart,
    distance_chart,
    gain_over_time_chart,
    head_to_head_table,
    paired_scatter,
    reach_bar_html,
    receiver_time_chart,
    relative_gain_chart,
    scoreboard_html,
    sector_means,
    timeline_chart,
    verdict_card_html,
)
from compare_stats import (
    BIN_MIN_SHARED,
    DALE_MIN_SPOTS,
    DALE_SITE_MIN,
    station_gains,
    gain_by_window,
    DIRECTION_EDGE_DB,
    gain_summary,
    WSPR_FLOOR_DB,
    EQUAL_DB,
    SLOT_NOISE_DB,
    analyze,
    build_verdict,
    count_pairs,
    default_labels,
    direction_advantage,
    db_from_watts,
    eligible_receivers,
    median_power_dbm,
    power_method,
    t_crit,
    receiver_table,
    _farthest,
    _lead_text,
    round_consistency,
    split_by_labels,
)
from data_sources import load_listening
from maps import build_map, compute_stats
from pdfs import compare_report_pdf
from ui_common import KM_PER_MILE, SPLIT_FREQ, _pretty_date, _views
from views import test_link
from world_map import MIN_CELL, azimuth_map_figure
from wspr_data import WsprError


def frequency_groups(spots, gap_khz):
    """Split spots into groups of nearby frequencies: a new group starts wherever the gap between
    neighbouring spot frequencies exceeds `gap_khz`. Returns [(median_freq, spots)] in frequency order."""
    ordered = spots.dropna(subset=["freq"]).sort_values("freq")
    group_id = (ordered["freq"].diff() > gap_khz).cumsum()
    return [(g["freq"].median(), g) for _, g in ordered.groupby(group_id)]


@contextmanager
def _box(title, **_):
    """A titled box for the setup. The setup lives inside one expander, and Streamlit cannot put an expander in an expander."""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        yield


def _name_inputs():
    """Optional names for the two sides, used everywhere in the results."""
    with _box("Name your antennas (optional)"):
        c1, c2 = st.columns(2)
        return (c1.text_input("\U0001F535 Name for A", placeholder="e.g. Dipole", key="cmp_name_a").strip(),
                c2.text_input("\U0001F7E0 Name for B", placeholder="e.g. 40m loop", key="cmp_name_b").strip())


def _separate_by_frequency(spots, gap_khz):
    """{'a', 'b', 'default'} chosen from frequency groups, or None if that can't be done."""
    groups = frequency_groups(spots, gap_khz)
    for tighter in (0.2, 0.1):  # two tests very close together look like one group: look a little closer before giving up
        if len(groups) >= 2:
            break
        groups = frequency_groups(spots, tighter)
    freqs = ", ".join(f"{f:.1f}" for f, _ in groups)
    if len(groups) < 2:
        st.warning(f"Found only {len(groups)} frequency group ({freqs or 'none'} kHz). Comparing needs spots on at "
                   f"least two frequencies. Call CQ on a different frequency with each antenna, at least 0.1 kHz apart.")
        return None

    def option_label(i):
        f, g = groups[i]
        return (f"{f:.1f} kHz · {g['spotter'].nunique()} skimmers ({len(g)} spots) · "
                f"{g['time'].min():%H:%M}–{g['time'].max():%H:%M} UTC")

    top_two = sorted(sorted(range(len(groups)), key=lambda i: -len(groups[i][1]))[:2])
    pick_a, pick_b = st.columns(2)
    ia = pick_a.selectbox("\U0001F535 Frequency A", range(len(groups)), index=top_two[0], format_func=option_label)
    ib = pick_b.selectbox("\U0001F7E0 Frequency B", range(len(groups)), index=top_two[1], format_func=option_label)
    if ia == ib:
        st.warning("Pick two different frequencies to compare.")
        return None
    (fa, spots_a), (fb, spots_b) = groups[ia], groups[ib]
    return {"a": spots_a, "b": spots_b, "default": {"A": f"A ({fa:.1f} kHz)", "B": f"B ({fb:.1f} kHz)"}}


def _without_ghost_cycles(spots):
    """Throw out cycles that only one or two receivers reported when ordinary cycles are heard by many more. They are
    nearly always one receiver with a wrong clock reporting your transmission under the next time slot, so they are
    not real transmissions. On a weak band, where every cycle has few reports, nothing is thrown out."""
    st.session_state["ghost_cycles"] = 0
    heard = spots.groupby("time")["spotter"].nunique()
    typical = heard[heard > GHOST_MAX].median()
    if pd.isna(typical) or typical < GHOST_MIN_TYPICAL:
        return spots
    ghosts = heard[heard <= GHOST_MAX].index
    if len(ghosts) == 0:
        return spots
    st.session_state["ghost_cycles"] = len(ghosts)
    st.caption(f"Left out {len(ghosts)} cycle{'s' if len(ghosts) > 1 else ''} that {GHOST_MAX} or fewer receivers heard, "
               f"when typical cycles were heard by about {typical:.0f}. A cycle that thin says little about an antenna, and "
               "it is often one receiver with a wrong clock reporting your signal under the wrong time slot.")
    return spots[~spots["time"].isin(ghosts)]


def _separate_by_log(spots):
    """WSPR antenna test: each transmission is logged with the antenna that was connected. Either the antenna changed
    after every transmission (the user says how many there were and where the test started, and the app lists them with a
    first guess the user can correct), or it stayed on one antenna for a long block and then the other, over hours."""
    spots = _without_ghost_cycles(spots)
    all_stamps = [pd.Timestamp(t) for t in np.sort(spots["time"].unique())]
    if len(all_stamps) < 2:
        st.warning("Only one transmission was found. An antenna test needs at least two, one on each antenna. Spots "
                   "take a few minutes to show up after you transmit, so try again shortly, or pick another day.")
        return None
    if spots["band"].nunique() > 1:
        st.warning(f"These spots cover {spots['band'].nunique()} bands. Test one band at a time: pick it under "
                   "**Filters \u2192 Band** in the sidebar.")
    st.markdown("**Your test**")
    mode = st.radio("How did you switch antennas?", ["After every transmission", "In long blocks"], horizontal=True,
                    key="test_mode",
                    help="After every transmission: A, then B, then A \u2026 (or A, B, B, A), the quick test. "
                         "In long blocks: one antenna for 30 minutes or an hour, then the other, repeated over several hours, "
                         "which suits a long, time-averaged test.")
    if mode == "In long blocks":
        return _blocks_test(spots, all_stamps)
    top = st.columns([4, 2, 3])
    most = max(2, min(len(all_stamps), 80))
    # Two transmissions is a coin toss; start on 12 (six pairs) when the data has them, and never above what there is
    st.session_state["test_cycles"] = max(2, min(st.session_state.get("test_cycles", 12 - 12 % 2), most - most % 2))
    n_cycles = int(top[0].number_input(
        "Transmissions in your test", min_value=2, max_value=most, step=2,
        key="test_cycles",
        help="Count every 2-minute transmission, on either antenna: A then B is 2, A B B A is 4, A B B A twice is 8. "
             "More is steadier: 8 or more (4 pairs) is a solid test, 12 to 16 for differences under 1 dB. "
             "Use whole rounds (a multiple of 2, or of 4 for A B B A): an odd count leaves one transmission without a partner."))
    n_cycles = min(n_cycles, len(all_stamps))
    if len(all_stamps) >= 20 and n_cycles < len(all_stamps) // 2:
        st.info(f"The data holds **{len(all_stamps)} transmissions**, but this quick test uses only the latest **{n_cycles}**. "
                "If you ran a long test with one antenna for 30 minutes or an hour at a time, choose **In long blocks** above "
                "to use all of it.")
    multi_day = all_stamps[0].date() != all_stamps[-1].date()
    fmt = (lambda t: f"{t:%d %b %H:%M}") if multi_day else (lambda t: f"{t:%H:%M}")
    LATEST = "Latest"
    start_options = [LATEST] + [fmt(t) for t in reversed(all_stamps[:-1])]
    if st.session_state.get("test_start") not in start_options:  # a link made for other spots
        st.session_state["test_start"] = LATEST
    start_choice = top[1].selectbox(
        "Starting at (UTC)", start_options, key="test_start",
        help="Leave on Latest for a test you have just run. To use an earlier test, pick the time of its first "
             "transmission.")
    patterns = {"A, B, A, B \u2026": "ABAB", "A, B, B, A \u2026": "ABBA"}  # every test starts on A
    order = top[2].radio("Order", list(patterns), key="test_order",
                         help="The order the antennas were connected, starting with A. A, B, B, A balances which "
                              "antenna goes first in each pair. If a test really started on B, change the Antenna "
                              "cells below.")
    pattern = patterns[order]
    if start_choice == LATEST:
        first = len(all_stamps) - n_cycles
    else:
        first = next(i for i, t in enumerate(all_stamps) if fmt(t) == start_choice)
    stamps = all_stamps[first:first + n_cycles]
    if len(stamps) < n_cycles:
        st.caption(f"Only {len(stamps)} transmissions are available from that start.")
    if len(stamps) < 2:
        st.warning("Pick an earlier start, or fewer transmissions.")
        return None
    if len(stamps) % 2:
        st.caption("⚠ An odd number of transmissions leaves one without a partner on the other antenna. It is left out "
                   "of the A/B comparison (it still shows on the maps), so an even count is better.")
    pairs = max(len(stamps) // 2, 1)
    smallest = 2.8 * SLOT_NOISE_DB / np.sqrt(pairs)  # 80 % chance of seeing a difference this big, from slot-to-slot fading
    st.caption(f"{len(stamps)} transmissions is about {pairs} pair{'s' if pairs != 1 else ''} of A and B: enough to "
               + (f"reliably show a difference of roughly {smallest:.1f} dB or more. More pairs show smaller differences."
                  if smallest > EQUAL_DB else
                  f"show differences down to about {EQUAL_DB:g} dB. Anything smaller counts as equal, so more pairs "
                  "won't change the answer much."))
    st.caption("Which antenna was connected for each transmission? The antenna changes after every transmission, down "
               "the list. Fix any row to match your notes, or clear a row to leave it out.")
    if any(g > 2.5 for g in [(y - x).total_seconds() / 60 for x, y in zip(stamps, stamps[1:])]):
        st.caption("\u26a0 Some cycles are missing between these rows (the transmitter skipped them, or nobody heard "
                   "them). The labels assume you changed antenna after each transmission you made; if you changed on "
                   "a timer instead, correct the rows after each gap.")
    log_key = f"antenna_log_{pattern}_{len(stamps)}_{stamps[0]:%d%H%M}"
    heard = spots.groupby("time")["spotter"].nunique()
    right_now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    arriving = [(right_now - t).total_seconds() / 60 < DATA_LAG_MINUTES for t in stamps]
    gaps = [0.0] + [(b - a).total_seconds() / 60 for a, b in zip(stamps, stamps[1:])]
    table = pd.DataFrame({"UTC time": [fmt(t) for t in stamps],
                          "Receivers that heard it": [int(heard[t]) for t in stamps],
                          "Antenna": default_labels(stamps, pattern),
                          "Status": [("\u23f3 may be incomplete" if late else
                                      (f"\u26a0 {gap:.0f} min after the row above" if gap > 2.5 else ""))
                                     for late, gap in zip(arriving, gaps)]})
    for row, lab in test_link.log_base(st.session_state, log_key).items():  # corrections a shared link carried
        if row < len(table):
            table.loc[row, "Antenna"] = lab
    edited = st.data_editor(
        table, key=log_key, hide_index=True, width="stretch",
        disabled=["UTC time", "Receivers that heard it", "Status"], height=min(35 * (len(table) + 1) + 3, 420),
        column_config={"Antenna": st.column_config.SelectboxColumn("Antenna", options=["A", "B"],
                                                                   help="A, B, or empty to leave this one out.")})
    marked = {t: lab for t, lab in zip(stamps, edited["Antenna"]) if lab in ("A", "B")}
    late = [t for t, wait in zip(stamps, arriving) if wait and t in marked]
    labels = marked
    if late:  # still counted; this is only a heads-up that the newest reports may not all be in yet
        ready_at = max(late) + pd.Timedelta(minutes=DATA_LAG_MINUTES)
        plural = len(late) > 1
        st.info(f"\u23f3 The newest transmission{'s' if plural else ''} (from {late[0]:%H:%M} UTC) "
                f"{'are' if plural else 'is'} less than {DATA_LAG_MINUTES} minutes old, so not all of the spots may be in "
                f"yet and the result could still shift. Click **Load spots** again after {ready_at:%H:%M} UTC to refresh it.")
    if "A" not in labels.values() or "B" not in labels.values():
        st.warning("Mark at least one transmission as A and one as B.")
        return None
    in_test = spots[spots["time"].isin(list(labels))]
    spots_a, spots_b = split_by_labels(in_test, labels)
    return {"a": spots_a, "b": spots_b, "time_based": True, "alternating": True, "labeled": True,
            "default": {"A": "A", "B": "B"}, "log": sorted(labels.items())}


def _clock(text):
    """(hour, minute) from a typed time such as 12:30, 1230, 12.30, 12 30 or 930, or None."""
    m = re.fullmatch(r"(\d{1,2})[:.\s]?(\d{2})", re.sub(r"\s*(utc|z)$", "", (text or "").strip(), flags=re.IGNORECASE))
    if m and int(m[1]) < 24 and int(m[2]) < 60:
        return int(m[1]), int(m[2])
    return None


def _cell_time(text, day0):
    """A block's From or To cell as a Timestamp: a clock time, with +1 (or +1d) after it for the next UTC day. `day0` is the date
    of the first block. Returns None when the cell isn't a time."""
    m = re.fullmatch(r"\s*(.*?)\s*(?:\+\s*(\d+)\s*d?)?\s*", str(text or ""))
    clock = _clock(m[1]) if m else None
    if clock is None:
        return None
    return pd.Timestamp(day0) + pd.Timedelta(days=int(m[2] or 0), hours=clock[0], minutes=clock[1])


def _parse_schedule(text, day0, pattern):
    """Blocks from pasted lines such as '16:40-17:12 A', one per line (the antenna is optional and then follows the A, B pattern;
    a block that starts earlier in the day than the one before it is taken to be on the next UTC day).
    Returns ([(antenna, lo, hi)], [problem lines])."""
    blocks, problems, day, last_lo = [], [], pd.Timestamp(day0).normalize(), None
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"\s*(\d{1,2}[:.]?\d{2})\s*(?:-|\u2013|to)\s*(\d{1,2}[:.]?\d{2})\s*([AaBb])?\s*", line)
        lo_c, hi_c = (_clock(m[1]), _clock(m[2])) if m else (None, None)
        if not m or lo_c is None or hi_c is None:
            problems.append(line.strip())
            continue
        lo = day + pd.Timedelta(hours=lo_c[0], minutes=lo_c[1])
        if last_lo is not None and lo < last_lo:
            day += pd.Timedelta(days=1)
            lo += pd.Timedelta(days=1)
        hi = day + pd.Timedelta(hours=hi_c[0], minutes=hi_c[1])
        if hi <= lo:
            hi += pd.Timedelta(days=1)
        antenna = m[3].upper() if m[3] else pattern[len(blocks) % len(pattern)]
        blocks.append((antenna, lo, hi))
        last_lo = lo
    return blocks, problems


def _block_start(label, all_stamps, fmt):
    """The start of a long test, from a transmission picked in the list or a time typed in. Times can be typed as 12:30, 1230,
    12.30, 12 30, 930 or 1230z, and a date can come first (08 Oct 12:30). Returns a Timestamp, or None if the text isn't a
    time. A time alone takes the date of the first transmission."""
    label = (label or "").strip()
    for t in all_stamps:
        if fmt(t) == label:
            return t
    first = all_stamps[0]
    text = re.sub(r"\s*(utc|z)$", "", label, flags=re.IGNORECASE).strip()
    clock = re.fullmatch(r"(\d{1,2})[:.\s]?(\d{2})", text)
    if clock:
        hour, minute = int(clock[1]), int(clock[2])
        if hour < 24 and minute < 60:
            return pd.Timestamp(first.year, first.month, first.day, hour, minute)
        return None
    for pattern in ("%d %b %H:%M", "%d %b %H%M"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        return pd.Timestamp(first.year, parsed.month, parsed.day, parsed.hour, parsed.minute)
    return None


def _blocks_test(spots, all_stamps):
    """A long test: one antenna for a block of time, then the other, repeated. Every transmission inside a block counts
    for that block's antenna. Nothing is paired: each receiver's reports on A and on B are compared as a whole."""
    multi_day = all_stamps[0].date() != all_stamps[-1].date()
    fmt = (lambda t: f"{t:%d %b %H:%M}") if multi_day else (lambda t: f"{t:%H:%M}")
    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    st.session_state.setdefault("blk_len", 30)
    block_min = int(c1.number_input("Minutes per block", 4, 480, step=2, key="blk_len",
                                    help="How long each antenna stayed connected before you switched."))
    # Open on the whole data set: the first transmission, and enough blocks to reach the last one. The keys carry the data's
    # first and last times, so loading different spots starts fresh instead of keeping the last test's choices.
    sig = f"{all_stamps[0]:%d%H%M}_{all_stamps[-1]:%d%H%M}_{block_min}"
    span_min = (all_stamps[-1] - all_stamps[0]).total_seconds() / 60 + 2
    fit = int(np.ceil(span_min / block_min))
    default_blocks = int(min(40, max(2, fit + fit % 2)))
    test_link.apply_blocks(st.session_state, sig)
    st.session_state.setdefault(f"blk_n_{sig}", default_blocks)
    n_blocks = int(c2.number_input("Number of blocks", 2, 40, step=2, key=f"blk_n_{sig}",
                                   help="Count every block, A and B together: A for 30 minutes then B for 30 is 2."))
    patterns = {"A, B, A, B \u2026": "ABAB", "A, B, B, A \u2026": "ABBA"}
    order = c4.radio("Block order", list(patterns), key="blk_order", horizontal=True,
                     help="Starting with A. A, B, B, A balances which antenna goes first.")
    st.session_state.setdefault(f"blk_start_{sig}", fmt(all_stamps[0]))
    start_label = c3.selectbox(
        "First block starts at (UTC)", [fmt(t) for t in all_stamps], key=f"blk_start_{sig}", accept_new_options=True,
        help="The time you first connected antenna A. Pick a transmission from the list, or type any time such as 12:30 or 1230, "
             "even if nothing was heard at that minute" + (" (a time alone means the first day; add the date as 08 Oct 12:30 "
                                                           "for another day)." if multi_day else "."))
    t0 = _block_start(start_label, all_stamps, fmt)
    if t0 is None:
        c3.caption(f"⚠ Couldn't read '{start_label}'. Type a time like 12:30; using {fmt(all_stamps[0])} for now.")
        t0 = all_stamps[0]
    pattern = patterns[order]
    day0 = t0.normalize()
    with _box("Paste a schedule instead (for uneven blocks)"):
        pasted = st.text_area("One block per line, like 16:40-17:12 A", key=f"blk_paste_{sig}", height=110,
                              help="Use this when the blocks are not all the same length, or when a switch came a cycle early or "
                                   "late. The antenna letter is optional: leave it off and the blocks follow your A, B order. "
                                   "A line that starts earlier than the one above it is taken to be after midnight UTC.")
    schedule, bad_lines = _parse_schedule(pasted, day0, pattern)
    if bad_lines:
        st.warning("Couldn't read: " + "; ".join(bad_lines[:3]) + (" and more" if len(bad_lines) > 3 else "")
                   + ". Use lines like 16:40-17:12 A.")
    pasted_used = bool(schedule)
    if not schedule:
        schedule = [(pattern[k % len(pattern)], t0 + pd.Timedelta(minutes=k * block_min),
                     t0 + pd.Timedelta(minutes=(k + 1) * block_min)) for k in range(n_blocks)]
    chain = False if pasted_used else st.checkbox(
        "Each block ends when the next one starts", value=True, key=f"blk_chain_{sig}",
        help="Then you only have to edit the From times: the To of a block follows the From of the next one, and the last block "
             "runs for the block length. Untick it to set every end time yourself, for blocks with a gap between them.")
    stamp = lambda ts: f"{ts:%H:%M}" + (f" +{(ts.normalize() - day0).days}d" if ts.normalize() > day0 else "")
    columns = {"Block": range(1, len(schedule) + 1), "Antenna": [a for a, _, _ in schedule],
               "From (UTC)": [stamp(lo) for _, lo, _ in schedule]}
    if not chain:
        columns["To (UTC)"] = [stamp(hi) for _, _, hi in schedule]
    table = pd.DataFrame(columns)
    if chain:
        st.caption("Each block is one antenna until the next block starts, so you only need to edit the From times (the last block "
                   "runs for the block length). Change an Antenna cell if you switched differently, or clear it to leave a block "
                   "out. A time after midnight UTC is shown with +1d.")
    else:
        st.caption("Each block is one antenna for its whole time. Change an Antenna cell if you switched differently, or clear it to "
                   "leave a block out. The From and To times can be edited too, for blocks that ran a little long or short; add +1 "
                   "after a time that falls on the next UTC day.")
    config = {"Antenna": st.column_config.SelectboxColumn("Antenna", options=["A", "B"],
                                                          help="A, B, or empty to leave this block out."),
              "From (UTC)": st.column_config.TextColumn("From (UTC)", help="Like 12:30, or 00:10 +1 for the next day.")}
    if not chain:
        config["To (UTC)"] = st.column_config.TextColumn("To (UTC)", help="Like 13:00, or 00:40 +1 for the next day.")
    edited = st.data_editor(
        table, hide_index=True, width="stretch",
        key=f"blocks_{pattern}_{block_min}_{n_blocks}_{t0:%d%H%M}_{abs(hash(pasted)) % 10**8}_{int(chain)}", disabled=["Block"],
        column_config=config)
    blocks, problems = [], []
    rows_list = list(edited.iterrows())
    los = [_cell_time(r["From (UTC)"], day0) for _, r in rows_list]
    if chain:
        his = []
        for i, lo in enumerate(los):
            later = [x for x in los[i + 1:] if x is not None and lo is not None and x > lo]
            his.append(later[0] if later else (lo + pd.Timedelta(minutes=block_min) if lo is not None else None))
    else:
        his = [_cell_time(r["To (UTC)"], day0) for _, r in rows_list]
    for (n, row), lo, hi in zip(rows_list, los, his):
        if row["Antenna"] not in ("A", "B"):
            continue
        if lo is None or hi is None:
            problems.append(f"block {int(row['Block'])}")
            continue
        if hi <= lo:  # a To that is not later than its From is taken to be after midnight
            hi += pd.Timedelta(days=1)
        blocks.append((int(row["Block"]), row["Antenna"], lo, hi))
    if problems:
        st.warning("Couldn't read the times for " + ", ".join(problems[:5]) + ". Use a time like 12:30; those blocks are left out.")
    overlaps = [(x[0], y[0]) for i, x in enumerate(blocks) for y in blocks[i + 1:] if x[2] < y[3] and y[2] < x[3]]
    if overlaps:
        st.warning(f"Blocks {overlaps[0][0]} and {overlaps[0][1]} overlap, so a transmission in the overlap counts for the first one.")
    labels = {}
    for t in all_stamps:
        for _, antenna, lo, hi in blocks:
            if lo <= t < hi:
                labels[t] = antenna
                break
    with _box("The blocks as the test will use them (From, To and what each caught)"):
        caught = pd.DataFrame([{"Block": n, "Antenna": a, "From (UTC)": stamp(lo), "To (UTC)": stamp(hi),
                                "Transmissions": sum(lo <= t < hi for t in all_stamps),
                                "Receivers": int(spots[(spots["time"] >= lo) & (spots["time"] < hi)]["spotter"].nunique())}
                               for n, a, lo, hi in blocks])
        st.dataframe(caught, hide_index=True, width="stretch")
    if "A" not in labels.values() or "B" not in labels.values():
        st.warning("There are no transmissions in at least one antenna's blocks. Check the start time, block length and "
                   "number of blocks against your notes.")
        return None
    sched_start, sched_end = min(b[2] for b in blocks), max(b[3] for b in blocks)
    if sched_end > all_stamps[-1] + pd.Timedelta(minutes=2):
        st.caption("\u26a0 The last block runs past the newest transmission in the data, so it may be cut short.")
        if sched_end.normalize() > all_stamps[-1].normalize() or all_stamps[-1].hour >= 23:
            st.info("This test runs past midnight UTC but the spots loaded stop there. In the sidebar choose **Date range** and "
                    "set **To** to the next day, then click **Load spots** again.")
    in_test = spots[spots["time"].isin(list(labels))]
    in_test = in_test.assign(antenna=in_test["time"].map(labels))
    spots_a, spots_b = in_test[in_test["antenna"] == "A"], in_test[in_test["antenna"] == "B"]
    per_a, per_b = spots_a.groupby("spotter").size(), spots_b.groupby("spotter").size()
    both = per_a.index.intersection(per_b.index)
    if len(both):
        st.caption(f"{spots_a['time'].nunique()} transmissions on A and {spots_b['time'].nunique()} on B. At the "
                   f"{len(both)} receivers that heard both, the middle receiver has {int(per_a[both].median())} reports on A and "
                   f"{int(per_b[both].median())} on B. A long test aims for {DALE_MIN_SPOTS} or more on each; the published method keeps only receivers with {DALE_SITE_MIN}.")
    lengths = [(hi - lo).total_seconds() / 60 for _, _, lo, hi in blocks]
    return {"a": spots_a, "b": spots_b, "time_based": True, "alternating": False, "labeled": False, "blocks": True,
            "t0": sched_start, "t_end": sched_end, "block_min": int(round(float(np.median(lengths)))), "n_blocks_set": len(blocks),
            "n_blocks": len(blocks), "default": {"A": "A", "B": "B"},
            "schedule": [(n, a, lo, hi, sum(lo <= t < hi for t in all_stamps)) for n, a, lo, hi in blocks]}


def _azimuth_points(ta_all, tb_all, an):
    """One row per receiver for the world map: where it is (km, bearing), whether only A, only B or both heard it, how much
    stronger A was than B (for both, from the same numbers the scatter chart uses), and each antenna's SNR there."""
    both = ta_all.index.intersection(tb_all.index)
    gap = an["shared_all"]["gap"] if "shared_all" in an else pd.Series(dtype=float)
    rows = [(ta_all.at[r, "km"], ta_all.at[r, "bearing"], "only_a", np.nan, ta_all.at[r, "snr"], np.nan)
            for r in ta_all.index.difference(both)]
    rows += [(tb_all.at[r, "km"], tb_all.at[r, "bearing"], "only_b", np.nan, np.nan, tb_all.at[r, "snr"])
             for r in tb_all.index.difference(both)]
    rows += [(ta_all.at[r, "km"], ta_all.at[r, "bearing"], "both", float(gap.get(r, ta_all.at[r, "snr"] - tb_all.at[r, "snr"])),
              ta_all.at[r, "snr"], tb_all.at[r, "snr"]) for r in both]
    return pd.DataFrame(rows, columns=["km", "bearing", "group", "gap", "snr_a", "snr_b"])


def _azimuth_map_section(points, an, names, noun, units, home, callsign, scale):
    """The world map for a comparison, with a choice of what the cells show and how much of the world to draw."""
    k = KM_PER_MILE if units == "mi" else 1
    views = _views(units)
    layers = {"Which antenna is stronger": "advantage", f"SNR with {names['A']}": "snr_a", f"SNR with {names['B']}": "snr_b",
              "Receivers only": "dots"}
    c1, c2 = st.columns([3, 2])
    layer_name = c1.radio("Colour the map by", list(layers), horizontal=True, key="azmap_layer")
    layer = layers[layer_name]
    show_dots = c2.checkbox("Show the receivers", value=False, key="azmap_dots") or layer == "dots"
    choice = st.radio("How much of the world", list(views), index=1, horizontal=True, key="azmap_view")
    radius = views[choice]
    map_col, info_col = st.columns([3, 2])
    map_col.pyplot(azimuth_map_figure(home[0], home[1], points, names, callsign, units, radius, layer, show_dots, scale,
                                    label_min=3 if noun == "skimmer" else 5),
                   width="stretch")
    outside = int((points["km"] > radius).sum())
    counts = points["group"].value_counts()
    info_col.markdown(
        f"**{int(counts.get('both', 0))}** {noun}s heard both \u00b7 **{int(counts.get('only_a', 0))}** only {names['A']} \u00b7 "
        f"**{int(counts.get('only_b', 0))}** only {names['B']}" + (f" \u00b7 {outside} beyond this view" if outside else ""))
    for side in ("a", "b"):
        far = an["dx"][side]
        if far:
            info_col.markdown(f"Farthest {noun} for {names[side.upper()]}: **{far['km'] / k:,.0f} {units}** ({far['who']})")
    info_col.caption(
        "The station is at the centre. Distance from the centre is true distance from you and the angle is true compass "
        "bearing, so a straight line from the centre is the path the signal takes. Each coloured cell is one direction "
        f"(30\u00b0) and distance band, shown only where at least {MIN_CELL} {noun}s were heard, with its number written in it from {3 if noun == 'skimmer' else 5}. "
        + ("Blue cells are where A was stronger and orange where B was; a brighter colour is a bigger difference and a more solid "
           "cell rests on more receivers." if layer == "advantage" else
           "The colour is the average SNR in the cell." if layer in ("snr_a", "snr_b") else
           "Filled dots heard both antennas, open rings only one.")
        + " Blank means nobody there heard it, which also happens wherever there are no receivers, such as over the oceans.")


def _sector_detail(shared, adv, label, width_deg, names, noun):
    """One sentence about the chosen direction: how many receivers, who is stronger, and the 95% range."""
    row = adv[adv["label"] == label]
    if row.empty or int(row["n"].iloc[0]) == 0:
        return f"No {noun} in that direction."
    center, n = float(row["center"].iloc[0]), int(row["n"].iloc[0])
    sector = (((shared["bearing_a"] + width_deg / 2) % 360) // width_deg).astype(int)
    d = shared[sector == int(round(center / width_deg))]["delta"].to_numpy()
    text = f"**{label}: {n} {noun}{'s' if n != 1 else ''} heard both.** {_lead_text(names, float(d.mean()))}."
    if n >= 3:
        t_value = t_crit(n - 1)
        half = t_value * float(d.std(ddof=1)) / np.sqrt(n)
        text += f" 95% range: {_range_text(names, float(d.mean()) - half, float(d.mean()) + half)}."
    else:
        text += " Too few for a range."
    return text


def _shared_counts_note(an, noun):
    """Why 'heard both' and 'compared' are different numbers: pairing, and counting one place once."""
    both, every, n = an["reach"]["both"], len(an["shared_all"]), an["strength"]["n"]
    text = f"**{both} {noun}s heard both sides.**"
    if an["paired"] and every != both:
        text += (f" {every} of them heard both in the same pair of neighbouring transmissions, which is what the strength "
                 "comparison needs.")
    if n != both:
        text += f" The strength result rests on {n}."
    return text


def _linear_average_view(spots_a, spots_b, an, names, noun, locs, home, kind, callsign, paired=None, floor=None, min_km=0.0,
                         weak_pct=0, default_min=1):
    """The SNR average (the standard comparison) next to the same comparison worked out by averaging power instead of dB,
    overall and by direction, and
    when to use which."""
    st.markdown(
        "Reports use dB, a logarithmic scale. The **SNR average** (the standard comparison) measures the **typical SNR difference "
        "at each receiver**. The **linear average** converts SNR to power ratios, averages them for each antenna, then converts back "
        "to dB and subtracts. Linear power averages signal power over time, and it gives stronger "
        "signals more weight, so it needs many reports per receiver. The SNR average is the typical dB difference, and one "
        "strong report can't dominate it, so it holds up better with few reports. Both compare **relative signal strength "
        "at the same receivers**, not absolute antenna gain.")
    with st.expander("Show the arithmetic"):
        st.markdown(
            "1. For every report, turn dB into power: **power = 10 ^ (dB / 10)**\n"
            "2. Average those power numbers for each antenna.\n"
            "3. Turn each average back into dB: **dB = 10 x log10(average power)**\n"
            "4. Subtract the two. That is the relative gain of A against B.\n\n"
            "Example: reports of 0, 0, 0 and 20 dB are powers of 1, 1, 1 and 100. Their average is 25.75, which is **14.1 dB**. "
            "Averaging the dB numbers directly gives 5 dB. The single strong report counts for much more in power.")
    min_spots = st.slider(f"Only {noun}s with at least this many reports on each antenna", 1, 60, default_min,
                          key=f"pm_min_spots_{default_min}",
                          help=f"A long test aims for {DALE_MIN_SPOTS} or more per receiver on each antenna, and the published "
                               f"method keeps only receivers with {DALE_SITE_MIN} or more. A short test can't reach that, but "
                               "raising this drops receivers heard only once or twice, whose single reports can swing a power "
                               "average.")
    s_ = an["strength"]
    pm = power_method(spots_a, spots_b, locs, home, min_spots=min_spots, floor=floor, offset=an["power"]["offset"], min_km=min_km,
                      weak_pct=weak_pct)
    if paired is not None and (paired[0]["time"].nunique() + paired[1]["time"].nunique()
                               != spots_a["time"].nunique() + spots_b["time"].nunique()):
        st.caption(f"The linear average uses every transmission ({spots_a['time'].nunique()} on A, {spots_b['time'].nunique()} on B), "
                   "because averaging power needs no partner transmission. The SNR average compares only paired transmissions "
                   f"({paired[0]['time'].nunique()} on A, {paired[1]['time'].nunique()} on B).")
    if pm["n"] < 2:
        st.info(f"Too few {noun}s heard both antennas with that many reports each. Lower the setting above.")
        return
    if min_spots < DALE_SITE_MIN:
        strict = power_method(spots_a, spots_b, locs, home, min_spots=DALE_SITE_MIN, floor=floor, offset=an["power"]["offset"],
                              min_km=min_km, weak_pct=weak_pct)
        st.caption(f"With the published minimum of {DALE_SITE_MIN} reports on each antenna, "
                   + (f"{strict['n']} {noun}s qualify: linear average {_lead_text(names, strict['mean'])}."
                      if strict["n"] >= 2 else f"too few {noun}s qualify ({strict['n']}), so a long test is needed for that."))
    m1, m2 = st.columns(2)
    m1.metric("SNR average: typical dB difference at the same receiver", _card_lead(s_["mean"]))
    m1.caption(f"{s_['n']} {noun}s. 95% range: {_range_text(names, s_['lo'], s_['hi'])}.")
    m2.metric("Linear average: each receiver's average power, A against B", _card_lead(pm["mean"]))
    m2.caption(f"{pm['n']} {noun}s. 95% range: {_range_text(names, pm['lo'], pm['hi'])}."
               + (" Reports or receivers you left out above are left out here, but not from the SNR average." if floor is not None or min_km or weak_pct else ""))
    if names["A"] != "A" or names["B"] != "B":
        st.caption(f"A is {names['A']}. B is {names['B']}.")
    gap = abs(s_["mean"] - pm["mean"]) if not np.isnan(s_["mean"]) else np.nan
    if not np.isnan(gap):
        if gap < 1.0:
            st.success(f"The two agree within 1 dB ({gap:.1f} dB apart), so how it is averaged isn't driving the answer. That is a consistency check, not proof.")
        else:
            st.warning(f"The two are {gap:.1f} dB apart. That usually means a few strong reports, or {noun}s with only one or "
                       "two reports each, are pulling the linear average. Try raising the minimum reports above.")
    if pm["near_floor"] >= 0.1:
        st.warning(f"{pm['near_floor']:.0%} of the reports are at or below {WSPR_FLOOR_DB} dB, the edge of what WSPR can decode. "
                   "Signals too weak to decode never appear at all, so the weaker antenna's weakest reports are missing, which can "
                   "shrink the apparent difference. Ticking the box to ignore these reports is a check on how much they matter, "
                   "not a correction. Compare how many receivers heard each antenna too.")
    elif pm["dropped"]:
        st.caption(f"{pm['dropped']} reports at or below {WSPR_FLOOR_DB} dB were ignored.")

    st.subheader("By direction, both ways")
    width = st.radio("Sector width", [45, 30, 15], horizontal=True, key="lin_width", format_func=lambda w: f"{w}\u00b0")
    adv_default = direction_advantage(an["shared"], width)
    adv_linear = direction_advantage(pm["sites"].rename(columns={"gain": "delta"}), width)
    tops = [float(a["delta"].abs().max()) for a in (adv_default, adv_linear) if a["delta"].notna().any()]
    shared_scale = max(4.0, float(np.ceil(max(tops) / 2) * 2)) if tops else 4.0
    left, right = st.columns(2)
    left.markdown("**SNR average**")
    left.pyplot(direction_advantage_chart(adv_default, names, width, callsign, scale=shared_scale), width="stretch")
    right.markdown("**Linear average**")
    right.pyplot(direction_advantage_chart(adv_linear, names, width, callsign, scale=shared_scale), width="stretch")
    both = adv_default.assign(linear=adv_linear["delta"], n_linear=adv_linear["n"])
    both = both[(both["n"] > 0) | (both["n_linear"] > 0)]
    st.dataframe(pd.DataFrame({
        "Direction": both["label"], f"{noun.capitalize()}s": both["n"].astype(int),
        "SNR average": [_gain_text(v) for v in both["delta"]], "Linear average": [_gain_text(v) for v in both["linear"]]}),
        width="stretch", hide_index=True, height=min(38 * (len(both) + 1), 360))
    st.caption("Both charts use the same scale, so you can compare bar lengths. Where the two differ a lot in one direction, "
               "a few strong reports from there are probably pulling the linear average.")

    with st.expander(f"Receiver by receiver, both ways"):
        t = pm["table"]
        default = an["shared_all"]["delta"] if "shared_all" in an else pd.Series(dtype=float)
        view = pd.DataFrame({
            noun.capitalize(): t.index, "Reports A": t["spots_a"].astype(int).to_numpy(),
            "Reports B": t["spots_b"].astype(int).to_numpy(), "Linear average": [_gain_text(v) for v in t["gain"]],
            "SNR average": [_gain_text(default.get(i, np.nan)) for i in t.index]}).sort_values("Reports A", ascending=False)
        st.dataframe(view, width="stretch", hide_index=True, height=min(38 * (len(view) + 1), 380))

    st.subheader("Which should you use?")
    left, right = st.columns(2)
    left.markdown(
        "**SNR average: the typical dB difference at the same receiver**\n\n"
        "Best for a **quick test** (8 to 16 transmissions, switching after each one). A and B are measured minutes apart, so "
        "fading and band changes mostly cancel, and one very strong report can't dominate. It gives a verdict and ranges by "
        "direction and distance.\n\n"
        "*Limits:* it averages in dB, which isn't a power average, and it assumes conditions barely change between "
        "neighbouring transmissions, which isn't always true (WSPR SNR can move several dB between 2-minute windows). It "
        "relies on having many receivers to average that out.")
    right.markdown(
        "**Linear average: each receiver's average power**\n\n"
        "Best for a **long test**: run one antenna for 30 minutes to an hour, then the other, and repeat for several hours. "
        "Each receiver then hears you 20 to 40 times on each antenna. For every receiver, the site averages all its reports "
        "for A and all its reports for B (as power, not dB) and compares the two averages.\n\n"
        "Any single report bounces up and down with fading, but over 20 to 40 reports those bounces even out, so the "
        "averages are steady. The result no longer depends on two particular transmissions lining up, and the 95% range "
        "(how far off the answer could plausibly be) comes out narrow.\n\n"
        "*Limits:* it needs many reports per receiver, so on a short test one lucky report can swing the result. Strong "
        "reports count for far more than weak ones, so a single peak can pull the average up. "
        "Reports right at WSPR's -28 dB floor throw it off, and a long run mixes in whatever the band did meanwhile.")
    st.markdown(
        "**In practice:** if the two agree within 1 dB, the answer holds either way. If they don't, look at the reports near the "
        "floor and raise the minimum reports. To use the long-test method as intended, choose **In long blocks** under *Your test* "
        "and give the start time, block length and number of blocks.")


def _method_summary(spots_a, spots_b, an, names, noun, locs, home):
    """One paragraph for the PDF: the SNR average next to the linear average, and whether they agree."""
    s_ = an["strength"]
    pm = power_method(spots_a, spots_b, locs, home, offset=an["power"]["offset"])
    if pm["n"] < 2 or np.isnan(s_["mean"]):
        return ""
    gap = abs(s_["mean"] - pm["mean"])
    return (f"SNR average (typical dB difference at the same {noun}): {_lead_text(names, s_['mean'])}. "
            f"Linear average (each {noun}'s average power, A against B): {_lead_text(names, pm['mean'])}. "
            + (f"The two agree within 1 dB ({gap:.1f} dB apart)." if gap < 1.0 else
               f"The two are {gap:.1f} dB apart, usually a few strong reports or {noun}s with only one or two reports."))


def _range_text(names, lo, hi):
    """'B ahead by 6.3 to 14.7 dB' when the whole range favours one side; otherwise 'A ahead by 1.4 dB to B ahead by 0.9 dB'."""
    if lo > 0 and hi > 0:
        return f"{names['A']} ahead by {lo:.1f} to {hi:.1f} dB"
    if lo < 0 and hi < 0:
        return f"{names['B']} ahead by {-hi:.1f} to {-lo:.1f} dB"
    return f"{_lead_text(names, lo)} to {_lead_text(names, hi)}"


def _gain_text(v):
    return "-" if pd.isna(v) or abs(v) < 0.05 else f"{'A' if v > 0 else 'B'} +{abs(v):.1f}"


def _gain_summary_view(summary, first_col, noun):
    """A gain_summary table as text for the screen and the PDF: the group, how many receivers, the average and its range."""
    def fmt(v):
        return "\u2013" if pd.isna(v) else f"{v:+.1f}"

    def rng(row):
        return "\u2013" if pd.isna(row["low"]) else f"{row['low']:+.1f} to {row['high']:+.1f}"

    return pd.DataFrame({
        first_col: summary["group"], f"{noun.capitalize()}s": summary["receivers"],
        "Average gain B \u2212 A (dB)": [fmt(v) for v in summary["mean"]],
        "95% range": [rng(r) for _, r in summary.iterrows()],
        "Note": ["" if n >= BIN_MIN_SHARED else ("none" if n == 0 else f"only {n}, a hint") for n in summary["receivers"]]})


def _card_lead(v):
    """A short value for a big summary card, such as 'B +4.9 dB', so a long antenna name can't cut it off."""
    if pd.isna(v) or abs(v) < 0.05:
        return "no difference"
    return f"{'A' if v > 0 else 'B'} +{abs(v):.1f} dB"


def _data_used(all_spots, spots, map_a, map_b, spots_a, spots_b, an, ta_all, tb_all, noun, sep, unpaired, listening_note, ghosts):
    """What went into a comparison and what was left out, as a short table and a few sentences, for the screen and the PDF."""
    n = lambda x: f"{int(x):,}"  # noqa: E731
    in_test, compared = len(map_a) + len(map_b), len(spots_a) + len(spots_b)
    heard_a, heard_b = set(map_a["spotter"]), set(map_b["spotter"])
    both_all = heard_a & heard_b
    shared = an["shared_all"]
    unlocated = (heard_a | heard_b) - (set(ta_all.index) | set(tb_all.index))
    reports = pd.concat([spots_a.groupby("spotter").size().reindex(shared.index),
                         spots_b.groupby("spotter").size().reindex(shared.index)], axis=1).min(axis=1).fillna(0)
    enough = {t: int((reports >= t).sum()) for t in (5, 20, 40)}
    rows = {"Spots in the test": [n(len(map_a)), n(len(map_b))],
            "Spots used in the comparison": [n(len(spots_a)), n(len(spots_b))],
            f"{noun.capitalize()}s that heard it": [n(len(heard_a)), n(len(heard_b))]}
    if sep.get("time_based"):
        rows["Transmissions"] = [n(map_a["time"].nunique()), n(map_b["time"].nunique())]
    lines = [f"{n(len(all_spots))} spots were loaded; {n(len(spots))} remain after your filters; {n(in_test)} are in the test and "
             f"{n(compared)} were used in the comparison.",
             f"{len(heard_a)} {noun}s heard A and {len(heard_b)} heard B; {len(both_all)} heard both. {len(shared)} of those have a "
             f"known position and were compared.",
             f"With at least 5, 20 and 40 reports on each antenna: {enough[5]}, {enough[20]} and {enough[40]} {noun}s."
             + (f" A long test aims for {DALE_MIN_SPOTS} or more, and the published method keeps {DALE_SITE_MIN}."
                if sep.get("blocks") else "")]
    left = []
    if unpaired:
        left.append(f"{unpaired} transmission{'s' if unpaired != 1 else ''} with no partner on the other antenna")
    if ghosts:
        left.append(f"{ghosts} cycle{'s' if ghosts != 1 else ''} heard by only 1 or 2 receivers")
    if listening_note and listening_note["kept"] < listening_note["total"]:
        gone = listening_note["total"] - listening_note["kept"]
        left.append(f"{gone} {noun}{'s' if gone != 1 else ''} not listening during both sides")
    if unlocated:
        left.append(f"{len(unlocated)} {noun}{'s' if len(unlocated) != 1 else ''} with no known position")
    if len(spots) - in_test > 0:
        left.append(f"{n(len(spots) - in_test)} spots outside the test")
    total_tx, used_tx = spots["time"].nunique(), pd.concat([map_a["time"], map_b["time"]]).nunique()
    if sep.get("time_based") and not sep.get("blocks") and total_tx >= 20 and used_tx < total_tx / 2:
        lines.insert(1, f"⚠ This quick test uses {used_tx} of the {total_tx} transmissions in the data. For a long test with "
                        "one antenna at a time for 30 minutes or more, choose In long blocks.")
    lines.append("Left out: " + "; ".join(left) + "." if left else "Nothing was left out.")
    label = (f"Data used: {n(compared)} spots from {len(set(spots_a['spotter']) | set(spots_b['spotter']))} {noun}s, "
             f"{len(shared)} compared")
    return {"label": label, "rows": rows, "lines": lines}


def _thin_slots(listening, slots):
    """The transmission slots in which far fewer receivers than usual reported anything on the band: a sign that wspr.live is
    missing most of the reports for that slot (seen at the start of each hour in some 2024 data). `listening` is from
    fetch_listening and `slots` the times of the test's transmissions. Returns the affected times, oldest first."""
    per_slot = listening.groupby("time")["spotter"].nunique()
    if len(per_slot) < 10 or per_slot.median() < 100:  # a quiet band has few receivers anyway, so nothing to compare with
        return []
    return sorted(t for t in slots if per_slot.get(t, 0) < 0.25 * per_slot.median())


def _test_setup(sep, names, kind, callsign, label, file_date, bands, pa, pb, offset):
    """What went into a comparison, for the PDF's Test setup section: the settings as rows, and the schedule of the test (the
    blocks, or the transmissions in order) as a table, so the analysis can be repeated from the report alone."""
    watts = lambda dbm: 10 ** ((dbm - 30) / 10)  # noqa: E731
    rows = [("Transmitter" if kind == "WSPR" else "Callsign", callsign), ("Location", label),
            ("Data source", "WSPR (wspr.live)" if kind == "WSPR" else "Reverse Beacon Network"),
            ("Date (UTC)", _pretty_date(file_date)), ("Band", bands), ("Antenna A", names["A"]), ("Antenna B", names["B"])]
    multi_day = len(file_date) > 8
    fmt = (lambda t: f"{t:%d %b %H:%M}") if multi_day else (lambda t: f"{t:%H:%M}")
    table = None
    if sep.get("blocks"):
        rows.append(("Switching", f"In long blocks, {sep['n_blocks']} blocks, about {sep['block_min']} minutes each"))
        table = (["Block", "Antenna", "From (UTC)", "To (UTC)", "Transmissions"],
                 [[str(n), a, fmt(lo), fmt(hi), str(c)] for n, a, lo, hi, c in sep["schedule"]], [0.12, 0.14, 0.26, 0.26, 0.22])
    elif sep.get("labeled"):
        rows.append(("Switching", f"After every transmission ({len(sep['log'])} transmissions, listed below)"))
        table = (["Transmission (UTC)", "Antenna"], [[fmt(t), lab] for t, lab in sep["log"][:80]], [0.5, 0.5])
    else:
        rows.append(("Switching", "By frequency: " + sep["default"]["A"] + " and " + sep["default"]["B"]))
    if kind == "WSPR":
        rows.append(("Transmit power", f"A {pa:.0f} dBm ({watts(pa):.2g} W), B {pb:.0f} dBm ({watts(pb):.2g} W)"))
    if offset:
        rows.append(("Power correction", f"B shifted by {offset:+.1f} dB to match A's power"))
    note = ("To repeat this analysis, load the same callsign, date and band in the app, choose the same way of switching, and "
            "enter these blocks or transmissions.") if table else ""
    return {"rows": rows, "table": table, "note": note}


def _summary_tiles_html(an, names, rounds, noun):
    """Three tiles above the verdict: how much stronger the leader was, how many receivers that rests on, and who won the rounds."""
    s, r = an["strength"], an["reach"]
    color = {"A": COLOR_A, "B": COLOR_B}
    if s["n"]:
        side = "A" if s["mean"] > 0 else "B"
        lo, hi = (s["lo"], s["hi"]) if side == "A" else (-s["hi"], -s["lo"])
        if abs(s["mean"]) < 0.05:
            big, sub, edge = "0.0 dB", "no difference at the receivers that heard both", COLOR_TIE
        else:
            who = f"{names[side]} stronger" if s["call"] in ("A", "B") else f"{names[side]} ahead, but not decisive"
            big, sub, edge = f"{abs(s['mean']):.1f} dB", f"{who} (95% range {lo:.1f} to {hi:.1f} dB)", color[side]
    else:
        big, sub, edge = "—", f"no {noun} heard both, so strength can't be compared", COLOR_TIE
    tiles = [("Signal advantage", big, sub, edge),
             (f"{noun.capitalize()}s that heard both", f"{s['n']}", f"{r['only_a']} heard only A, {r['only_b']} heard only B", COLOR_TIE)]
    if rounds is not None and len(rounds) >= 3:
        a_r, b_r = int((rounds["delta"] > 0).sum()), int((rounds["delta"] < 0).sum())
        tiles.append(("Rounds won", f"A {a_r} · B {b_r}", f"of {len(rounds)} rounds, an A transmission and the B one after it", COLOR_TIE))
    elif s["n"]:
        tiles.append((f"{noun.capitalize()}s that were stronger on", f"A {s['wins_a']} · B {s['wins_b']}", f"tied at {s['ties']}", COLOR_TIE))
    cells = "".join(
        f'<div style="flex:1 1 210px;border:1px solid rgba(128,128,128,.3);border-left:5px solid {edge};border-radius:10px;'
        f'padding:10px 14px"><div style="font-size:.8rem;opacity:.7">{head}</div>'
        f'<div style="font-size:1.6rem;font-weight:700;line-height:1.3">{big}</div>'
        f'<div style="font-size:.82rem;opacity:.75">{sub}</div></div>' for head, big, sub, edge in tiles)
    return f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin:2px 0 10px 0">{cells}</div>'


def _setup_line(sep, names, kind):
    """One line saying what the comparison assumed, so a wrong guess is spotted without opening anything."""
    if sep.get("blocks"):
        how = f"switched in long blocks ({sep['n_blocks']} blocks of about {sep['block_min']} minutes)"
    elif sep.get("labeled"):
        log = sep["log"]
        how = (f"switched after every transmission ({len(log)} transmissions from {log[0][0]:%H:%M} UTC, order "
               + " ".join(str(lab) for _, lab in log[:8]) + (" …" if len(log) > 8 else "") + ")")
    elif sep.get("time_based"):
        how = "separated by time window"
    else:
        how = "separated by frequency"
    who = f"A is {names['A']} and B is {names['B']}, " if (names["A"], names["B"]) != ("A", "B") else ""
    return (f"Assumed setup: {who}{how}. If that is not what you did, open \"How this test was set up\" just below to change it.")


def compare_view(spots, locs, home, label, callsign, file_date, tiles, show_all, units, split, gap_khz,
                 scale, noun, kind, all_spots):
    """Compare antennas: separate `spots` into an A side and a B side (by frequency, time window, timed blocks,
    or every other transmission), then say which one is getting out better, by how much, and in which directions."""
    top = st.container()  # the answer comes first; the setup it rests on sits just below it
    notes = top.container()
    setup = st.container()
    details = st.container()
    confirmed = bool(st.session_state.get("setup_ok"))
    with setup:  # open until you say it is right, then it folds away and the result is all you see
        box = st.expander("How this test was set up" if confirmed else "Check how this test was set up", expanded=not confirmed)
    with box:
        if confirmed:
            st.caption("Change anything here and the result above updates.")
        else:
            st.info("I guessed how you switched, from the spots. Check it below and fix anything that is wrong, then confirm "
                    "and I will work out the result.")
        name_a, name_b = _name_inputs()
        if split == SPLIT_FREQ:
            sep = _separate_by_frequency(spots, gap_khz)
        else:
            sep = _separate_by_log(spots)
        if sep is None:
            return
        spots_a, spots_b = sep["a"], sep["b"]
        if spots_a.empty or spots_b.empty:
            st.warning("One side has no spots. Adjust the selection above, or the filters in the sidebar.")
            return
        if not confirmed:
            st.button("This matches what I did: show the result", type="primary", key="confirm_setup",
                      on_click=lambda: st.session_state.update(setup_ok=True))
            return
        map_a, map_b = spots_a, spots_b  # the maps and timeline show everything that was heard
        unpaired = 0
        if kind == "WSPR" and sep.get("time_based"):
            if sep.get("labeled") and count_pairs(spots_a, spots_b) >= 1:
                # An alternating test is judged pair by pair, and a pair has exactly one transmission on each antenna, so
                # the time on the air is equal by construction. A transmission with no partner on the other antenna has
                # nothing to be compared with, so it is left out of the comparison (it still shows on the maps). Thinning
                # the longer side at random instead could throw away a paired transmission and leave nothing to compare.
                spots_a, spots_b = spots_a[spots_a["round"].notna()], spots_b[spots_b["round"].notna()]
                sep["exposure"] = None
                unpaired = (map_a["time"].nunique() - spots_a["time"].nunique()
                            + map_b["time"].nunique() - spots_b["time"].nunique())
                if unpaired:
                    notes.caption(f"\u2139\ufe0f {unpaired} transmission{'s' if unpaired != 1 else ''} had no partner on the other "
                               "antenna, so they are left out of the A/B comparison (they still show on the maps).")
            else:
                # For WSPR the time on the air that matters is the transmissions that actually happened (2 minutes each),
                # not the scheduled minutes: a transmitter that picks its slots at random can put far more of them on one side.
                n_a, n_b = spots_a["time"].nunique(), spots_b["time"].nunique()
                sep["exposure"] = (2.0 * n_a, 2.0 * n_b) if abs(n_a - n_b) > 1 else None
        listening_note = None
        if kind == "WSPR" and sep.get("time_based"):
            bands = set(spots_a["band"]) | set(spots_b["band"])
            if len(bands) == 1:
                times = pd.concat([spots_a["time"], spots_b["time"]])
                try:
                    with st.spinner("Checking which receivers were listening\u2026"):
                        listening, _ = load_listening(next(iter(bands)), times.min().to_pydatetime(),
                                                      (times.max() + pd.Timedelta(minutes=2)).to_pydatetime())
                    eligible = eligible_receivers(spots_a, spots_b, listening)
                    heard = set(spots_a["spotter"]) | set(spots_b["spotter"])
                    listening_note = {"kept": len(heard & eligible), "total": len(heard)}
                    thin = _thin_slots(listening, set(times.unique()))
                    if thin:
                        notes.warning(f"wspr.live seems to be missing most of the reports for {len(thin)} of your "
                                   f"{times.nunique()} transmissions (for example {', '.join(f'{x:%H:%M}' for x in thin[:4])} UTC): far "
                                   "fewer receivers than usual reported anything on this band in those slots. Spots from them are "
                                   "undercounted, which can favour whichever antenna was on the air at other times. This is a gap in "
                                   "wspr.live's own data, mostly in older dates; a recent test is not affected.")
                    spots_a = spots_a[spots_a["spotter"].isin(eligible)]
                    spots_b = spots_b[spots_b["spotter"].isin(eligible)]
                except WsprError as exc:
                    notes.warning(f"Couldn't check which receivers were listening ({exc}). Receivers that only listen in some "
                               "time slots may make one side look like it reached more.")
                if spots_a.empty or spots_b.empty:
                    notes.warning("No receiver was listening during both sides' transmissions, so there is nothing fair to "
                               "compare. Try a longer test.")
                    return
        names = {"A": name_a or sep["default"]["A"], "B": name_b or sep["default"]["B"]}
        if names["A"] == names["B"]:  # the names become table headings, so they have to differ
            names = {"A": names["A"] + " (A)", "B": names["B"] + " (B)"}

        # ---- power: WSPR sends it with every spot; for RBN you can say what you ran
        with _box("Power"):
            if kind == "WSPR":
                pa, pb = median_power_dbm(spots_a), median_power_dbm(spots_b)
                watts = lambda dbm: 10 ** ((dbm - 30) / 10)  # noqa: E731
                st.caption(f"Transmit power from the spots: {names['A']} {pa:.0f} dBm ({watts(pa):.2g} W), "
                           f"{names['B']} {pb:.0f} dBm ({watts(pb):.2g} W).")
                normalize = st.checkbox("Correct for any power difference (recommended)", value=True,
                                        help="If the two tests used different power, B's SNR is shifted to A's power so the "
                                             "antennas are compared, not the transmitters.")
            else:
                st.caption("Optional. If the two tests ran at different power, enter both so the comparison is about "
                           "the antennas, not the transmitters. Leave at 0 if you don't know or they were equal.")
                c1, c2 = st.columns(2)
                watts_a = c1.number_input(f"Power for {names['A']} (watts)", min_value=0.0, value=0.0, step=5.0)
                watts_b = c2.number_input(f"Power for {names['B']} (watts)", min_value=0.0, value=0.0, step=5.0)
                pa, pb = db_from_watts(watts_a), db_from_watts(watts_b)
                normalize = True

        an = analyze(spots_a, spots_b, locs, home, pa, pb, normalize, units, exposure=sep.get("exposure"))
        # Describe what was heard the way the maps do, from every receiver that heard each side. The fairness checks (listening
        # during both sides, real A/B pairs) decide who counts for the reach and strength verdict, but they would otherwise
        # hide a receiver from the "farthest" figure that the map plainly shows.
        ta_all, tb_all = receiver_table(map_a, locs, home), receiver_table(map_b, locs, home)
        far_all_a, far_all_b = _farthest(ta_all), _farthest(tb_all)
        if far_all_a and far_all_b:
            an["dx"] = {**an["dx"], "a": far_all_a, "b": far_all_b}
        rounds = None
        if sep.get("alternating") and "round" in spots_a:
            rounds = round_consistency(spots_a, spots_b, locs, home, an["power"]["offset"])
        n_pairs = count_pairs(map_a, map_b)
        sequential = not sep.get("alternating")
        if sep.get("labeled"):  # a log of mostly unpaired transmissions (A A A B B B) is a one-after-the-other test
            transmissions = map_a["time"].nunique() + map_b["time"].nunique()
            sequential = transmissions > 0 and 2 * n_pairs < 0.5 * transmissions
        if sep.get("blocks"):
            sequential = sep["n_blocks"] < 4  # a few alternations average out the changing band; one A then one B does not
        close_in_time = None
        if sequential and not sep.get("labeled") and not sep.get("blocks"):
            # Two frequencies or two windows sent within a few minutes of each other saw the same band conditions, so the
            # warning that the time of day could explain the difference does not apply.
            all_times = pd.concat([map_a["time"], map_b["time"]])
            span_minutes = (all_times.max() - all_times.min()).total_seconds() / 60
            if span_minutes <= SIMULTANEOUS_MINUTES:
                sequential, close_in_time = False, span_minutes
        verdict = build_verdict(an, names, exposure=sep.get("exposure") if sep.get("time_based") else None,
                                rounds=rounds, receiver_word=noun, sequential=sequential,
                                n_pairs=n_pairs if sep.get("alternating") else None)

        if close_in_time is not None:
            verdict["caveats"].append(
                "A and B were sent within " + (f"{close_in_time:.0f} minute{'s' if round(close_in_time) != 1 else ''}"
                                               if close_in_time >= 1 else "a minute") +
                " of each other, so they saw the same band conditions and the time of day is not a concern here.")
        if listening_note and listening_note["kept"] < listening_note["total"]:
            verdict["caveats"].append(
                f"Only receivers that were listening during both sides' transmissions are counted ({listening_note['kept']} "
                f"of {listening_note['total']}). Many WSPR receivers hop between bands and only listen in some time slots, "
                "which would otherwise look like one side reaching more.")

        r, s_, dx = an["reach"], an["strength"], an["dx"]
        k = KM_PER_MILE if units == "mi" else 1
        means_a, means_b = sector_means(ta_all), sector_means(tb_all)
        with _box("Leave out some reports or receivers (for the averages)"):
            f1, f2, f3 = st.columns(3)
            floor_on = kind == "WSPR" and f1.checkbox(
                f"Ignore reports at or below {WSPR_FLOOR_DB} dB", key="avg_floor",
                help="WSPR can only decode down to about -28 dB. A report at that edge is unreliable, and the weaker antenna's "
                     "weakest reports are often missing altogether.")
            near = float(f2.number_input(f"Leave out receivers closer than ({units})", 0, 5000, step=10, key=f"avg_near_{units}",
                                         help="Close stations can hear you by ground wave, which says little about the antenna's "
                                              "take-off angle. Leave at 0 to keep every receiver."))
            weak_pct = int(f3.number_input("Drop a station if more than this % of its reports are at the floor", 0, 100, step=5,
                                           key="avg_weak", disabled=kind != "WSPR",
                                           help="Stations that only hear you at the very edge, below about -27 dB, give weak and "
                                                "noisy numbers. Instead of ignoring just those reports, this removes the whole "
                                                "station when more than this share of its reports (both antennas together) are at "
                                                "the floor. Leave at 0 to keep every station."))
            st.caption("This changes the averages on the Receiver by receiver tab and the linear average on the Linear average tab. "
                       "The verdict, the SNR average and the other tabs are not changed.")
        avg_floor = WSPR_FLOOR_DB if floor_on else None
        avg_min_km = near * (KM_PER_MILE if units == "mi" else 1.0)
        avg_note = ", ".join(([f"reports at or below {WSPR_FLOOR_DB} dB left out"] if floor_on else [])
                             + ([f"receivers closer than {near:g} {units} left out"] if near else [])
                             + ([f"stations with more than {weak_pct}% of their reports at the floor left out"] if weak_pct else []))

    with top:
        st.markdown(_summary_tiles_html(an, names, rounds, noun), unsafe_allow_html=True)
        st.caption(_setup_line(sep, names, kind))
        st.markdown(verdict_card_html(verdict), unsafe_allow_html=True)

    with details:
        tab_result, tab_where, tab_each, tab_method, tab_maps = st.tabs(
            ["\U0001F4CA Result", "\U0001F9ED Direction & distance", "\U0001F4CB Receiver by receiver",
             "\U0001F4D0 Linear average", "\U0001F5FA️ Maps"])

        with tab_result:
            snr_note = f"at the {s_['n']} {noun}s that heard both" if s_["n"] else f"no {noun} heard both"
            avg_a = f"{an['shared']['snr_a'].mean():.1f} dB" if s_["n"] else "—"
            avg_b = f"{an['shared']['snr_b'].mean():.1f} dB" if s_["n"] else "—"
            far = lambda d: (f"{d['km'] / k:,.0f} {units}", d["who"]) if d else ("—", "")  # noqa: E731
            typical = lambda t: (f"{t['km'].median() / k:,.0f} {units}", "") if len(t) else ("—", "")  # noqa: E731
            score_rows = [
                (f"Unique {noun}s that heard you", ("equal time on the air, " if an["equalized"] else "") + ("listening during both, " if listening_note else "") + "each one counted once · more is better",
                 (r["a"], f"from {r['spots_a']:,} spots"), (r["b"], f"from {r['spots_b']:,} spots"),
                 r["call"] if r["call"] in ("A", "B") else None),
                ("Average SNR", snr_note, (avg_a, ""), (avg_b, ""), s_["call"] if s_["call"] in ("A", "B") else None),
                (f"Farthest {noun}", "your longest path", far(dx["a"]), far(dx["b"]), dx["call"]),
                ("Typical distance", "half the stations were closer than this", typical(ta_all), typical(tb_all), None),
                ("Strongest direction", "where your signal was best",
                 (best_direction(means_a), ""), (best_direction(means_b), ""), None),
            ]
            n_a, n_b = map_a["time"].nunique(), map_b["time"].nunique()
            used = _data_used(all_spots, spots, map_a, map_b, spots_a, spots_b, an, ta_all, tb_all, noun, sep, unpaired,
                              listening_note, st.session_state.get("ghost_cycles", 0))
            pdf_ctx = dict(data_used=used,
                setup=_test_setup(sep, names, kind, callsign, label, file_date, ", ".join(sorted(set(map_a["band"]) | set(map_b["band"]))),
                                  pa, pb, an["power"]["offset"]), 
                names=names, an=an, verdict=verdict, noun=noun, units=units, score_rows=score_rows, callsign=callsign, label=label,
                file_date=file_date, source="WSPR" if kind == "WSPR" else "Reverse Beacon Network",
                bands=", ".join(sorted(set(map_a["band"]) | set(map_b["band"]))),
                test=(f"Test: {n_a} transmission{'s' if n_a != 1 else ''} on A, {n_b} on B" if sep.get("labeled")
                      else "Test: A and B separated by " + ("frequency" if not sep.get("time_based") else "time")),
                time_based=bool(sep.get("time_based")), means_a=means_a, means_b=means_b,
                adv=direction_advantage(an["shared"], 45), counts_note=_shared_counts_note(an, noun),
                az_points=_azimuth_points(ta_all, tb_all, an), home=home, scale=scale,
                method=_method_summary(spots_a, spots_b, an, names, noun, locs, home),
                timeline=(map_a, map_b))
            st.download_button("\u2B07\ufe0f Download report (PDF)", data=lambda: compare_report_pdf(pdf_ctx),
                               file_name=f"RBN_compare_{callsign}_{file_date}.pdf", mime="application/pdf",
                               help="The verdict, scoreboard and every chart and table, as a PDF you can keep or share.")
            with st.expander(used["label"]):
                st.dataframe(pd.DataFrame(used["rows"], index=[names["A"], names["B"]]).T, width="stretch")
                for line in used["lines"]:
                    st.markdown(f"- {line}")
            st.markdown(scoreboard_html(names["A"], names["B"], score_rows), unsafe_allow_html=True)

            st.markdown(f"##### Who heard you on each {'side' if sep.get('time_based') else 'frequency'}?")
            st.markdown(reach_bar_html(r["only_a"], r["both"], r["only_b"]), unsafe_allow_html=True)
            st.caption(f"Each {noun} is counted once, however many times it spotted you. A *spot* is one report, so "
                       "spots always outnumber stations.")
            st.markdown(_shared_counts_note(an, noun))

            st.subheader("Timeline")
            st.pyplot(timeline_chart(map_a, map_b, names), width="stretch")
            st.caption("Each faint dot is one spot; the solid dots are the median of each transmission, placed in the "
                       "order they happened (a dotted line marks a long pause). If one colour sits above the other "
                       "throughout, that's a real difference; if they swap places, propagation moved around more than "
                       "the antennas differ.")
            if rounds is not None and len(rounds) >= 3:
                if len(rounds) <= 12:
                    st.caption("Who was stronger in each round, and by how much (dB): "
                               + " \u00b7 ".join("equal" if abs(d) < 0.05 else f"{'A' if d > 0 else 'B'} +{abs(d):.1f}"
                                                 for d in rounds["delta"]))
                else:
                    st.caption(f"{len(rounds)} rounds (one A transmission and the B one after it). A was stronger in "
                               f"{int((rounds['delta'] > 0).sum())}, B in {int((rounds['delta'] < 0).sum())}; the typical "
                               f"round: {_lead_text(names, float(rounds['delta'].median()))}.")

        with tab_where:
            az_points = _azimuth_points(ta_all, tb_all, an)
            if len(az_points):
                st.subheader("Where each antenna reached")
                _azimuth_map_section(az_points, an, names, noun, units, home, callsign, scale)
            if s_["n"]:
                st.subheader("Which side is stronger, by direction")
                width = st.radio("Sector width", [45, 30, 15], horizontal=True, key="adv_width",
                                 format_func=lambda w: f"{w}\u00b0", help="Narrower sectors show more detail but each holds fewer receivers.")
                adv = direction_advantage(an["shared"], width)
                have = adv[adv["n"] > 0]
                options = ["All"] + list(have["label"])
                picked = st.pills("Look at one direction", options, default="All", key="adv_sector",
                                  help="Outlines that direction on the chart and gives its numbers.")
                picked = picked if picked in options else "All"
                chart_col, table_col = st.columns([1, 1])
                chart_col.pyplot(direction_advantage_chart(adv, names, width, callsign, None if picked == "All" else picked),
                                 width="stretch")
                if picked != "All":
                    table_col.markdown(_sector_detail(an["shared"], adv, picked, width, names, noun))
                table_col.dataframe(pd.DataFrame({
                    "Direction": have["label"], f"{noun.capitalize()}s": have["n"].astype(int),
                    "Stronger by": [_gain_text(v) for v in have["delta"]],
                    "Note": ["only " + str(int(n)) + " - treat as a hint" if n < BIN_MIN_SHARED else "" for n in have["n"]]}),
                    width="stretch", hide_index=True, height=min(38 * (len(have) + 1), 420))
                table_col.caption("The longer the bar, the more one side beat the other toward that direction; blue is A and orange "
                                  "is B. A more solid bar rests on more receivers, and a hatched one on too few to trust. Unlike "
                                  "the average-SNR chart below, there is nothing to subtract in your head.")
            if means_a.notna().any() or means_b.notna().any():
                st.subheader("Average SNR by direction")
                min_dir = int(st.slider(f"Only directions with at least this many {noun}s", 1, 20, 1, key="snr_dir_min",
                                        help="A direction with only one or two receivers can stretch the chart. Raise this to leave "
                                             "those directions out of the chart (the table keeps them)."))
                if min_dir > 1:
                    means_a, means_b = sector_means(ta_all, min_dir), sector_means(tb_all, min_dir)
                    pdf_ctx["means_a"], pdf_ctx["means_b"] = means_a, means_b
                chart_col, table_col = st.columns([1, 1])
                chart_col.pyplot(direction_chart(means_a, means_b, names["A"], names["B"]), width="stretch")
                table_col.dataframe(breakdown_table(an["direction"], names, "Direction", noun), width="stretch", hide_index=True,
                                    height=35 * (len(an["direction"]) + 1) + 3)
                table_col.caption("Distance from the centre in the chart is the average SNR toward that direction. In the "
                                  "table, “Stronger by” is measured at the stations that heard both, and a side is "
                                  f"“better” past {DIRECTION_EDGE_DB:g} dB.")
            if len(an["distance"]) and an["distance"][["a", "b"]].to_numpy().sum():
                st.subheader("Distance")
                chart_col, table_col = st.columns([1, 1])
                chart_col.pyplot(distance_chart(an["distance"], names), width="stretch")
                table_col.dataframe(breakdown_table(an["distance"], names, "Distance", noun), width="stretch", hide_index=True,
                                    height=35 * (len(an["distance"]) + 1) + 3)
                table_col.caption("Bars show how much stronger one side was at the stations that heard both; a faded bar "
                                  "means fewer than 3 stations, so don't lean on it. "
                                  "Longer paths usually mean a lower take-off angle. If one antenna wins close in and the "
                                  "other wins far out, that's the signature of high-angle versus low-angle performance.")

        with tab_each:
            if s_["n"]:
                blocks_mode = bool(sep.get("blocks"))
                ctrl_method, ctrl_min = st.columns([2, 3])
                method_label = ctrl_method.radio(
                    f"Average each {noun}'s reports by", ["SNR (dB)", "Linear power"], index=1 if blocks_mode else 0,
                    horizontal=True, key=f"rx_method_{'blk' if blocks_mode else 'pair'}",
                    help="SNR (dB) takes the middle of a station's reports in dB. Linear power turns each report into power, "
                         "averages the power and turns it back into dB. Linear is the proper average of signal power over "
                         "time and suits a long test with many reports per station; the dB middle is steadier when a station "
                         "has only a few reports.")
                min_key = f"rx_min_reports_{'blk' if blocks_mode else 'pair'}"
                st.session_state.setdefault(min_key, DALE_MIN_SPOTS if blocks_mode else 1)
                min_reports = int(ctrl_min.slider(
                    f"Only {noun}s with at least this many reports on each antenna", 1, 60,
                    key=min_key,
                    help="Raising this drops stations with few reports. The far, weak ones go first, but each station left has a "
                         "steadier average, and the answer settling as you raise it is a sign it can be trusted."))
                plain = method_label == "SNR (dB)" and min_reports == 1 and avg_floor is None and not avg_min_km and not weak_pct
                if avg_note:
                    st.caption("Averages here have " + avg_note + ".")
                fa = spots_a if avg_floor is None else spots_a[spots_a["snr"] > avg_floor]
                fb = spots_b if avg_floor is None else spots_b[spots_b["snr"] > avg_floor]
                if plain:
                    every, scatter_src = an["shared_all"], an["shared"]
                else:
                    every = station_gains(spots_a, spots_b, locs, home, "linear" if method_label == "Linear power" else "snr",
                                          min_reports, an["power"]["offset"], avg_floor, avg_min_km, weak_pct)
                    scatter_src = every
                if every.empty:
                    st.info(f"No {noun} has {min_reports} or more reports on both antennas. Lower the number above.")
                else:
                    pdf_ctx["every"], pdf_ctx["scatter_src"] = every, scatter_src
                    pdf_ctx["settings"] = (
                        f"Settings for this section: each {noun}'s reports are averaged by "
                        f"{'linear power' if method_label == 'Linear power' else 'the SNR (dB) median'}; only {noun}s with at least "
                        f"{min_reports} report{'s' if min_reports != 1 else ''} on each antenna are used; "
                        + (avg_note + "; " if avg_note else "no reports or receivers were left out; ")
                        + f"every {noun} counts on its own; distances are in {units}.")
                    pdf_ctx["rx_note"] = (f"Each SNR is the {'average power, in dB,' if method_label == 'Linear power' else 'median'} "
                                          f"of that station's spots"
                                          + (f"; only {noun}s with at least {min_reports} reports on each antenna are included." if min_reports > 1
                                             else "."))
                    chart_col, table_col = st.columns([1, 1])
                    chart_col.pyplot(paired_scatter(scatter_src, names), width="stretch")
                    if plain:
                        table_col.caption(f"{s_['n']} {noun}s heard both. \U0001F535 A was stronger at {s_['wins_a']}, "
                                          f"\U0001F7E0 B at {s_['wins_b']}, tied at {s_['ties']}. Each SNR is the median of that "
                                          f"{noun}'s spots. Click a column heading to sort.")
                    else:
                        wins_a, wins_b = int((every["gap"] > 0).sum()), int((every["gap"] < 0).sum())
                        table_col.caption(f"{len(every)} {noun}s with at least {min_reports} report{'s' if min_reports != 1 else ''} on each antenna. "
                                          f"\U0001F535 A was stronger at {wins_a}, \U0001F7E0 B at {wins_b}, tied at "
                                          f"{len(every) - wins_a - wins_b}. Each SNR is the "
                                          f"{'average power, in dB,' if method_label == 'Linear power' else 'median'} of that "
                                          f"{noun}'s spots. Click a column heading to sort.")
                    table_col.dataframe(head_to_head_table(every, units), width="stretch",
                                        height=min(38 * (len(every) + 1), 420))
                    table_col.download_button("⬇️ This table (CSV)", head_to_head_table(every, units).data.rename(
                                                  columns=lambda c: c.replace("🔵 ", "").replace("🟠 ", "")).to_csv(),
                                              f"RBN_compare_{callsign}_{file_date}_by_{noun}.csv", "text/csv",
                                              help="Every receiver's average SNR on A and B and the relative gain, as shown.")
                if not every.empty:
                    st.subheader("Relative gain by direction and distance")
                    st.caption(f"Relative gain is B's SNR minus A's SNR at the same {noun}. Think of A as the reference antenna and "
                               f"B as the test antenna: above zero, the test antenna gave that {noun} more signal. Because it is "
                               f"a difference at one {noun}, that {noun}'s own noise level cancels, so {noun}s can be compared "
                               "with each other. This holds when both antennas used the same power and receiver and have "
                               "similar patterns.")
                    gain_dir, gain_dist = st.columns(2)
                    gain_dir.pyplot(relative_gain_chart(every, names, units, "direction"), width="stretch")
                    gain_dist.pyplot(relative_gain_chart(every, names, units, "distance"), width="stretch")

                    st.markdown("##### Average relative gain by direction and distance")
                    c_groups, c_short, c_medium = st.columns([2, 1, 1])
                    groups = c_groups.radio("Group directions as", ["Octants (8)", "Quadrants (4)"], horizontal=True, key="gs_groups",
                                            help="Each direction is centred on its compass point, like the direction charts: N covers "
                                                 "337\u00b0 to 22\u00b0 for octants, and 315\u00b0 to 45\u00b0 for quadrants.")
                    kk_gs = KM_PER_MILE if units == "mi" else 1.0
                    short_max = float(c_short.number_input(f"Short ends at ({units})", min_value=50, max_value=20000,
                                                           value=1000 if units == "mi" else 1500, step=100, key=f"gs_short_{units}"))
                    medium_max = float(c_medium.number_input(f"Medium ends at ({units})", min_value=100, max_value=20000,
                                                             value=2500 if units == "mi" else 4000, step=100, key=f"gs_med_{units}"))
                    if medium_max <= short_max:
                        st.warning("Medium has to end beyond where Short ends. Using the two numbers in order.")
                        short_max, medium_max = sorted((short_max, medium_max))
                    dir_sum = gain_summary(every, "octant" if groups.startswith("Octants") else "quadrant")
                    dist_labels = [f"Short: under {short_max:,.0f} {units}", f"Medium: {short_max:,.0f} to {medium_max:,.0f} {units}",
                                   f"Long: over {medium_max:,.0f} {units}"]
                    dist_sum = gain_summary(every, "distance", [short_max * kk_gs, medium_max * kk_gs], dist_labels)
                    gs_dir = _gain_summary_view(dir_sum, "Direction", noun)
                    gs_dist = _gain_summary_view(dist_sum, "Distance", noun)
                    pdf_ctx["gain_summary"] = {"direction": gs_dir, "distance": gs_dist}
                    t_dir, t_dist = st.columns(2)
                    t_dir.dataframe(gs_dir, width="stretch", hide_index=True, height=35 * (len(gs_dir) + 1) + 3)
                    t_dist.dataframe(gs_dist, width="stretch", hide_index=True, height=35 * (len(gs_dist) + 1) + 3)
                    st.caption(f"The average of B \u2212 A at the {noun}s in each group, with each {noun} counting once and "
                               f"a 95% range. Above zero, B was stronger there. A group with fewer than {BIN_MIN_SHARED} {noun}s is a "
                               f"hint, not a result. It uses the same averaging ({method_label}) and minimum reports as the charts above.")

                    st.subheader(f"One {noun} over time")
                    reports_a, reports_b = fa.groupby("spotter").size(), fb.groupby("spotter").size()
                    kk = KM_PER_MILE if units == "mi" else 1
                    sort_by = st.radio("Sort the list by", ["A\u2013Z", "Distance", "Direction", "Most reports"], horizontal=True,
                                       key="rx_sort", help="You can also click the box and type part of a callsign to find it.")
                    order_key = {"A\u2013Z": lambda r: r,
                                 "Distance": lambda r: every.at[r, "km_a"],
                                 "Direction": lambda r: every.at[r, "bearing_a"],
                                 "Most reports": lambda r: (-min(reports_a.get(r, 0), reports_b.get(r, 0)), every.at[r, "km_a"])}[sort_by]
                    ranked = sorted(every.index, key=order_key)
                    pick = st.selectbox(
                        f"Pick a {noun}", ranked,
                        format_func=lambda r: (f"{r} \u00b7 {every.at[r, 'km_a'] / kk:,.0f} {units} "
                                               f"{COMPASS_16[int(((every.at[r, 'bearing_a'] + 11.25) % 360) // 22.5)]} \u00b7 "
                                               f"{reports_a.get(r, 0)} on A, {reports_b.get(r, 0)} on B"))
                    offset = an["power"]["offset"]
                    st.pyplot(receiver_time_chart(fa, fb, pick, names, offset, linear=method_label == "Linear power"),
                              width="stretch")
                    one_a = fa[fa["spotter"] == pick]["snr"].to_numpy(float)
                    one_b = fb[fb["spotter"] == pick]["snr"].to_numpy(float) + offset
                    m_a, m_b, m_gain = st.columns(3)
                    for col, label, values in ((m_a, names["A"], one_a), (m_b, names["B"], one_b)):
                        col.metric(label, f"{_linear_db(values):.1f} dB")
                        col.caption(f"Linear average \u00b7 {len(values)} reports \u00b7 dB average {values.mean():.1f} \u00b7 middle {np.median(values):g} "
                                    f"\u00b7 spread (std) {values.std(ddof=1) if len(values) > 1 else float('nan'):.1f} dB")
                    diff, lo, hi = _gain_interval(one_a, one_b)
                    m_gain.metric("Relative gain B \u2212 A", f"{_linear_db(one_b) - _linear_db(one_a):+.1f} dB")
                    m_gain.caption(f"Linear power. dB averages: {diff:+.1f} dB"
                                   + (f", 95% range {lo:+.1f} to {hi:+.1f} dB" if not np.isnan(lo) else ", too few reports for a range"))
                    st.caption(f"Each dot is one report from this {noun}; the line is each antenna's average "
                               f"({'linear power' if method_label == 'Linear power' else 'middle of the dB reports'}). "
                               "Wide scatter means the signal was fading or the band was changing, which is why a few reports "
                               "can give a misleading average. In a long-block test the shading shows when each antenna was "
                               "connected.")
                    if sep.get("blocks") and sep.get("t0") is not None:
                        st.subheader("Relative gain over time")
                        block_min = sep["block_min"]
                        t_start = sep["t0"]
                        t_end = sep.get("t_end") or t_start + pd.Timedelta(minutes=block_min * sep["n_blocks_set"])
                        multiples = [m for m in (2, 4, 6, 8) if m * block_min <= (t_end - t_start).total_seconds() / 60]
                        c_len, c_min = st.columns(2)
                        mult = c_len.selectbox("Window length", multiples or [2], index=min(1, max(len(multiples) - 1, 0)),
                                               format_func=lambda m: (f"{m * block_min // 60} hour{'s' if m * block_min != 60 else ''}"
                                                                      if m * block_min >= 60 and m * block_min % 60 == 0
                                                                      else f"{m * block_min:g} minutes"),
                                               key=f"rx_win_len_{block_min}",
                                               help="A window should hold whole A and B blocks so both antennas get equal time. "
                                                    "Two hours (four 30-minute blocks) balances either block order.")
                        win_min = c_min.slider(f"Minimum reports per {noun} in a window", 1, 20, 3, key="rx_win_min",
                                               help=f"Each window is averaged on its own, so it holds fewer reports per {noun} than "
                                                    "the whole test. A low number keeps more stations; a higher one is steadier.")
                        step = pd.Timedelta(minutes=mult * block_min)
                        windows, cursor = [], t_start
                        while cursor < t_end:
                            windows.append((cursor, min(cursor + step, t_end)))
                            cursor += step
                        over_time = gain_by_window(spots_a, spots_b, locs, home, windows,
                                                   "linear" if method_label == "Linear power" else "snr", win_min, offset,
                                                   receiver=pick, floor=avg_floor, min_km=avg_min_km, weak_pct=weak_pct)
                        if over_time["n"].sum() == 0:
                            st.caption("No window has enough reports at that minimum. Lower it.")
                        else:
                            st.pyplot(gain_over_time_chart(over_time, names, pick), width="stretch")
                            st.dataframe(pd.DataFrame({
                                "Window (UTC)": [f"{s:%H:%M}\u2013{e:%H:%M}" for s, e in zip(over_time["start"], over_time["end"])],
                                f"{noun.capitalize()}s": over_time["n"],
                                "Average gain B \u2212 A (dB)": over_time["mean"].round(1),
                                "95% range": [f"{lo:+.1f} to {hi:+.1f}" if not np.isnan(lo) else "" for lo, hi in
                                              zip(over_time["lo"], over_time["hi"])],
                                f"{pick} (dB)": over_time["one"].round(1),
                                f"{pick} reports (A / B)": [f"{a} / {b}" for a, b in over_time["one_n"]]}),
                                width="stretch", hide_index=True)
                            st.caption("The bars are the average relative gain over every station that heard both antennas in that "
                                       "window, with the 95% range across stations; the line is the picked "
                                       f"{noun}. If the bars and line move up or down during the test, a single average over the "
                                       "whole test hides it. A band opening, for example Europe late in the day, shows up here.")
            else:
                st.info(f"No {noun} heard both sides, so there is nothing to compare receiver by receiver.")

        with tab_method:
            _linear_average_view(map_a, map_b, an, names, noun, locs, home, kind, callsign, paired=(spots_a, spots_b),
                                 floor=avg_floor, min_km=avg_min_km, weak_pct=weak_pct,
                                 default_min=DALE_MIN_SPOTS if sep.get("blocks") else 1)

        with tab_maps:
            st.caption("Both maps use the same view, the same SNR scale and the same dot sizes.")
            both = pd.concat([map_a, map_b])
            col_a, col_b = st.columns(2)
            for col, side, group in ((col_a, "A", map_a), (col_b, "B", map_b)):
                html = build_map(group, locs, home, label, callsign, show_all, tiles, units,
                                 compute_stats(group, locs, home)["farthest"],
                                 title=f"{callsign} · {names[side]}", fit_spots=both, scale=scale, noun=noun
                                 ).get_root().render()
                icon = "\U0001F535" if side == "A" else "\U0001F7E0"
                slug = re.sub(r"[^A-Za-z0-9]+", "_", names[side]).strip("_")
                with col:
                    st.markdown(f"#### {icon} {names[side]}")
                    st.iframe(html, height=620)
                    st.download_button(f"⬇️ Download map {side}", html,
                                       f"RBN_map_{callsign}_{file_date}_{slug}.html", "text/html", key=f"dl_{side}")


    # wspr.live fills a slot in over several minutes: about 1 receiver at 2 minutes old, half by 3-4, complete by 6-8 (measured).
    # A transmission younger than this is left out of a comparison, since a half-filled slot makes its antenna look worse.
GHOST_MAX, GHOST_MIN_TYPICAL = 2, 16  # see _without_ghost_cycles


SIMULTANEOUS_MINUTES = 10  # A and B sent within this many minutes of each other count as the same conditions


DATA_LAG_MINUTES = 5  # measured: reports for a slot are complete ~3 min after it ends, ~5 min after it starts
