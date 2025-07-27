# update_spotters.py
import pandas as pd

def main():
    raw_file = "spotters_raw.txt"
    output_file = "spotter_coords.csv"

    data = []
    with open(raw_file, "r") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 3:
                callsign = parts[0]
                lat = float(parts[1])
                lon = float(parts[2])
                data.append({"callsign": callsign, "latitude": lat, "longitude": lon})

    df = pd.DataFrame(data)
    df.to_csv(output_file, index=False)
