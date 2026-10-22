"""Checks of the calculations and the HTML helpers, with made-up data and no network. Run from the project folder with:

    python -m unittest discover -s tests -v
"""
import math
import os
import sys
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import compare_stats as cs  # noqa: E402
import conditions as cond  # noqa: E402
import dashboard as dash  # noqa: E402
import ionosphere as iono  # noqa: E402
import space_weather as sw  # noqa: E402
import world_map as wm  # noqa: E402

HOME = (31.0, -102.0)


def spots(by_call):
    """A spot table from {call: [snr, ...]}."""
    return pd.DataFrame([(c, s) for c, snrs in by_call.items() for s in snrs], columns=["spotter", "snr"])


def places(*calls):
    """Receivers a known distance from HOME: the n-th is n * 5 degrees of longitude east of it (a few hundred miles apart)."""
    return {c: (31.0, -102.0 + 5 * (n + 1)) for n, c in enumerate(calls)}


class RelativeGain(unittest.TestCase):
    def test_sign_b_minus_a(self):
        a, b = spots({"AAA": [-20, -20, -20]}), spots({"AAA": [-15, -15, -15]})
        t = cs.station_gains(a, b, places("AAA"), HOME)
        self.assertEqual(float(-t.loc["AAA", "gap"]), 5.0)  # B is 5 dB stronger

    def test_linear_average_example_from_the_readme(self):
        a, b = spots({"AAA": [0, 0, 0, 20]}), spots({"AAA": [5, 5, 5, 5]})
        t = cs.station_gains(a, b, places("AAA"), HOME, method="linear")
        self.assertAlmostEqual(float(t.loc["AAA", "snr_a"]), 14.1, places=1)
        self.assertAlmostEqual(float(t.loc["AAA", "snr_b"]), 5.0, places=1)

    def test_minimum_reports(self):
        a, b = spots({"AAA": [-10] * 5, "BBB": [-10] * 2}), spots({"AAA": [-8] * 5, "BBB": [-8] * 2})
        t = cs.station_gains(a, b, places("AAA", "BBB"), HOME, min_spots=3)
        self.assertEqual(list(t.index), ["AAA"])

    def test_floor_drops_reports_not_stations(self):
        a, b = spots({"AAA": [-30, -20, -20]}), spots({"AAA": [-15, -15, -15]})
        t = cs.station_gains(a, b, places("AAA"), HOME, floor=cs.WSPR_FLOOR_DB)
        self.assertEqual(float(t.loc["AAA", "snr_a"]), -20.0)  # the -30 is gone, the station stays

    def test_leave_out_close_receivers(self):
        locs = {"NEAR": (31.0, -101.5), "FAR": (31.0, -90.0)}
        a, b = spots({"NEAR": [-10] * 3, "FAR": [-10] * 3}), spots({"NEAR": [-8] * 3, "FAR": [-8] * 3})
        t = cs.station_gains(a, b, locs, HOME, min_km=200)
        self.assertEqual(list(t.index), ["FAR"])

    def test_drop_a_station_that_is_mostly_at_the_floor(self):
        a = spots({"WEAK": [-30, -30, -30, -20], "OK": [-20] * 4})
        b = spots({"WEAK": [-28, -28, -28, -20], "OK": [-18] * 4})
        locs = places("WEAK", "OK")
        self.assertEqual(set(cs.station_gains(a, b, locs, HOME, weak_pct=50).index), {"OK"})
        self.assertEqual(set(cs.station_gains(a, b, locs, HOME, weak_pct=90).index), {"WEAK", "OK"})
        self.assertEqual(set(cs.station_gains(a, b, locs, HOME).index), {"WEAK", "OK"})

    def test_every_station_counts_on_its_own(self):
        locs = {"K9TRV": (40.0, -90.0), "K9TRV-2": (40.0, -90.0)}  # two stations at one place
        a, b = spots({"K9TRV": [-20] * 3, "K9TRV-2": [-20] * 3}), spots({"K9TRV": [-15] * 3, "K9TRV-2": [-10] * 3})
        pm = cs.power_method(a, b, locs, HOME)
        self.assertEqual(pm["n"], 2)


class Intervals(unittest.TestCase):
    def test_student_t_values(self):
        self.assertAlmostEqual(cs.t_crit(10), 2.228, places=2)
        self.assertAlmostEqual(cs.t_crit(2), 4.303, places=2)
        self.assertAlmostEqual(cs.t_crit(1000), 1.962, places=2)

    def test_mean_interval(self):
        mean, lo, hi = cs.mean_interval([1, 2, 3, 4, 5])
        half = cs.t_crit(4) * np.std([1, 2, 3, 4, 5], ddof=1) / math.sqrt(5)
        self.assertAlmostEqual(mean, 3.0)
        self.assertAlmostEqual(hi - mean, half, places=6)
        self.assertAlmostEqual(mean - lo, half, places=6)

    def test_one_number_has_no_range(self):
        mean, lo, hi = cs.mean_interval([4.0])
        self.assertEqual(mean, 4.0)
        self.assertTrue(math.isnan(lo) and math.isnan(hi))


class BandConditions(unittest.TestCase):
    def test_chance_of_being_heard(self):
        self.assertAlmostEqual(float(cond.chance(0)), 0.5)
        self.assertGreater(float(cond.chance(24)), 0.99)
        self.assertLess(float(cond.chance(-24)), 0.01)

    def test_the_mode_list(self):
        self.assertEqual(list(cond.MODES), ["CW", "SSB", "FT8", "WSPR"])

    def test_thin_regions_are_hints_not_dx(self):
        bands = ["20m", "15m"]
        regions = ["Europe", "Oceania"]
        frame = lambda v: pd.DataFrame(v, index=bands, columns=regions)  # noqa: E731
        result = {"regions": frame([[10.0, 10.0], [10.0, 10.0]]), "regions_n": frame([[1, 5], [2, 0]]),
                  "chance": frame([[0.99, 0.9], [0.95, np.nan]])}
        cells, solid, good, hints = cond._dx_cells(result)
        self.assertEqual([(b, r) for b, r, _ in good], [("20m", "Oceania")])  # only the cell with enough receivers
        self.assertEqual({(b, r) for b, r, _ in hints}, {("20m", "Europe"), ("15m", "Europe")})


class DashboardPieces(unittest.TestCase):
    def test_open_and_rising_reads_open_with_an_arrow(self):
        self.assertEqual(dash.state("Open", "opening"), ("Open ↗", "tg"))
        self.assertEqual(dash.state("Open", "fading"), ("Open ↘", "tg"))
        self.assertEqual(dash.state("Marginal", "steady"), ("Marginal", "ta"))
        self.assertEqual(dash.state("Quiet", "unclear"), ("Quiet", "tm"))

    def test_thin_cells_show_only_the_count(self):
        chance = pd.DataFrame({"US East": [0.99, 0.8]}, index=["20m", "15m"])
        counts = pd.DataFrame({"US East": [1, 12]}, index=["20m", "15m"])
        html = dash.grid_html(chance, counts)
        self.assertIn("1 RX", html)
        self.assertNotIn("99%", html)
        self.assertIn("80%", html)

    def test_detail_table_hides_figures_for_thin_cells(self):
        margin = pd.DataFrame({"Europe": [20.0, 9.0]}, index=["20m", "15m"])
        need = pd.DataFrame({"Europe": [1.0, 25.0]}, index=["20m", "15m"])
        counts = pd.DataFrame({"Europe": [2, 9]}, index=["20m", "15m"])
        html = dash.detail_grid_html(margin, need, counts, 3)
        self.assertIn("2 RX", html)
        self.assertNotIn("+20 dB", html)
        self.assertIn("+9 dB", html)
        self.assertIn("25 W", html)

    def test_tiles_for_every_band(self):
        bands = pd.DataFrame({"Status": ["Open", "Quiet"], "Trend": ["steady", "unclear"], "Receivers": [30, 1],
                              "Good": [20, 1], "Marginal": [5, 0]}, index=["20m", "10m"])
        html = dash.tiles_html(bands, {})
        self.assertIn("20m", html)
        self.assertIn("10m", html)
        self.assertIn("High confidence", html)
        self.assertIn("Low confidence", html)


class SunAndSky(unittest.TestCase):
    def test_space_weather_readings(self):
        words = {r["label"]: r["word"] for r in sw.readings(
            {"flux": 121, "ssn": 66, "kp": 2.0, "xray": 1.2e-6, "xray_max": 1.2e-6, "wind": 430, "bz": -1})}
        self.assertEqual(words["Solar flux"], "Good")
        self.assertEqual(words["Kp index"], "Quiet")
        self.assertEqual(words["X-ray"], "Low activity")
        self.assertEqual(words["Solar wind"], "Calm")

    def test_missing_feeds_are_left_out(self):
        self.assertEqual(sw.readings({}), [])

    def test_top_band_under_the_muf(self):
        self.assertEqual(iono.top_band(10.2), "30m")
        self.assertEqual(iono.top_band(30.0), "10m")
        self.assertIsNone(iono.top_band(1.0))

    def test_shorter_paths_need_a_lower_frequency(self):
        self.assertLess(iono.muf_for(20.0, 300, 500, 3.0), iono.muf_for(20.0, 300, 3000, 3.0))

    def test_sun_position_in_october(self):
        decl, lon = wm._sun_position(datetime(2026, 10, 21, 12, 0))
        self.assertAlmostEqual(decl, -11, delta=1.5)
        self.assertAlmostEqual(lon, -4, delta=1.5)

    def test_day_night_line_stays_inside_the_map(self):
        x, y = wm._terminator(31.0, -102.0, 12000.0, datetime(2026, 10, 21, 12, 0))
        inside = ~np.isnan(x)
        self.assertTrue(inside.any())
        self.assertTrue((np.hypot(x[inside], y[inside]) <= 12000.0 + 1e-6).all())

    def test_map_draws_with_the_day_night_line(self):
        pts = pd.DataFrame({"km": [500.0, 3000.0], "bearing": [10.0, 100.0], "group": "single", "gap": np.nan,
                            "snr_a": [5.0, 3.0], "snr_b": np.nan, "color": "#ffffff"})
        fig = wm.azimuth_map_figure(31.0, -102.0, pts, {"A": "", "B": ""}, "TEST", "mi", 12000.0, "dots", True, (0, 20),
                                    when=datetime(2026, 10, 21, 12, 0))
        self.assertIsNotNone(fig)


if __name__ == "__main__":
    unittest.main()
