import pandas as pd
import folium
import matplotlib.colors as mcolors
from gridtools import Grid
import requests
import zipfile
import os
from io import BytesIO
import streamlit as st

def download_and_extract_rbn_data(date):
    url = f'https://data.reversebeacon.net/rbn_history/{date}.zip'
    response = requests.get(url)
    if response.status_code == 200:
        with zipfile.ZipFile(BytesIO(response.content)) as z:
            csv_filename = None
            for file_info in z.infolist():
                if file_info.filename.endswith('.csv'):
                    csv_filename = file_info.filename
                    z.extract(csv_filename)
                    break
            if csv_filename is None:
                raise Exception("No CSV file found in the ZIP archive")
            return csv_filename
    else:
        raise Exception(f"Error downloading RBN data: {response.status_code}")

def process_pasted_data(pasted_data):
    # Remove unnecessary lines and split the data
    lines = pasted_data.split('\n')
    lines = [line.strip() for line in lines if line.strip() and not line.startswith('●')]
    
    # Extract columns and data
    data = []
    for line in lines[1:]:  # Skip the header
        parts = line.split()
        spotter = parts[0]
        spotted = parts[1]
        distance = parts[2] + ' ' + parts[3]
        freq = parts[4]
        mode = parts[5]
        type_ = parts[6]
        snr = parts[7] + ' ' + parts[8]
        speed = parts[9] + ' ' + parts[10]
        time = parts[11] + ' ' + parts[12] + ' ' + parts[13]
        seen = ' '.join(parts[14:])
        data.append([spotter, spotted, distance, freq, mode, type_, snr, speed, time, seen])
    
    # Create a DataFrame
    df = pd.DataFrame(data, columns=['spotter', 'spotted', 'distance', 'freq', 'mode', 'type', 'snr', 'speed', 'time', 'seen'])
    
    # Convert SNR to numeric
    df['snr'] = df['snr'].str.split().str[0].astype(float)
    
    # Extract frequency as float
    df['freq'] = df['freq'].astype(float)
    
    return df

def get_color(snr):
    color_map = mcolors.LinearSegmentedColormap.from_list('custom', ['green', 'yellow', 'red'])
    return mcolors.to_hex(color_map(snr / 30))

def get_band(freq):
    if 1.8 <= freq <= 2.0:
        return '160m'
    elif 3.5 <= freq <= 4.0:
        return '80m'
    elif 7.0 <= freq <= 7.3:
        return '40m'
    elif 10.1 <= freq <= 10.15:
        return '30m'
    elif 14.0 <= freq <= 14.35:
        return '20m'
    elif 18.068 <= freq <= 18.168:
        return '17m'
    elif 21.0 <= freq <= 21.45:
        return '15m'
    elif 24.89 <= freq <= 24.99:
        return '12m'
    elif 28.0 <= freq <= 29.7:
        return '10m'
    elif 50.0 <= freq <= 54.0:
        return '6m'
    else:
        return 'unknown'

def create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons):
    m = folium.Map(location=[39.8283, -98.5795], zoom_start=4)

    # Add small black dots at every beacon's location if show_all_beacons is True
    if show_all_beacons:
        for spotter, coords in spotter_coords.items():
            folium.CircleMarker(
                location=coords,
                radius=1,  # Small black dot
                color='black',
                fill=True,
                fill_color='black'
            ).add_to(m)

    # Add the spotter locations to the map with varying marker sizes based on SNR
    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            snr = row['snr']
            folium.CircleMarker(
                location=coords,
                radius=snr / 2,  # Scale the size based on SNR
                popup=f'Spotter: {spotter}<br>SNR: {snr} dB',
                color=get_color(snr),
                fill=True,
                fill_color=get_color(snr)
            ).add_to(m)

    # Add grid square marker
    folium.Marker(
        location=grid_square_coords,
        icon=folium.Icon(icon='star', color='red'),
        popup=f'Your Location: {grid_square}'
    ).add_to(m)
    
    # Define colors for different ham bands
    band_colors = {
        '160m': 'blue',
        '80m': 'green',
        '40m': 'teal',
        '30m': 'purple',
        '20m': 'darkblue',  # Set 20m to dark blue
        '17m': 'orange',
        '15m': 'lime',
        '12m': 'pink',
        '10m': 'red',
        '6m': 'magenta'
    }

    # Add lines with different colors based on ham bands
    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            band = get_band(row['freq'])
            color = band_colors.get(band, 'blue')  # Default to blue if band not found
            folium.PolyLine(
                locations=[grid_square_coords, coords],
                color=color,
                weight=1
            ).add_to(m)
    
    # Add a smaller legend
    legend_html = '''
     <div style="position: fixed; 
     bottom: 20px; left: 20px; width: 120px; height: 180px; 
     border:1px solid grey; z-index:9999; font-size:10px;
     background-color:white;
     ">
     &nbsp; <b>Legend</b> <br>
     &nbsp; 160m &nbsp; <i class="fa fa-circle" style="color:blue"></i><br>
     &nbsp; 80m &nbsp; <i class="fa fa-circle" style="color:green"></i><br>
     &nbsp; 40m &nbsp; <i class="fa fa-circle" style="color:teal"></i><br>
     &nbsp; 30m &nbsp; <i class="fa fa-circle" style="color:purple"></i><br>
     &nbsp; 20m &nbsp; <i class="fa fa-circle" style="color:darkblue"></i><br>
     &nbsp; 17m &nbsp; <i class="fa fa-circle" style="color:orange"></i><br>
     &nbsp; 15m &nbsp; <i class="fa fa-circle" style="color:lime"></i><br>
     &nbsp; 12m &nbsp; <i class="fa fa-circle" style="color:pink"></i><br>
     &nbsp; 10m &nbsp; <i class="fa fa-circle" style="color:red"></i><br>
     &nbsp; 6m &nbsp; <i class="fa fa-circle" style="color:magenta"></i><br>
     </div>
     '''
    m.get_root().html.add_child(folium.Element(legend_html))

    return m

# Streamlit app
st.title("RBN Signal Map Generator")

callsign = st.text_input("Enter Callsign:")
date = st.text_input("Enter the date (YYYYMMDD):", "")
grid_square = st.text_input("Enter Grid Square:")
show_all_beacons = st.checkbox("Show all reverse beacons")

pasted_data = st.text_area("Paste the RBN data here:")

if st.button("Generate Map"):
    try:
        if date:
            csv_filename = download_and_extract_rbn_data(date)
            df = pd.read_csv(csv_filename)
            os.remove(csv_filename)
            
            filtered_df = df[df['dx'] == callsign].copy()
            filtered_df['snr'] = pd.to_numeric(filtered_df['db'], errors='coerce')
            filtered_df['freq'] = pd.to_numeric(filtered_df['freq'], errors='coerce')
            filtered_df['spotter'] = filtered_df['de']
        else:
            df = process_pasted_data(pasted_data)
            filtered_df = df[df['spotted'] == callsign].copy()

        spotter_coords = {
            'OZ1AAB': (55.7, 12.6),
            'HA1VHF': (47.9, 19.2),
            'W6YX': (37.4, -122.2),
            'KV4TT': (36.0, -79.8),
            'W4AX': (34.2, -84.0),
            'HA6PX': (47.1, 18.9),
            'DF7GB': (49.0, 9.2),
            'AC0C-1': (39.0, -94.5),
            'GI4DOH': (54.5, -6.3),
            'WE4M': (36.8, -79.4),
            'HG8A': (46.7, 21.1),
            'OH0K/6': (60.1, 19.9),
            'UP2L': (51.2, 51.3),
            'K4PP': (32.6, -83.6),
            'VK3RASA': (-37.8, 145.0),
            'BG4GOV': (31.2, 121.5),
            'UA0S': (45.0, 135.0),
            'BH4XDZ': (32.0, 120.0),
            'SP8R': (50.0, 22.0),
            'LZ7AA': (42.7, 23.3),
            'K6FOD': (34.1, -118.3),
            'OE6TZE': (47.2, 15.3),
            'ZF9CW': (19.3, -81.3),
            'G3XBI': (51.0, -1.0),
            'OZ4ADX': (56.0, 9.0),
            'SM1HEV': (57.6, 18.3),
            'K9LC': (42.1, -87.9),
            'DK8NE': (50.1, 8.6),
            'DC8YZ': (49.5, 11.5),
            'R6YY': (45.0, 41.0),
            'DK2GOX': (50.0, 8.5),
            'HG0Y': (47.1, 19.5),
            'S50U': (46.2, 15.3),
            'DJ9IE': (51.1, 10.0),
            'LA6TPA': (60.0, 10.0),
            'KD2OGR': (40.7, -74.0),
            'DE1LON': (51.2, 6.8),
            'BI4SSB': (30.7, 114.3),
            'MM3NDH-3': (57.5, -4.3),
            'N7TUG': (47.6, -122.3),
            'DK9IP': (48.8, 9.2),
            'KC4YVA': (38.0, -78.5),
            'DD5XX': (50.8, 10.3),
            'BI4MPH-1': (32.1, 118.7),
            'DK9IP-1': (48.8, 9.2),
            'SV1CDN': (38.0, 23.7),
            'LZ3CB': (42.6, 23.4),
            '3V8SS': (36.8, 10.2),
            'S53A': (46.1, 14.5),
            'K9IMM': (42.0, -88.0),
            'HB9DCO': (47.2, 8.3),
            'DL9GTB': (53.0, 8.8),
            'WV4P': (35.2, -88.2),
            'YO4RDW': (45.2, 28.6),
            'K7MJG': (38.0, -89.0),
            'TI7W': (9.9, -84.0),
            'RU9CZD': (53.5, 91.0),
            'W3UA': (42.5, -71.6),
            '9M2CNC': (3.2, 101.7),
            'EA5WU': (38.9, -0.5),
            'SV8RV': (37.9, 23.7),
            'EA5RQ': (38.7, -0.4),
            '7Q6M': (-13.5, 34.0),
            'WW1L': (41.4, -73.2),
            'ES2RR': (59.4, 24.8),
            'S53M': (46.2, 14.5),
            'N6TV': (37.3, -121.9),
            'G4ZFE': (51.5, -0.2),
            'DK3UA': (53.5, 10.0),
            'OK1FCJ': (50.1, 14.5),
            'KO7SS': (34.0, -112.0),
            'BH4RXP': (30.7, 104.1),
            'JN1ILK': (35.7, 139.7),
            'KA7OEI': (40.8, -111.9),
            'EA2CW': (43.3, -2.9),
            'DK0TE': (48.3, 11.0),
            'OE9GHV': (47.3, 9.8),
            'BA6KC': (30.0, 120.0),
            'RK3TD': (55.6, 37.6),
            'G0KTN': (51.2, -0.6),
            'SV1DPJ': (37.8, 23.7),
            'F4VVG': (47.8, 7.0),
            'W1NT-2': (43.1, -71.5),
            'V51YJ': (-22.6, 17.1),
            'EA1URA': (43.3, -2.0),
            'DM5GG': (50.7, 13.0),
            'W8WWV': (41.0, -81.4),
            'OG66X': (60.4, 24.8),
            'VE7XMC': (49.3, -123.0),
            'WE9V': (42.0, -88.2),
            'VE6WZ': (51.0, -114.0),
            'J68HZ': (13.9, -60.9),
            'KM3T-3': (42.3, -71.1),
            'BG2TFW': (43.9, 125.3),
            '9A1CIG': (45.8, 16.0),
            'W1NT-6': (43.1, -71.5),
            'N0OI': (32.9, -97.0),
            'SQ5J': (52.2, 21.0),
            'SE5E': (58.2, 12.3),
            'W2NAF': (40.7, -75.2),
            '2E0INH': (52.6, -2.1),
            'ON6ZQ': (51.0, 4.0),
            'WC8GOP': (41.5, -81.7),
            'M7TAW': (51.5, -0.1),
            'LZ5DI': (42.1, 24.7),
            'DL5RCN': (50.8, 10.3),
            'PA5WT': (52.1, 5.2),
            'MM3NDH': (56.5, -4.0),
            'BI4UYX': (30.7, 120.6),
            'VE3EID': (43.7, -79.4),
            'WZ7I': (40.0, -75.2),
            'BH4RRG': (30.6, 114.3),
            'G4IRN': (53.4, -2.4),
            'VE7CC': (49.3, -123.0),
            'VE6WZ-2': (51.0, -114.0),
            'OK4QRO': (49.7, 15.0),
            'SZ1A': (38.0, 23.7),
            'WB6BEE': (37.7, -122.4),
            'K2PO/7': (45.5, -122.7),
            'JA1JRS': (35.7, 139.7),
            'WS3W': (39.9, -75.2),
            'DL0LA': (50.9, 6.9),
            'YO2MAX': (45.8, 21.3),
            'DL8LAS': (50.9, 13.8),
            'W3DAN': (41.1, -80.4),
            'KM3T': (42.3, -71.1),
            'JH7CSU1': (35.7, 139.7),
            'ZS1NN': (-33.9, 18.5),
            'N9CO': (41.8, -88.2),
            'N8DXE': (41.1, -80.4),
            'SM7IUN': (55.6, 13.0),
            'W4KAZ': (35.9, -78.8),
            'CR6K': (38.7, -9.1),
            'VE6JY': (53.5, -113.5),
            'NG7M': (40.8, -111.9),
            'S53WW': (45.8, 14.5),
            'WC2L': (40.8, -74.2),
            'DL8TG': (51.0, 13.0),
            'E28AC': (15.1, 100.8),
            'PA8MM': (52.3, 5.3),
            'DL0PF': (49.0, 10.4),
            'IK3STG': (45.5, 12.0),
            'JP1LRT': (35.7, 139.7),
            'WA7LNW': (40.8, -111.9),
            'MW0MUT': (51.7, -3.3),
            'MM3NDH-2': (56.5, -4.0),
            'K5TR': (30.3, -97.8),
            'DR4W': (49.5, 11.5),
            'W3LPL': (39.1, -77.1),
            'KH6LC': (20.0, -155.8),
            'R1LB': (60.0, 30.3),
            'DF2CK': (50.0, 8.0),
            'ZL4YL': (-45.8, 170.5),
            'K7CO': (40.6, -111.9),
            'S59DBC': (45.7, 15.0),
            'NN3RP': (39.0, -77.0),
            'LZ4AE': (42.7, 25.2),
            'VR2FUN-77': (22.3, 114.2),
            'AA0O': (30.0, -90.0),
            'OK1HRA': (50.1, 14.5),
            'K7EK': (47.6, -122.3),
            'BI4RFP-YZ': (30.7, 104.1),
            'JI1HFJ': (35.7, 139.7),
            'VU2CPL': (12.9, 77.6),
            'SM0TCZ': (59.3, 18.1),
            'PE5TT': (52.0, 5.4),
            '7N4XCV': (35.7, 139.7),
            'MM0ZBH': (56.5, -4.0),
            'DL1HWS': (51.0, 13.7),
            'VE6WZ-1': (51.0, -114.0),
            'UA4CC': (55.8, 49.1),
            'BG0AJO': (36.7, 117.0),
            'ET3AA': (9.0, 38.8),
            'BD8CS': (30.6, 114.3),
            'KP3CW': (18.2, -66.0),
            'W2MV': (41.5, -74.4),
            'HA8TKS': (47.1, 19.0),
            'VK6ANC': (-31.9, 115.9),
            'IK4VET': (44.8, 11.6),
            'VK2EBN': (-34.0, 151.1),
            'HB9BXE': (47.4, 8.6),
            'IK6HIR': (43.3, 13.3),
            '3D2AG': (-18.0, 178.5),
            'JJ2VLY': (35.7, 139.7),
            'DM6EE': (51.3, 8.0),
            'NT6Q': (37.7, -122.5),
            'HA5PP': (47.5, 19.1),
            'OH8KA': (65.0, 25.5),
            'UA4M': (56.1, 47.3),
            'LA7GIA': (60.1, 10.2),
            'NH6HI': (21.3, -157.8),
            'VE6AO': (51.0, -114.1),
            'LZ4UX': (42.7, 23.3),
            'EA7/VE3NZ': (36.7, -4.5),
            'K3PA-1': (39.0, -94.5),
            'TF3Y': (64.1, -21.9),
            'OH6BG': (63.1, 23.0),
            'JO1YYP': (35.7, 139.7),
            'N8NJH': (41.4, -81.0),
            'ND7K': (33.4, -112.0),
            'VY0ERC': (82.5, -62.3),
            'S50ARX': (46.0, 14.5),
            'G3YPP': (52.0, -1.0),
            'UD4FD': (54.7, 56.0),
            'SP5AI': (52.2, 21.0),
            'EA4FIT': (40.4, -3.7),
            'DJ1RK': (50.8, 9.5),
            'HG5FMV': (47.1, 19.5),
            'ZL3X': (-43.5, 172.6),
            'UY2RA': (49.1, 33.4),
            'F8DGY': (48.9, 2.3),
            'KE3BK': (37.8, -122.4),
            'W3OA': (35.2, -78.7),
            'OZ1BZS': (55.5, 9.0),
            'K3PA-2': (39.0, -94.5),
            'W8WTS': (41.5, -81.5),
            'N4ZR': (39.2, -77.6),
            'PA3GRM': (52.0, 5.3),
            'PA0MBO': (52.1, 5.0),
            'OH4KA': (62.0, 25.7),
            'KM3T-1': (42.3, -71.1),
            'KM3T-2': (42.3, -71.1),
            'EA/VE3NZ': (36.7, -4.5),
            'KM5SW': (32.5, -97.3),
            'ZF1A': (19.3, -81.3),
            'W3RGA': (39.8, -77.0),
            'DK1MAX': (48.1, 11.5),
            'KX7M': (39.0, -119.8),
            'BD7JNA': (24.4, 113.7),
            'LY2XW': (54.9, 23.9),
            'PA5KT': (52.0, 4.5),
            'W7VJ': (47.6, -122.3),
            'ON7KEC': (51.0, 3.0),
            'DM7EE': (51.0, 13.8),
            'W3RGA-2': (39.8, -77.0),
            'DJ3AK': (52.5, 13.5),
            'ON4EM': (50.8, 4.4),
            'DL1EFW': (50.8, 7.0),
            'SQ5OUO': (52.2, 21.0),
            'JH4UTP': (34.7, 135.5),
            'JI1ACI': (35.7, 139.7),
            'DJ2BC': (53.1, 9.1),
            'JA1BJI': (35.7, 139.7),
            'UT5R': (49.8, 24.0),
            'HG3X': (47.3, 19.0),
            'YL2VW': (56.9, 24.2),
            'LU8XW': (-34.5, -58.5),
            'N4SBJ': (36.0, -79.8),
            'DF1DR': (50.8, 6.9),
            'ZF1A-11': (19.3, -81.3),
            'EL2BG': (6.4, -10.7),
            'DF2CK-1': (50.0, 8.0),
            'XV9Q': (10.8, 106.7),
            'LY3G': (55.1, 23.9),
            'BI4MPH': (32.1, 118.7),
            'BD4QJP': (31.2, 121.5),
            'EA2RCF-4': (42.8, -1.7),
            'AA4VV': (35.8, -81.6),
            'OH4MFA': (64.0, 27.6),
            'VK2RH': (-33.8, 151.2),
            'OE8TED': (46.6, 14.3),
            'G4MKR': (52.0, -0.2),
            '3B8CW': (-20.2, 57.5),
            'EL2BG/W4': (32.2, -95.8),
            'BG0ARE': (36.7, 117.0),
            'PD0WAG': (52.0, 5.8),
            'TZ4AM': (12.6, -8.0),
            'YU7GM': (45.3, 19.8),
            'K1RA': (39.0, -77.3),
            'SP5ABT': (52.2, 21.0),
            'CT2IWW': (40.2, -8.4),
            'EA5RQ/A': (38.7, -0.4),
            'DD5XX-3': (50.8, 10.3),
            'EA8BFK': (28.1, -15.4),
            'BD4QJP1': (31.2, 121.5),
            'W2LB': (42.9, -75.0),
            'LB9KJ': (59.0, 9.6),
            'W4IPC': (36.8, -79.4),
            'IZ2CPS': (45.5, 9.2),
            'VA3UXA': (43.7, -79.4),
            'OG73X': (60.4, 24.8),
            'OH2BBT': (60.2, 24.9),
            'F5AHD': (47.3, 6.1),
            'HA2NA': (46.4, 17.0),
            'CX6VM': (-34.9, -56.2),
            'G4BRK': (51.5, -0.2),
            'TF4M': (66.3, -18.1),
            'LY2BGP': (55.1, 23.9),
            'EI6LF': (53.3, -8.0),
            'F6IIT': (48.8, 2.3),
            'DL1AMQ': (51.5, 9.5),
            'HA2NA-5': (46.4, 17.0),
            'JG1DLY': (35.7, 139.7),
            'DL8LAS-1': (50.9, 13.8),
            'RW9AV': (58.0, 56.3),
            'M0JYE': (52.2, -0.8),
            'N2QT': (37.2, -76.5),
            'BD7DT': (24.8, 113.6),
            'BD4QJP-1': (31.2, 121.5),
            'BH4SCF': (30.7, 104.1),
            'K9QC': (41.9, -87.7),
            'EA5RQ/P': (38.7, -0.4),
            'DD0VS': (49.5, 11.5),
            'EA5RQ/FLEX': (38.7, -0.4),
            'RN4WA': (56.0, 37.2),
            'VK2GEL': (-33.8, 151.2),
            'BG7IBS': (23.0, 113.0),
            'BG4WOM': (31.3, 121.5),
            'OG6K': (61.7, 25.8),
            'BI4RFP': (30.7, 104.1),
            'PD2RPS': (52.0, 5.4),
            'SQ8J': (50.0, 22.0),
            'G0TMX': (52.0, 0.1),
            'F1FCA-7': (48.0, 2.4),
            'JR1Y': (35.7, 139.7),
            'F1TRE': (47.8, 7.0),
            'YO5LD': (47.7, 26.2),
            'F6KGL': (48.9, 2.3),
            'IZ4DIW': (44.8, 11.6),
            'DF4UE': (49.0, 10.4),
            'BY4XRA': (31.2, 121.5),
            'M0WJG': (52.2, -0.1),
            'RL3A': (56.3, 38.1),
            'DM5GG-2': (50.7, 13.0),
            'HA5PP-2': (47.5, 19.1),
            'W1UE': (42.3, -71.0),
            'P40L': (12.5, -69.9),
            'KQ8M': (41.5, -81.4),
            'G4BVY-3': (52.0, -2.0),
            'IK7YTT': (41.1, 16.9),
            'UT4QM': (50.3, 30.4),
            'EY8ZE': (37.8, 67.7),
            'DR5X': (54.0, 13.2),
            'VE3NZ': (43.7, -79.4),
            'K5KHK': (42.7, -73.7),
            'BG2TEM': (43.9, 125.3),
            'IW9GTD': (37.5, 15.1),
            'DK8NE/0': (50.1, 8.6),
            'PC5Q': (52.2, 5.2),
            'RO5F': (54.7, 20.5),
            'AA4VT': (36.0, -79.8),
            'K5EM': (47.6, -122.3),
            'EA1RX': (42.2, -8.7),
            'BA6KC1': (30.0, 120.0),
            'DK3WW': (53.1, 9.1),
            '5W1SA': (-13.8, -172.0),
            'W6BB': (37.9, -122.3),
            'NU6XB': (37.9, -122.3),
            'DM5I': (49.5, 11.5),
            'IV3DXW': (46.1, 13.2)
        }

        grid = Grid(grid_square)
        grid_square_coords = (grid.lat, grid.long)
        
        m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons)
        m.save('map.html')
        st.write("Map generated successfully!")
        
        # Display map
        st.components.v1.html(open('map.html', 'r').read(), height=700)

        # Provide download link
        with open("map.html", "rb") as file:
            btn = st.download_button(
                label="Download Map",
                data=file,
                file_name="RBN_signal_map_with_snr.html",
                mime="text/html"
            )
    except Exception as e:
        st.error(f"Error: {e}")

