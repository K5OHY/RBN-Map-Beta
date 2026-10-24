"""Makes the saved example data in `examples/`: the real spots behind each example button, where the transmitter is, and for a
WSPR test which receivers were listening. Needs the internet. Run it again to refresh the examples:

    python build_examples.py            (all of them)
    python build_examples.py rbn long   (just these)
"""
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from data_sources import download_rbn_day
from rbn_data import load_skimmers, lookup_callsign_location
from wspr_data import clean_callsign, fetch_listening, fetch_wspr_spots

OUT = Path(__file__).resolve().parent / "examples"

# name: (callsign, source, day, band for the listening list)
EXAMPLES = {
    "map": ("K5OHY", "RBN", date(2025, 10, 23), None),
    "rbn": ("K5OHY", "RBN", date(2026, 9, 27), None),
    "quick": ("K5OHY", "WSPR", date(2026, 10, 4), "20m"),
    "long": ("N3HXZ", "WSPR", date(2026, 8, 26), "20m"),
}


def build(name):
    call, source, day, band = EXAMPLES[name]
    found = lookup_callsign_location(call, load_skimmers())
    if not found:
        raise SystemExit(f"No location found for {call}.")
    lat, lon, grid, via = found
    meta = {"callsign": call, "kind": source, "file_date": f"{day:%Y%m%d}", "tx_call": None, "listening": None,
            "home": [lat, lon, f"{grid or f'{lat:.2f}, {lon:.2f}'} via {via}"],
            "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    if source == "RBN":
        spots = download_rbn_day(f"{day:%Y%m%d}", call)
    else:
        meta["tx_call"] = clean_callsign(call)
        spots, truncated = fetch_wspr_spots(call, datetime.combine(day, time(0, 0)), datetime.combine(day, time(23, 59, 59)))
        if truncated:
            raise SystemExit(f"{name}: the spot limit was hit, so the day is incomplete.")
    spots.to_csv(OUT / f"{name}.csv.gz", index=False)
    print(f"{name}: {len(spots)} spots")
    if band:
        on_band = spots[spots["band"] == band]
        start, end = on_band["time"].min().to_pydatetime(), (on_band["time"].max() + timedelta(minutes=2)).to_pydatetime()
        listening, truncated = fetch_listening(band, start, end)
        if truncated:
            raise SystemExit(f"{name}: the listening list hit its limit.")
        listening.to_csv(OUT / f"{name}_listening.csv.gz", index=False)
        meta["listening"] = {"band": band, "start": f"{start:%Y-%m-%d %H:%M:%S}", "end": f"{end:%Y-%m-%d %H:%M:%S}"}
        print(f"{name}: {len(listening)} listening rows on {band}")
    (OUT / f"{name}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for chosen in sys.argv[1:] or EXAMPLES:
        build(chosen)
