"""Saved copies of the real data behind the example buttons, so an example opens at once and shows the same thing every time,
without waiting on the Reverse Beacon Network or wspr.live. `build_examples.py` makes them; the files live in `examples/`."""
import json
from pathlib import Path

import pandas as pd

DIR = Path(__file__).resolve().parent / "examples"


def available(name):
    return (DIR / f"{name}.json").exists() and (DIR / f"{name}.csv.gz").exists()


def load(name):
    """(meta, spots) for an example, or None when its files are missing."""
    if not available(name):
        return None
    meta = json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))
    spots = pd.read_csv(DIR / f"{name}.csv.gz", parse_dates=["time"])
    return meta, spots


def listening(band, start, end):
    """What a saved example knows about which receivers were decoding on `band` between `start` and `end`, in the shape
    wspr_data.fetch_listening returns, or None when no saved example covers that."""
    for path in sorted(DIR.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        saved = meta.get("listening")
        file = DIR / f"{path.stem}_listening.csv.gz"
        if not saved or saved["band"] != band or not file.exists():
            continue
        lo, hi = pd.Timestamp(saved["start"]), pd.Timestamp(saved["end"])
        if lo <= pd.Timestamp(start) and pd.Timestamp(end) <= hi:
            rows = pd.read_csv(file, parse_dates=["time"])
            return rows[(rows["time"] >= pd.Timestamp(start)) & (rows["time"] <= pd.Timestamp(end))].reset_index(drop=True), False
    return None
