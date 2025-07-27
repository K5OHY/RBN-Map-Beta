import re
import pandas as pd
from web import grid_square_to_latlon  # from your main file

def main():
    with open("spotters_raw.txt", "r", encoding="utf-8") as f:
        lines = f.readlines()

    pattern = re.compile(r"^(\\S+)\\s+(?:[^\\s]*,?)*\\s+([A-R]{2}\\d{2}[a-xA-X]{2})\\s")
    records = []

    for line in lines:
        match = pattern.search(line)
        if match:
            callsign, grid = match.groups()
            try:
                lat, lon = grid_square_to_latlon(grid.upper())
                records.append((callsign, lat, lon))
            except Exception:
                continue

    df = pd.DataFrame(records, columns=[\"callsign\", \"latitude\", \"longitude\"])
    df.drop_duplicates(subset=\"callsign\", inplace=True)
    df.to_csv(\"spotter_coords.csv\", index=False)

if __name__ == \"__main__\":
    main()

