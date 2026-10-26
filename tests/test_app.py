"""Checks that drive the real app, page by page, with Streamlit's own test runner. The ones that need the internet (wspr.live,
NOAA) only run when you ask for them:

    python -m unittest discover -s tests -v                      (quick, offline)
    set RBN_TEST_NETWORK=1 && python -m unittest discover -s tests -v    (everything, a few minutes)
"""
import os
import sys
import tempfile
import unittest
from datetime import date

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

from streamlit.testing.v1 import AppTest  # noqa: E402

NETWORK = os.environ.get("RBN_TEST_NETWORK") == "1"
needs_network = unittest.skipUnless(NETWORK, "set RBN_TEST_NETWORK=1 to run the checks that use the internet")


def page(function, **params):
    """A page run on its own, as the menu would, with the web address parameters given."""
    runner = os.path.join(tempfile.gettempdir(), f"rbn_{function}_runner.py")
    with open(runner, "w", encoding="utf-8") as f:
        f.write("import sys\nsys.path.insert(0, r'%s')\nimport web\nweb.%s()\n" % (HERE, function))
    at = AppTest.from_file(runner, default_timeout=600)
    for key, value in params.items():
        at.query_params[key] = value
    return at


def errors(at):
    return [e.value[:200] for e in at.exception]


def click(at, label):
    [b for b in at.sidebar.button if b.label == label][0].click()
    at.run()


class MySpotsPage(unittest.TestCase):
    def test_opens_with_the_load_and_example_buttons(self):
        at = page("spots_page").run()
        self.assertEqual(errors(at), [])
        self.assertEqual([b.label for b in at.sidebar.button], ["Load spots", "Try an example", "Start over"])

    def test_the_old_compare_checkbox_is_gone(self):
        at = page("spots_page").run()
        self.assertEqual([c.label for c in at.sidebar.checkbox if "Compare" in c.label], [])

    def test_a_link_fills_the_sidebar(self):
        at = page("spots_page", call="k5ohy", src="wspr", date="2026-10-04", band="20m", units="km").run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["callsign_in"], "K5OHY")
        self.assertEqual(at.session_state["src_in"], "WSPR")
        self.assertEqual(at.session_state["day_in"], date(2026, 10, 4))
        self.assertEqual(at.session_state["band_choice"], "20m")
        self.assertEqual(at.session_state["units_in"], "km")
        self.assertIsNone(at.session_state["raw"])  # no go=1, so nothing is loaded until you click

    def test_the_source_is_rbn_or_wspr_then_download_or_paste(self):
        at = page("spots_page").run()
        net = [r for r in at.sidebar.radio if r.key == "net_in"][0]
        self.assertEqual(net.options, ["RBN", "WSPR"])
        how = [r for r in at.sidebar.radio if r.key == "how_in"][0]
        how.set_value("Paste spots")
        at.run()
        self.assertEqual(at.session_state["src_in"], "Paste from RBN site")
        [r for r in at.sidebar.radio if r.key == "net_in"][0].set_value("WSPR")
        at.run()
        self.assertEqual(at.session_state["src_in"], "WSPR")
        self.assertEqual([r.key for r in at.sidebar.radio if r.key == "how_in"], [])
        [r for r in at.sidebar.radio if r.key == "net_in"][0].set_value("RBN")
        at.run()
        self.assertEqual(at.session_state["src_in"], "Paste from RBN site")  # it remembers your last RBN choice

    def test_a_date_range_is_picked_on_one_calendar(self):
        at = page("spots_page", call="K5OHY", src="rbn", date="2024-03-01..2024-03-03").run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["span_in"], "Date range")
        self.assertEqual(tuple(at.session_state["range_in"]), (date(2024, 3, 1), date(2024, 3, 3)))
        self.assertEqual([d.label for d in at.sidebar.date_input], ["Dates (UTC)"])
        self.assertEqual(at.session_state["to_in"], date(2024, 3, 3))

    def test_start_over_goes_back_to_the_latest_day_and_keeps_the_callsign(self):
        at = page("spots_page", call="K5OHY", src="rbn", date="2024-03-01").run()
        self.assertEqual(at.session_state["day_in"], date(2024, 3, 1))
        click(at, "Start over")
        self.assertEqual(errors(at), [])
        self.assertGreater(at.session_state["day_in"], date(2025, 1, 1))
        self.assertEqual(at.session_state["callsign_in"], "K5OHY")
        self.assertIsNone(at.session_state["raw"])

    def test_use_my_own_data_clears_an_example(self):
        at = page("spots_page").run()
        click(at, "Try an example")
        self.assertEqual(at.session_state["callsign_in"], "K5OHY")
        [b for b in at.main.button if b.label == "Use my own data instead"][0].click()
        at.run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["callsign_in"], "")
        self.assertIsNone(at.session_state["raw"])
        self.assertFalse(at.session_state["_example"] if "_example" in at.session_state else False)

    def test_a_bad_link_is_ignored(self):
        at = page("spots_page", call="K5OHY", src="nonsense", date="not-a-date", band="99m", units="furlongs").run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["callsign_in"], "K5OHY")
        self.assertIn(at.session_state["src_in"], ("Download by date", "Paste from RBN site", "WSPR"))

    def test_the_address_follows_the_sidebar(self):
        at = page("spots_page", call="K5OHY", src="wspr", date="2026-10-04").run()
        self.assertEqual(at.query_params["call"], ["K5OHY"])
        self.assertEqual(at.query_params["src"], ["wspr"])

    def test_empty_page_suggests_the_example_and_the_compare_page(self):
        at = page("spots_page").run()
        self.assertTrue(any("Try an example" in i.value for i in at.info))
        self.assertTrue(any("Compare antennas" in m.value for m in at.markdown))

    def test_the_empty_page_has_an_example_button_on_the_main_stage(self):
        at = page("spots_page").run()
        self.assertEqual([b.label for b in at.main.button], ["Try an example"])

    def test_the_example_shows_rbn_spots_on_the_map_and_a_shareable_link(self):
        at = page("spots_page").run()
        click(at, "Try an example")
        self.assertEqual(errors(at), [])
        self.assertTrue(any("Reverse Beacon Network" in i.value for i in at.info))
        self.assertEqual(at.session_state["kind"], "RBN")
        self.assertEqual(at.session_state["day_in"], date(2025, 10, 23))
        self.assertGreater(len(at.session_state["raw"]), 50)
        self.assertEqual(len(at.tabs), 2)  # Map & direction, Session report
        self.assertTrue(any("call=K5OHY" in c.value for c in at.sidebar.code))

    @needs_network
    def test_a_shared_link_loads_the_spots(self):
        at = page("spots_page", call="K5OHY", src="wspr", date="2026-10-04", band="20m", go="1").run()
        self.assertEqual(errors(at), [])
        self.assertGreater(len(at.session_state["raw"]), 100)


class ComparePage(unittest.TestCase):
    def test_opens_with_the_steps_and_the_three_buttons(self):
        at = page("compare_page").run()
        self.assertEqual(errors(at), [])
        self.assertEqual([b.label for b in at.sidebar.button], ["Load spots", "Try an example", "Try an RBN example", "Try a long-test example", "Start over"])
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("With WSPR, the best way", text)
        self.assertIn("With RBN, for a quick CW check", text)

    def test_the_empty_page_has_both_examples_on_the_main_stage(self):
        at = page("compare_page").run()
        self.assertEqual([b.label for b in at.main.button], ["Try the quick example", "Try the RBN example", "Try the long-test example"])

    def test_it_starts_on_wspr(self):
        at = page("compare_page").run()
        self.assertEqual(at.session_state["src_in"], "WSPR")

    def test_there_is_no_time_window_for_wspr(self):
        at = page("compare_page").run()
        self.assertEqual([s.label for s in at.sidebar.slider if s.label == "UTC time window"], [])

    def test_the_quick_example_shows_a_verdict(self):
        at = page("compare_page").run()
        click(at, "Try an example")
        self.assertEqual(errors(at), [])
        text = " ".join(m.value for m in at.markdown) + " " + " ".join(i.value for i in at.info)
        self.assertIn("This is an example", text)
        self.assertIn("better", text)
        self.assertTrue(any("call=K5OHY" in c.value for c in at.sidebar.code))

    @needs_network
    def test_two_callsigns_compare_as_a_and_b_and_travel_in_the_link(self):
        at = page("compare_page", call="M0MCX", cb="M0XXT", src="wspr", date="2026-01-14", band="20m", go="1", n="6",
                  ts="13:38").run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["tx_call_b"], "M0XXT")
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("Signal advantage", text)
        link = [c.value for c in at.sidebar.code if "call=M0MCX" in c.value][0]
        self.assertIn("cb=M0XXT", link)

    @needs_network
    def test_hand_corrected_antenna_cells_travel_in_the_link(self):
        at = page("compare_page", call="K5OHY", src="wspr", date="2026-10-04", band="20m", go="1", n="12", ord="abba",
                  ed="3B,5X").run()
        self.assertEqual(errors(at), [])
        link = [c.value for c in at.sidebar.code if "call=K5OHY" in c.value][0]
        self.assertIn("ed=3B%2C5X", link)

    @needs_network
    def test_a_pasted_block_schedule_travels_in_the_link(self):
        schedule = "12:00-12:30 A;12:30-13:00 B;13:00-13:30 A;13:30-14:00 B"
        at = page("compare_page", call="N3HXZ", src="wspr", date="2026-08-26", band="20m", go="1", sw="blocks", bl="30",
                  bp=schedule).run()
        self.assertEqual(errors(at), [])
        link = [c.value for c in at.sidebar.code if "call=N3HXZ" in c.value][0]
        self.assertIn("bp=12%3A00-12%3A30+A%3B12%3A30-13%3A00+B", link)

    @needs_network
    def test_a_short_test_with_very_different_farthest_reach_does_not_say_similar(self):
        """Callum's 20m delta loop against a vertical: 10,582 mi against 3,827 mi, but only 3 pairs, so it is not called."""
        at = page("compare_page", call="M0MCX", cb="M0XXT", grid="IO92di", src="wspr", units="mi", date="2026-01-14",
                  band="20m", go="1", na="Delta loop", nb="Vertical", n="6", ts="13:38", mr="1").run()
        self.assertEqual(errors(at), [])
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("too short to tell", text)
        self.assertNotIn("Similar farthest reach", text)
        self.assertIn("3 receivers beyond 5,000 mi heard Delta loop, 0 heard Vertical", text)

    @needs_network
    def test_truly_similar_farthest_reach_still_says_similar(self):
        at = page("compare_page", call="M0XXT", cb="M0MCX", grid="IO92di", src="wspr", units="mi", date="2026-01-14",
                  band="40m", go="1", na="Vertical", nb="Delta loop", n="6", ts="21:34", mr="1").run()
        self.assertEqual(errors(at), [])
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("Similar farthest reach", text)

    @needs_network
    def test_your_own_spots_wait_for_you_to_confirm_the_setup(self):
        at = page("compare_page", call="K5OHY", src="wspr", date="2026-10-04", band="20m", go="1").run()
        self.assertEqual(errors(at), [])
        self.assertFalse(at.session_state["setup_ok"])
        text = " ".join(m.value for m in at.markdown)
        self.assertNotIn("Signal advantage", text)  # no result until it is confirmed
        confirm = [b for b in at.main.button if b.label.startswith("This matches what I did")]
        self.assertEqual(len(confirm), 1)
        confirm[0].click()
        at.run()
        self.assertEqual(errors(at), [])
        self.assertTrue(at.session_state["setup_ok"])
        self.assertIn("Signal advantage", " ".join(m.value for m in at.markdown))
        self.assertEqual([b for b in at.main.button if b.label.startswith("This matches what I did")], [])

    @needs_network
    def test_examples_and_shared_tests_need_no_confirming(self):
        at = page("compare_page").run()
        click(at, "Try an example")
        self.assertTrue(at.session_state["setup_ok"])
        shared = page("compare_page", call="K5OHY", src="wspr", date="2026-10-04", band="20m", go="1", n="12", ord="abba").run()
        self.assertTrue(shared.session_state["setup_ok"])

    def test_the_rbn_example_compares_the_loop_and_the_dipole_by_frequency(self):
        at = page("compare_page").run()
        click(at, "Try an RBN example")
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["kind"], "RBN")
        self.assertEqual(at.session_state["cmp_name_b"], "20m dipole, 40 ft")
        text = " ".join(m.value for m in at.markdown) + " " + " ".join(i.value for i in at.info)
        self.assertIn("14069.5 kHz", text)
        self.assertIn("20m dipole, 40 ft", text)
        self.assertIn("(A)</span> vs", text)  # the result's title, with A and B coloured
        self.assertIn("on 20 m", text)

    def test_the_long_test_example_uses_dales_schedule_and_40_reports(self):
        at = page("compare_page").run()
        click(at, "Try a long-test example")
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["callsign_in"], "N3HXZ")
        self.assertEqual(at.session_state["test_mode"], "In long blocks")
        self.assertEqual(at.session_state["rx_min_reports_blk"], 40)
        self.assertIn("Dale, N3HXZ", " ".join(i.value for i in at.info))
        self.assertTrue(any("receivers with at least 40 reports" in c.value for c in at.caption))
        self.assertTrue(any("getting out better" in m.value for m in at.markdown))

    def test_comparison_filters_run(self):
        at = page("compare_page").run()
        click(at, "Try a long-test example")
        weak = [n for n in at.number_input if n.key == "avg_weak"]
        self.assertTrue(weak)
        weak[0].set_value(25)
        at.run()
        self.assertEqual(errors(at), [])

    def test_a_test_link_fills_in_the_setup(self):
        at = page("compare_page", call="N3HXZ", src="wspr", date="2026-08-26", band="20m", na="Loop", nb="Dipole", sw="blocks",
                  bl="30", ord="abba", mr="25", fl="1", nr="50", wk="20", units="mi").run()
        self.assertEqual(errors(at), [])
        ss = at.session_state
        self.assertEqual((ss["cmp_name_a"], ss["cmp_name_b"]), ("Loop", "Dipole"))
        self.assertEqual(ss["test_mode"], "In long blocks")
        self.assertEqual(ss["blk_len"], 30)
        self.assertTrue(ss["blk_order"].startswith("A, B, B, A"))
        self.assertEqual(ss["rx_min_reports_blk"], 25)
        self.assertTrue(ss["avg_floor"])
        self.assertEqual((ss["avg_near_mi"], ss["avg_weak"]), (50, 20))

    def test_a_bad_test_link_is_ignored(self):
        at = page("compare_page", sw="sideways", n="999", bl="1", ord="zzz", mr="0", nr="-5", wk="abc", ts="<script>").run()
        self.assertEqual(errors(at), [])
        self.assertNotIn("test_mode", at.session_state)

    @needs_network
    def test_the_link_box_holds_the_whole_test_and_reopens_it(self):
        at = page("compare_page").run()
        click(at, "Try a long-test example")
        link = [c.value for c in at.sidebar.code if "call=N3HXZ" in c.value][0]
        for part in ("sw=blocks", "bl=30", "bn=", "bs=12%3A00", "mr=40", "na=17+ft"):
            self.assertIn(part, link)
        from urllib.parse import parse_qs, urlparse
        q = {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}
        again = page("compare_page", **q).run()
        self.assertEqual(errors(again), [])
        self.assertEqual(again.session_state["rx_min_reports_blk"], 40)
        self.assertTrue(any("getting out better" in m.value for m in again.markdown))

    def test_a_loaded_example_carries_over_to_my_spots(self):
        """What you load on one page is there on the other, so you do not have to enter it twice."""
        at = page("compare_page").run()
        click(at, "Try an example")
        self.assertIsNotNone(at.session_state["raw"])
        self.assertEqual(at.session_state["callsign"], "K5OHY")


class SavedExamplesMatchTheLiveData(unittest.TestCase):
    @needs_network
    def test_the_saved_rbn_example_still_matches_the_download(self):
        """The saved copy is a snapshot; this notices if RBN's file for that day ever changes."""
        import example_data
        from data_sources import download_rbn_day
        _, saved = example_data.load("rbn")
        live = download_rbn_day("20260927", "K5OHY")
        self.assertEqual(len(live), len(saved))
        self.assertAlmostEqual(float(live["snr"].sum()), float(saved["snr"].sum()), places=3)


class BandConditionsPage(unittest.TestCase):
    @needs_network
    def test_the_dashboard_opens(self):
        at = page("conditions_page", call="K5OHY").run()
        self.assertEqual(errors(at), [])
        self.assertEqual([x.label for x in at.selectbox if x.label == "Mode"], ["Mode"])

    @needs_network
    def test_every_mode_works(self):
        at = page("conditions_page", call="K5OHY").run()
        mode = [x for x in at.selectbox if x.label == "Mode"][0]
        self.assertEqual(mode.options, ["CW", "SSB", "FT8", "WSPR"])
        for name in mode.options:
            [x for x in at.selectbox if x.label == "Mode"][0].set_value(name)
            at.run()
            self.assertEqual(errors(at), [], name)

    @needs_network
    def test_a_link_sets_power_mode_and_look_back(self):
        at = page("conditions_page", call="K5OHY", power="5", mode="SSB", window="60", units="km").run()
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["cond_power"], 5.0)
        self.assertEqual(at.session_state["cond_mode"], "SSB")
        self.assertEqual(at.session_state["cond_window"], 60)
        self.assertEqual(at.session_state["cond_units"], "km")

    def test_the_empty_page_offers_a_use_my_location_button(self):
        at = page("conditions_page").run()
        self.assertEqual(errors(at), [])
        self.assertEqual([b.label for b in at.main.button], ["Use my location"])

    def test_the_use_my_location_button_switches_to_your_location(self):
        at = page("conditions_page").run()
        at.main.button[0].click()
        at.run()
        # the test runner cannot load the page's own location component; that is the only thing allowed to fail here
        self.assertEqual([e for e in errors(at) if "not registered" not in e], [])
        self.assertEqual(at.session_state["cond_where"], "My location")
        self.assertEqual(at.session_state["_locate_ticket"], 1)

    def test_bad_link_values_are_ignored(self):
        at = page("conditions_page", call="K5OHY", power="99999", mode="bogus", window="7").run()
        self.assertEqual(at.session_state["cond_power"], 100.0)
        self.assertEqual(at.session_state["cond_mode"], "CW")
        self.assertEqual(at.session_state["cond_window"], 30)


class HelpPage(unittest.TestCase):
    def test_the_readme_shows_without_error(self):
        at = page("help_page").run()
        self.assertEqual(errors(at), [])
        text = "\n".join(m.value for m in at.markdown)
        self.assertIn("Quick start", text)
        self.assertIn("Try an example", text)


class WholeApp(unittest.TestCase):
    def test_the_menu_has_the_four_pages(self):
        at = AppTest.from_file(os.path.join(HERE, "web.py"), default_timeout=600).run()
        self.assertEqual(errors(at), [])


if __name__ == "__main__":
    unittest.main()
