import requests
import pandas as pd
import folium
import matplotlib.colors as mcolors
import zipfile
import os
import re
from io import BytesIO
import streamlit as st
from datetime import datetime, timedelta, timezone, time
from geopy.distance import geodesic
from bs4 import BeautifulSoup
import time as time_module

DEFAULT_GRID_SQUARE = "DM81wx"

def fetch_spotter_data():
    """
    Fetch spotter data from https://www.reversebeacon.net/nodes/ and return a DataFrame.
    """
    url = "https://www.reversebeacon.net/nodes/"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            # Look for the table with the 'Nodes list' header or specific class
            table = soup.find('table', {'summary': 'Nodes list'}) or soup.find('table')
            if not table:
                st.warning("No table found on the RBN nodes page.")
                return None
            
            data = []
            for row in table.find_all('tr')[1:]:  # Skip header row
                cols = row.find_all('td')
                if len(cols) >= 3:  # Ensure enough columns
                    callsign = cols[0].text.strip()
                    grid_square = cols[2].text.strip()  # Grid square is in the third column
                    if grid_square and len(grid_square) >= 4:  # Validate grid square
                        try:
                            lat, lon = grid_square_to_latlon(grid_square)
                            data.append([callsign, lat, lon])
                        except Exception as e:
                            st.warning(f"Invalid grid square for {callsign}: {grid_square}")
                            continue
                    else:
                        st.warning(f"Missing or invalid grid square for {callsign}: {grid_square}")
            
            if not data:
                st.warning("No valid spotter data extracted from the RBN nodes page.")
                return None
            
            return pd.DataFrame(data, columns=['callsign', 'latitude', 'longitude'])
        else:
            st.error(f"Failed to fetch spotter data: HTTP {response.status_code}")
            return None
    except Exception as e:
        st.error(f"Error fetching spotter data: {e}")
        return None

def update_spotter_coords():
    """
    Update spotter_coords.csv with the latest spotter data from RBN.
    """
    new_spotter_data = fetch_spotter_data()
    if new_spotter_data is not None:
        try:
            existing_data = pd.read_csv('/tmp/spotter_coords.csv')
        except FileNotFoundError:
            try:
                existing_data = pd.read_csv('spotter_coords.csv')
            except FileNotFoundError:
                existing_data = pd.DataFrame(columns=['callsign', 'latitude', 'longitude'])
        
        updated_data = pd.concat([existing_data, new_spotter_data]).drop_duplicates(subset='callsign', keep='last')
        updated_data.to_csv('/tmp/spotter_coords.csv', index=False)
        st.success("Spotter coordinates updated successfully in the background!")
        return True
    st.warning("Failed to update spotter data; using existing data.")
    return False

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

def get_color(snr):
    color_map = mcolors.LinearSegmentedColormap.from_list('custom', ['green', 'yellow', 'red'])
    return mcolors.to_hex(color_map(snr / 30))

def get_band(freq):
    try:
        freq = float(freq)
    except ValueError:
        return 'unknown'
    
    if 1800 <= freq <= 2000:
        return '160m'
    elif 3500 <= freq <= 4000:
        return '80m'
    elif 7000 <= freq <= 7300:
        return '40m'
    elif 10100 <= freq <= 10150:
        return '30m'
    elif 14000 <= freq <= 14350:
        return '20m'
    elif 18068 <= freq <= 18168:
        return '17m'
    elif 21000 <= freq <= 21450:
        return '15m'
    elif 24890 <= freq <= 24990:
        return '12m'
    elif 28000 <= freq <= 29700:
        return '10m'
    elif 50000 <= freq <= 54000:
        return '6m'
    else:
        return 'unknown'

def create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column, callsign, stats):
    m = folium.Map(location=[39.8283, -98.5795], zoom_start=4)

    if show_all_beacons:
        for spotter, coords in spotter_coords.items():
            folium.CircleMarker(
                location=coords,
                radius=1,
                color='black',
                fill=True,
                fill_color='black'
            ).add_to(m)

    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            snr = row['snr']
            time = row['time']
            time_str = time.strftime("%H:%M")
            folium.CircleMarker(
                location=coords,
                radius=snr / 2,
                popup=f'Spotter: {spotter}<br>SNR: {snr} dB<br>Time: {time_str}',
                color=get_color(snr),
                fill=True,
                fill_color=get_color(snr)
            ).add_to(m)

    folium.Marker(
        location=grid_square_coords,
        icon=folium.Icon(icon='star', color='red'),
        popup=f'Your Location: {grid_square}'
    ).add_to(m)
    
    band_colors = {
        '160m': '#FFFF00',
        '80m': '#003300',
        '40m': '#FFA500',
        '30m': '#FF4500',
        '20m': '#0000FF',
        '17m': '#800080',
        '15m': '#696969',
        '12m': '#00FFFF',
        '10m': '#FF00FF',
        '6m': '#F5DEB3',
    }

    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            if use_band_column:
                band = row['band']
            else:
                freq = row['freq']
                band = get_band(freq)
            color = band_colors.get(band, 'blue')

            folium.PolyLine(
                locations=[grid_square_coords, coords],
                color=color,
                weight=1
            ).add_to(m)
    
    band_stats = "<br>".join([f"{band}: {count}" for band, count in stats['bands'].items()])
    
    stats_html = f'''
     <div style="position: absolute; 
     top: 20px; right: 20px; width: 150px; height: auto; 
     border:1px solid grey; z-index:9999; font-size:10px;
     background-color:white;
     padding: 10px;
     ">
     <b>Callsign: {callsign}</b><br>
     Total Spots: {stats['spots']}<br>
     Max Distance: {stats['max_distance']:.2f} mi<br>
     Max SNR: {stats['max_snr']} dB<br>
     Average SNR: {stats['avg_snr']:.2f} dB<br>
     <b>Bands:</b><br>
     {band_stats}
     </div>
     '''
    m.get_root().html.add_child(folium.Element(stats_html))

    legend_html = '''
    <div style="position: absolute; 
    top: 50%; transform: translateY(-50%); left: 20px; width: auto; max-width: 150px; height: auto; 
    border:1px solid grey; z-index:9999; font-size:10px;
    background-color:white;
    padding: 5px;
    word-wrap: break-word;
    ">
    <b>Legend</b><br>
    <table>
        <tr><td>160m</td><td><i class="fa fa-circle" style="color:#FFFF00"></i></td></tr>
        <tr><td>80m</td><td><i class="fa fa-circle" style="color:#003300"></i></td></tr>
        <tr><td>40m</td><td><i class="fa fa-circle" style="color:#FFA500"></i></td></tr>
        <tr><td>30m</td><td><i class="fa fa-circle" style="color:#FF4500"></i></td></tr>
        <tr><td>20m</td><td><i class="fa fa-circle" style="color:#0000FF"></i></td></tr>
        <tr><td>17m</td><td><i class="fa fa-circle" style="color:#800080"></i></td></tr>
        <tr><td>15m</td><td><i class="fa fa-circle" style="color:#696969"></i></td></tr>
        <tr><td>12m</td><td><i class="fa fa-circle" style="color:#00FFFF"></i></td></tr>
        <tr><td>10m</td><td><i class="fa fa-circle" style="color:#FF00FF"></i></td></tr>
        <tr><td>6m</td><td><i class="fa fa-circle" style="color:#F5DEB3"></i></td></tr>
    </table>
    </div>
    '''
    m.get_root().html.add_child(folium.Element(legend_html))

    return m

def grid_square_to_latlon(grid_square):
    upper_alpha = "ABCDEFGHIJKLMNOPQR"
    digits = "0123456789"
    lower_alpha = "abcdefghijklmnopqrstuvwx"

    grid_square = grid_square.upper()

    lon = -180 + (upper_alpha.index(grid_square[0]) * 20) + (digits.index(grid_square[2]) * 2)
    lat = -90 + (upper_alpha.index(grid_square[1]) * 10) + (digits.index(grid_square[3]) * 1)

    if len(grid_square) == 6:
        lon += (lower_alpha.index(grid_square[4].lower()) + 0.5) / 12
        lat += (lower_alpha.index(grid_square[5].lower()) + 0.5) / 24

    return lat, lon

def process_pasted_data(pasted_data):
    lines = pasted_data.split('\n')
    lines = [line.strip() for line in lines if line.strip()]
    
    data = []
    for line in lines:
        parts = line.split()
        if len(parts) < 14:
            print(f"Skipping incomplete row: {line}")
            continue
        
        spotter = parts[0]
        dx = parts[1]
        distance = parts[2] + ' ' + parts[3]
        freq = parts[4]
        mode = parts[5]
        type_ = parts[6]
        snr = parts[7] + ' ' + parts[8]
        speed = parts[9] + ' ' + parts[10]
        time_str = parts[11] + ' ' + parts[12] + ' ' + parts[13]
        
        try:
            time = datetime.strptime(time_str, "%H%Mz %d %b")
        except ValueError:
            time = None
        
        if all([spotter, dx, distance, freq, mode, type_, snr, speed, time]):
            data.append([spotter, dx, distance, freq, mode, type_, snr, speed, time])
    
    df = pd.DataFrame(data, columns=['spotter', 'dx', 'distance', 'freq', 'mode', 'type', 'snr', 'speed', 'time'])
    
    df['snr'] = df['snr'].str.split().str[0].astype(float)
    df['freq'] = df['freq'].astype(float)
    
    if 'band' not in df.columns:
        df['band'] = df['freq'].apply(get_band)
    
    return df

def process_downloaded_data(filename):
    df = pd.read_csv(filename)
    df = df.rename(columns={'callsign': 'spotter', 'dx': 'dx', 'db': 'snr', 'freq': 'freq', 'band': 'band', 'date': 'time'})
    df['snr'] = pd.to_numeric(df['snr'], errors='coerce')
    df['freq'] = pd.to_numeric(df['freq'], errors='coerce')
    df['time'] = pd.to_datetime(df['time'])
    return df

def calculate_statistics(filtered_df, grid_square_coords, spotter_coords):
    spots = len(filtered_df)
    avg_snr = filtered_df['snr'].mean()
    max_snr = filtered_df['snr'].max()
    bands = filtered_df['band'].value_counts().to_dict()
    
    max_distance = 0
    if not filtered_df.empty:
        for _, row in filtered_df.iterrows():
            spotter = row['spotter']
            if spotter in
