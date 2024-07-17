import requests
import pandas as pd
import folium
import matplotlib.colors as mcolors
import zipfile
import os
import re
from io import BytesIO
import streamlit as st
from datetime import datetime, timedelta, timezone
from geopy.distance import geodesic
from folium.plugins import MarkerCluster, HeatMap

DEFAULT_GRID_SQUARE = "DM81wx"  # Default grid square location

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

def create_custom_cluster_icon(snr):
    color = get_color(snr)
    icon = folium.Icon(color=color, icon='circle')
    return icon

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

    marker_cluster = MarkerCluster(icon_create_function=lambda x: create_custom_cluster_icon(max([item[2] for item in x])).get_root().render()).add_to(m)

    cluster_data = {}

    for spotter, spot_df in filtered_df.groupby('spotter'):
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            max_snr = spot_df['snr'].max()
            cluster_data[spotter] = (coords, max_snr)
            for _, row in spot_df.iterrows():
                popup_text = f'Spotter: {spotter}<br>SNR: {row["snr"]} dB'
                folium.CircleMarker(
                    location=coords,
                    radius=row["snr"] / 2,
                    popup=popup_text,
                    color=get_color(row["snr"]),
                    fill=True,
                    fill_color=get_color(row["snr"])
                ).add_to(marker_cluster)

    for coords, max_snr in cluster_data.values():
        folium.CircleMarker(
            location=coords,
            radius=max_snr / 2,
            color=get_color(max_snr),
            fill=True,
            fill_color=get_color(max_snr),
            fill_opacity=0.7
        ).add_to(m)

    folium.Marker(
        location=grid_square_coords,
        icon=folium.Icon(icon='star', color='red'),
        popup=f'Your Location: {grid_square}'
    ).add_to(m)
    
    band_colors = {
        '160m': '#FFFF00',  # yellow
        '80m': '#003300',   # dark green
        '40m': '#FFA500',   # orange
        '30m': '#FF4500',   # red
        '20m': '#0000FF',   # blue
        '17m': '#800080',   # purple
        '15m': '#696969',   # dim gray
        '12m': '#00FFFF',   # cyan
        '10m': '#FF00FF',   # magenta
        '6m': '#F5DEB3',    # wheat
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
     <div style="position: fixed; 
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
     <div style="position: fixed; 
     bottom: 20px; left: 20px; width: 80px; height: auto; 
     border:1px solid grey; z-index:9999; font-size:10px;
     background-color:white;
     padding: 5px;
     ">
     <b>Legend</b><br>
     160m <i class="fa fa-circle" style="color:#FFFF00"></i><br>
     80m <i class="fa fa-circle" style="color:#003300"></i><br>
     40m <i class="fa fa-circle" style="color:#FFA500"></i><br>
     30m <i class="fa fa-circle" style="color:#FF4500"></i><br>
     20m <i class="fa fa-circle" style="color:#0000FF"></i><br>
     17m <i class="fa fa-circle" style="color:#800080"></i><br>
     15m <i class="fa fa-circle" style="color:#696969"></i><br>
     12m <i class="fa fa-circle" style="color:#00FFFF"></i><br>
     10m <i class="fa fa-circle" style="color:#FF00FF"></i><br>
     6m <i class="fa fa-circle" style="color:#F5DEB3"></i>
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
        time = parts[11] + ' ' + parts[12] + ' ' + parts[13]
        seen = ' '.join(parts[14:]) if len(parts) > 14 else ''
        
        if all([spotter, dx, distance, freq, mode, type_, snr, speed, time]):
            data.append([spotter, dx, distance, freq, mode, type_, snr, speed, time, seen])
    
    df = pd.DataFrame(data, columns=['spotter', 'dx', 'distance', 'freq', 'mode', 'type', 'snr', 'speed', 'time', 'seen'])
    
    df['snr'] = df['snr'].str.split().str[0].astype(float)
    df['freq'] = df['freq'].astype(float)
    
    if 'band' not in df.columns:
        df['band'] = df['freq'].apply(get_band)
    
    return df

def process_downloaded_data(filename):
    df = pd.read_csv(filename)
    df = df.rename(columns={'callsign': 'spotter', 'dx': 'dx', 'db': 'snr', 'freq': 'freq', 'band': 'band'})
    df['snr'] = pd.to_numeric(df['snr'], errors='coerce')
    df['freq'] = pd.to_numeric(df['freq'], errors='coerce')
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
            if spotter in spotter_coords:
                coords = spotter_coords[spotter]
                distance = geodesic(grid_square_coords, coords).miles
                if distance > max_distance:
                    max_distance = distance
    
    return {
        'spots': spots,
        'avg_snr': avg_snr,
        'max_distance': max_distance,
        'max_snr': max_snr,
        'bands': bands
    }

def create_heatmap(filtered_df, map_object, spotter_coords):
    heat_data = []
    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            heat_data.append([coords[0], coords[1], row['snr']])
    
    # Normalize SNR values to 0-1 range for better heatmap scaling
    snr_values = filtered_df['snr']
    min_snr = snr_values.min()
    max_snr = snr_values.max()
    normalized_heat_data = [[lat, lon, (snr - min_snr) / (max_snr - min_snr)] for lat, lon, snr in heat_data]
    
    HeatMap(
        data=normalized_heat_data,
        min_opacity=0.3,
        max_val=1,  # max_val is now 1 due to normalization
        radius=15,
        blur=10,
        gradient={0.0: 'green', 0.5: 'yellow', 1.0: 'red'}  # Adjusted gradient for better contrast
    ).add_to(map_object)

def main():
    st.set_page_config(layout="wide", page_title="RBN Signal Mapper", page_icon=":radio:")

    # Center the title
    st.markdown("<h1 style='text-align: center;'>RBN Signal Mapper</h1>", unsafe_allow_html=True)

    if 'map_html' not in st.session_state:
        st.session_state.map_html = None

    with st.sidebar:
        st.header("Input Data")
        callsign = st.text_input("Enter Callsign:")
        grid_square = st.text_input("Enter Grid Square (optional):")
        show_all_beacons = st.checkbox("Show all reverse beacons")
        data_source = st.radio(
            "Select data source",
            ('Paste RBN data', 'Download RBN data by date')
        )

        if data_source == 'Paste RBN data':
            pasted_data = st.text_area("Paste RBN data here:")
        else:
            date = st.text_input("Enter the date (YYYYMMDD):")

        generate_map = st.button("Generate Map")

        band_colors = {
            '160m': '#FFFF00',  # yellow
            '80m': '#003300',   # dark green
            '40m': '#FFA500',   # orange
            '30m': '#FF4500',   # red
            '20m': '#0000FF',   # blue
            '17m': '#800080',   # purple
            '15m': '#696969',   # dim gray
            '12m': '#00FFFF',   # cyan
            '10m': '#FF00FF',   # magenta
            '6m': '#F5DEB3',    # wheat
        }
        band_options = ['All'] + list(band_colors.keys())
        selected_band = st.selectbox('Select Band', band_options)

        with st.expander("Instructions", expanded=False):
            st.markdown("""
            **Instructions:**
            1. Enter a callsign and grid square.
            2. Select the data source:
                - Paste RBN data manually.
                - Download RBN data by date.
            3. Optionally, choose to show all reverse beacons.
            4. Click 'Generate Map' to visualize the signal map.
            5. You can download the generated map using the provided download button.
            """)

    if generate_map:
        try:
            with st.spinner("Generating map..."):
                use_band_column = False
                file_date = ""

                if callsign:
                    callsign = callsign.upper()

                if grid_square:
                    grid_square = grid_square[:2].upper() + grid_square[2:]

                if not grid_square:
                    st.warning(f"No grid square provided, using default: {DEFAULT_GRID_SQUARE}")
                    grid_square = DEFAULT_GRID_SQUARE

                if data_source == 'Paste RBN data' and not pasted_data.strip():
                    data_source = 'Download RBN data by date'
                    date = ""

                if data_source == 'Paste RBN data' and pasted_data.strip():
                    df = process_pasted_data(pasted_data)
                    st.write("Using pasted data.")
                    file_date = datetime.now(timezone.utc).strftime("%Y%m%d")
                elif data_source == 'Download RBN data by date':
                    if not date.strip():
                        yesterday = datetime.now(timezone.utc) - timedelta(1)
                        date = yesterday.strftime('%Y%m%d')
                        st.write(f"Using latest available date: {date}")
                    csv_filename = download_and_extract_rbn_data(date)
                    df = process_downloaded_data(csv_filename)
                    os.remove(csv_filename)
                    use_band_column = True
                    file_date = date
                    st.write("Using downloaded data.")
                else:
                    st.error("Please provide the necessary data.")

                filtered_df = df[df['dx'] == callsign].copy()
                if selected_band != 'All':
                    filtered_df = filtered_df[filtered_df['band'] == selected_band]

                spotter_coords_df = pd.read_csv('spotter_coords.csv')
                spotter_coords = {
                    row['callsign']: (row['latitude'], row['longitude']) for _, row in spotter_coords_df.iterrows()
                }

                if grid_square:
                    grid_square_coords = grid_square_to_latlon(grid_square)
                else:
                    grid_square_coords = grid_square_to_latlon(DEFAULT_GRID_SQUARE)

                stats = calculate_statistics(filtered_df, grid_square_coords, spotter_coords)

                m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column, callsign, stats)

                # Add heatmap
                if not filtered_df.empty:
                    create_heatmap(filtered_df, m, spotter_coords)

                map_html = m._repr_html_()
                st.session_state.map_html = map_html
                st.session_state.file_date = file_date
                st.write("Map generated successfully!")
        except Exception as e:
            st.error(f"Error: {e}")

    if st.session_state.map_html:
        st.components.v1.html(st.session_state.map_html, height=700)

        map_filename = f"RBN_signal_map_{st.session_state.file_date}.html"
        with open(map_filename, "w") as file:
            file.write(st.session_state.map_html)

        with open(map_filename, "rb") as file:
            st.download_button(
                label="Download Map",
                data=file,
                file_name=map_filename,
                mime="text/html"
            )

if __name__ == "__main__":
    main()
