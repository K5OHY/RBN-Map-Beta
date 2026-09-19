"""Manually refresh spotter_coords.csv from reversebeacon.net.

The web app does this automatically every 24 hours; this is only for forcing it.
"""
from rbn_data import refresh_skimmer_cache

if __name__ == "__main__":
    print(refresh_skimmer_cache(force=True)[1])
