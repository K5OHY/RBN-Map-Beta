"""The program against a published study. Dale, N3HXZ, compared a tuned elevated vertical with a ground-mounted 17 ft whip on a
5:1 matching unit on 26 August 2026: WSPR on 20m at 1 W, alternating 30 minute blocks from 12:00 to 21:30 UTC with the
ground-mounted whip (the reference, A) first, using the stations that recorded 40 or more spots per antenna. His table gives each
station's relative gain, the elevated vertical minus the ground-mounted one in dB, from linear-power averaging.

These checks download that day's spots from wspr.live, so they only run when you ask for them:

    set RBN_TEST_NETWORK=1 && python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import compare_stats as cs  # noqa: E402
import wspr_data as wd  # noqa: E402
from rbn_data import maidenhead_to_latlon  # noqa: E402

DALE_RELATIVE_GAIN_DB = {  # station: relative gain from the table in "Comparing Performance between an Elevated and a Ground mounted Vertical Antenna"
    "AC0G/B4": 5.403969, "KV0S": 5.780277, "NI5F": 2.001499, "NI5F-4": 5.828897, "KF4JU": 6.072543, "KD2M": 2.942492,
    "AC0G/ND": 5.082934, "KN4TGQ": 5.414176, "K4DDI": 4.116029, "N5MCB": 1.494609, "KI5MQY": 1.403417, "VE4DRK": 6.493243,
    "KC5HHQ": 3.255781, "KD5QLM": 1.882696, "VE4KRK": 5.617567, "K5XL": 1.817759, "N5DUX": 3.117083, "K5JBT": 5.404188,
    "N5MN": 0.32614, "KD7EFG": 6.923649, "KD7EFG-1": 7.056082, "AF7KR": 7.614699, "VE6JY": 5.420208, "KW7MM": 7.654218,
    "KB7GF": 1.617767, "K6VZK": 6.398291, "KFS/OMNI": 5.784728,
}
NETWORK = os.environ.get("RBN_TEST_NETWORK") == "1"


@unittest.skipUnless(NETWORK, "set RBN_TEST_NETWORK=1 to run the checks that use the internet")
class DalePerformerStudy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spots, _ = wd.fetch_wspr_spots("N3HXZ", datetime(2026, 8, 26), datetime(2026, 8, 27))
        spots = spots[spots["band"] == "20m"].copy()
        t0 = pd.Timestamp("2026-08-26 12:00")
        block = ((spots["time"] - t0).dt.total_seconds() // 1800).astype(int)
        spots = spots[(block >= 0) & (block < 19)]
        side = np.where(block[spots.index] % 2 == 0, "A", "B")
        cls.a, cls.b = spots[side == "A"], spots[side == "B"]
        cls.table = cs.station_gains(cls.a, cls.b, wd.receiver_locations(spots), maidenhead_to_latlon("EN90xi")[:2], "linear", 40)
        cls.table["gain"] = -cls.table["gap"]

    def test_the_spot_counts_match_the_study(self):
        self.assertAlmostEqual(len(self.a) + len(self.b), 9023, delta=60)  # "9,023 spots were recorded"

    def test_every_station_in_the_study_is_found_with_enough_reports(self):
        self.assertEqual([c for c in DALE_RELATIVE_GAIN_DB if c not in self.table.index], [])

    def test_each_stations_relative_gain_matches_the_published_table(self):
        diffs = pd.Series({c: self.table.loc[c, "gain"] - g for c, g in DALE_RELATIVE_GAIN_DB.items()})
        self.assertLess(diffs.abs().max(), 0.25, diffs.abs().sort_values().tail(3).to_dict())
        self.assertLess(abs(diffs.mean()), 0.05)

    def test_the_distance_averages_match_the_study(self):
        miles = self.table["km_a"] / 1.609344
        near = self.table[(miles >= 500) & (miles < 1500)]["gain"].mean()
        self.assertAlmostEqual(near, 3.86, delta=0.15)  # his region 1 average, 500 to 1,500 miles

    def test_the_performer_is_stronger_at_every_station(self):
        self.assertTrue((self.table["gain"] > 0).all())


if __name__ == "__main__":
    unittest.main()
