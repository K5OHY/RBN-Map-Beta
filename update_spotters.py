import re
import pandas as pd
from web import grid_square_to_latlon

# Raw pasted data from your input
with open('spotters_raw.txt', 'r') as f:
    raw_data = f.readlines()

# Regex to extract lines with at least callsign and valid grid square (6-char)
pattern = re.compile(r"^(\S+)\s+(?:[^\s]*,?)*\s+([A-R]{2}\d{2}[a-xA-X]{2})\s")

records = []

for line in raw_data:
    match = pattern.search(line)
    if match:
        callsign, grid = match.groups()
        try:
            lat, lon = grid_square_to_latlon(grid.upper())
            records.append((callsign, lat, lon))
        except Exception as e:
            print(f"Invalid grid {grid} for {callsign}: {e}")

# Create and save the cleaned DataFrame
spotter_df = pd.DataFrame(records, columns=['callsign', 'latitude', 'longitude'])
spotter_df.drop_duplicates(subset='callsign', inplace=True)
spotter_df.to_csv('spotter_coords.csv', index=False)

print(f"Updated spotter_coords.csv with {len(spotter_df)} entries.")
