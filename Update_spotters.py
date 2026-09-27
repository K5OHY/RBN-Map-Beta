#!/usr/bin/env python3
import csv
import re
from collections import OrderedDict

# Paste your RBN table here (exactly as copied)
RBN_DATA = r"""
callsign	band	grid	dxcc	cont	itu	cq	first seen	last seen
ZL3X		RE66IR	ZL	OC	60	32	5 years ago	online
BH4XDZ	10m,15m,17m,20m,40m	OM94NO	BY	AS	44	24	5 years ago	online
... paste everything here ...
"""

GRID_RE = re.compile(r"^[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?([0-9]{2})?$")


def maidenhead_to_latlon(locator: str):
    loc = locator.strip().upper()
    if len(loc) < 4:
        raise ValueError("locator too short")

    lon = -180.0
    lat = -90.0

    # Field
    lon += (ord(loc[0]) - 65) * 20
    lat += (ord(loc[1]) - 65) * 10

    # Square
    lon += int(loc[2]) * 2
    lat += int(loc[3]) * 1

    lon_size = 2.0
    lat_size = 1.0

    # Subsquare
    if len(loc) >= 6 and loc[4].isalpha():
        lon += (ord(loc[4]) - 65) * (2.0 / 24)
        lat += (ord(loc[5]) - 65) * (1.0 / 24)
        lon_size /= 24
        lat_size /= 24

    # Extended
    if len(loc) >= 8 and loc[6].isdigit():
        lon += int(loc[6]) * (lon_size / 10)
        lat += int(loc[7]) * (lat_size / 10)
        lon_size /= 10
        lat_size /= 10

    return round(lat + lat_size / 2, 3), round(lon + lon_size / 2, 3)


def main():
    rows = OrderedDict()

    for line in RBN_DATA.splitlines():
        if not line.strip() or line.lower().startswith("callsign"):
            continue

        # Prefer tabs
        parts = line.split("\t")
        if len(parts) < 3:
            continue

        callsign = re.sub(r"\s+", "-", parts[0].strip())
        grid = parts[2].strip()

        if not callsign or not grid or not GRID_RE.match(grid):
            continue

        try:
            lat, lon = maidenhead_to_latlon(grid)
        except Exception:
            continue

        # keep LAST occurrence
        rows[callsign] = (lat, lon)

    with open("updated_spotter_coords.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["callsign", "latitude", "longitude"])
        for cs, (lat, lon) in rows.items():
            w.writerow([cs, lat, lon])

    print(f"Wrote {len(rows)} spotters to updated_spotter_coords.csv")


if __name__ == "__main__":
    main()
