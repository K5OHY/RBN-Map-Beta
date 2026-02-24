"""Update spotter_coords.csv from Reverse Beacon Nodes data.

Supported inputs:
1) Direct URL fetch (default: https://www.reversebeacon.net/nodes/)
2) A local text/TSV file copied from the nodes page

The script extracts callsign + Maidenhead grid, converts grid to lat/lon, and
writes a deduplicated CSV file.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import OrderedDict
from pathlib import Path
from typing import Iterable

# Paste your RBN table here (exactly as copied)
RBN_DATA = r"""
callsign	band	grid	dxcc	cont	itu	cq	first seen	last seen
ZL3X		RE66IR	ZL	OC	60	32	5 years ago	online
BH4XDZ	10m,15m,17m,20m,40m	OM94NO	BY	AS	44	24	5 years ago	online
... paste everything here ...
"""
import requests
from requests import RequestException
from bs4 import BeautifulSoup

GRID_RE = re.compile(r"^[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?([0-9]{2})?$")
DEFAULT_SOURCE_URL = "https://www.reversebeacon.net/nodes/"
DEFAULT_OUTPUT = "spotter_coords.csv"
GRID_RE = re.compile(r"^[A-Ra-r]{2}\d{2}(?:[A-Xa-x]{2})?(?:\d{2})?$")


def maidenhead_to_latlon(locator: str):
def maidenhead_to_latlon(locator: str) -> tuple[float, float]:
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
    lat += int(loc[3])

    lon_size = 2.0
    lat_size = 1.0

    # Subsquare
    if len(loc) >= 6 and loc[4].isalpha():
    if len(loc) >= 6 and loc[4].isalpha() and loc[5].isalpha():
        lon += (ord(loc[4]) - 65) * (2.0 / 24)
        lat += (ord(loc[5]) - 65) * (1.0 / 24)
        lon_size /= 24
        lat_size /= 24

    # Extended
    if len(loc) >= 8 and loc[6].isdigit():
    # Extended square
    if len(loc) >= 8 and loc[6].isdigit() and loc[7].isdigit():
        lon += int(loc[6]) * (lon_size / 10)
        lat += int(loc[7]) * (lat_size / 10)
        lon_size /= 10
        lat_size /= 10

    return round(lat + lat_size / 2, 3), round(lon + lon_size / 2, 3)


def main():
    rows = OrderedDict()
def parse_tsv_lines(lines: Iterable[str]) -> list[tuple[str, str]]:
    """Parse tab-separated node lines and return (callsign, grid)."""
    rows: list[tuple[str, str]] = []

    for line in RBN_DATA.splitlines():
        if not line.strip() or line.lower().startswith("callsign"):
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("callsign"):
            continue

        # Prefer tabs
        parts = line.split("\t")
        if len(parts) < 3:
            continue

        callsign = re.sub(r"\s+", "-", parts[0].strip())
        grid = parts[2].strip()
        grid = parts[2].strip().upper()

        if callsign and GRID_RE.match(grid):
            rows.append((callsign, grid))

    return rows

        if not callsign or not grid or not GRID_RE.match(grid):

def parse_nodes_html(html: str) -> list[tuple[str, str]]:
    """Parse callsign+grid from the Reverse Beacon nodes page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[tuple[str, str]] = []

    # Strategy 1: parse HTML table rows
    for tr in soup.select("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 3:
            continue
        callsign = tds[0]
        grid = tds[2].upper()
        if callsign and GRID_RE.match(grid):
            rows.append((callsign, grid))

    if rows:
        return rows

    # Strategy 2: parse tab-separated text blobs embedded in page/scripts
    text = soup.get_text("\n")
    rows = parse_tsv_lines(text.splitlines())

    if rows:
        return rows

    # Strategy 3: regex fallback for lines like: CALL \t ... \t GRID \t ...
    pattern = re.compile(
        r"^([A-Z0-9/\-]+)\t[^\n\t]*\t([A-Ra-r]{2}\d{2}(?:[A-Xa-x]{2})?(?:\d{2})?)\t",
        re.MULTILINE,
    )
    for match in pattern.finditer(html):
        rows.append((match.group(1), match.group(2).upper()))

    return rows


def fetch_url_text(url: str, timeout: int) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def build_output_rows(callsign_grid_rows: Iterable[tuple[str, str]]) -> OrderedDict[str, tuple[float, float]]:
    # keep last occurrence so newer grid wins if duplicate callsigns are present
    out: OrderedDict[str, tuple[float, float]] = OrderedDict()
    for callsign, grid in callsign_grid_rows:
        try:
            lat, lon = maidenhead_to_latlon(grid)
            out[callsign] = maidenhead_to_latlon(grid)
        except Exception:
            continue
    return out


def write_csv(rows: OrderedDict[str, tuple[float, float]], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["callsign", "latitude", "longitude"])
        for callsign, (lat, lon) in rows.items():
            writer.writerow([callsign, lat, lon])

        # keep LAST occurrence
        rows[callsign] = (lat, lon)

    with open("updated_spotter_coords.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["callsign", "latitude", "longitude"])
        for cs, (lat, lon) in rows.items():
            w.writerow([cs, lat, lon])
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update spotter_coords.csv from Reverse Beacon nodes data")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="URL to fetch nodes table from")
    parser.add_argument("--input-file", help="Local file containing pasted tab-separated nodes data")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output CSV path")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    return parser.parse_args()

    print(f"Wrote {len(rows)} spotters to updated_spotter_coords.csv")

def main() -> None:
    args = parse_args()

    if args.input_file:
        source_path = Path(args.input_file)
        text = source_path.read_text(encoding="utf-8")
        parsed = parse_tsv_lines(text.splitlines())
        source_label = f"file:{source_path}"
    else:
        try:
            text = fetch_url_text(args.source_url, args.timeout)
        except RequestException as exc:
            raise SystemExit(
                f"Unable to download nodes data from {args.source_url}: {exc}. "
                "Try using --input-file with copied nodes data."
            ) from exc
        parsed = parse_nodes_html(text)
        source_label = args.source_url

    if not parsed:
        raise SystemExit(
            "No callsign/grid entries were parsed. "
            "If the website layout changed, try '--input-file spotters_raw.txt' with copied data."
        )

    rows = build_output_rows(parsed)
    output_path = Path(args.output)
    write_csv(rows, output_path)

    print(f"Parsed {len(parsed)} entries from {source_label}")
    print(f"Wrote {len(rows)} unique spotters to {output_path}")


if __name__ == "__main__":
    main()
