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
        self.assertEqual([b.label for b in at.sidebar.button], ["Load spots", "Try an example"])

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

    @needs_network
    def test_the_example_shows_the_map_and_a_shareable_link(self):
        at = page("spots_page").run()
        click(at, "Try an example")
        self.assertEqual(errors(at), [])
        self.assertTrue(any("This is an example" in i.value for i in at.info))
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
        self.assertEqual([b.label for b in at.sidebar.button], ["Load spots", "Try an example", "Try a long-test example"])
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("With WSPR, the best way", text)
        self.assertIn("With RBN, for a quick CW check", text)

    def test_it_starts_on_wspr(self):
        at = page("compare_page").run()
        self.assertEqual(at.session_state["src_in"], "WSPR")

    def test_there_is_no_time_window_for_wspr(self):
        at = page("compare_page").run()
        self.assertEqual([s.label for s in at.sidebar.slider if s.label == "UTC time window"], [])

    @needs_network
    def test_the_quick_example_shows_a_verdict(self):
        at = page("compare_page").run()
        click(at, "Try an example")
        self.assertEqual(errors(at), [])
        text = " ".join(m.value for m in at.markdown) + " " + " ".join(i.value for i in at.info)
        self.assertIn("This is an example", text)
        self.assertIn("better", text)
        self.assertTrue(any("call=K5OHY" in c.value for c in at.sidebar.code))

    @needs_network
    def test_the_long_test_example_uses_dales_schedule_and_40_reports(self):
        at = page("compare_page").run()
        click(at, "Try a long-test example")
        self.assertEqual(errors(at), [])
        self.assertEqual(at.session_state["callsign_in"], "N3HXZ")
        self.assertEqual(at.session_state["test_mode"], "In long blocks")
        self.assertEqual(at.session_state["rx_min_reports_blk"], 40)
        self.assertIn("Dale Ostergaard", " ".join(i.value for i in at.info))
        self.assertTrue(any("receivers with at least 40 reports" in c.value for c in at.caption))
        self.assertTrue(any("getting out better" in m.value for m in at.markdown))

    @needs_network
    def test_comparison_filters_run(self):
        at = page("compare_page").run()
        click(at, "Try a long-test example")
        weak = [n for n in at.number_input if n.key == "avg_weak"]
        self.assertTrue(weak)
        weak[0].set_value(25)
        at.run()
        self.assertEqual(errors(at), [])

    @needs_network
    def test_a_loaded_example_carries_over_to_my_spots(self):
        """What you load on one page is there on the other, so you do not have to enter it twice."""
        at = page("compare_page").run()
        click(at, "Try an example")
        self.assertIsNotNone(at.session_state["raw"])
        self.assertEqual(at.session_state["callsign"], "K5OHY")


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
