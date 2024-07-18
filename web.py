import requests
import pandas as pd
import folium
import matplotlib.colors as mcolors
import zipfile
import os
from io import BytesIO
import streamlit as st
from datetime import datetime, timedelta, timezone
from geopy.distance import geodesic

DEFAULT_GRID_SQUARE = "DM81wx"  # Default grid square location

def download_and_extract_rbn_data(date):
    """
    Download and extract RBN data for a specific date.

    Parameters:
    date (str): Date in the format YYYYMMDD

    Returns:
    str: Filename of the extracted CSV file
    """
    url = f'https://data.reversebeacon.net/rbn_history/{date}.zip'
    response = requests.get(url)
    if response.status_code == 200:
        with zipfile.ZipFile(BytesIO(response.content)) as z:
            for file_info in z.infolist():
                if file_info.filename.endswith('.csv'):
                    z.extract(file_info.filename)
                    return file_info.filename
    else:
        raise Exception(f"Error downloading RBN data: {response.status_code}")

def get_color(snr):
    """
    Get color based on SNR value.

    Parameters:
    snr (float): Signal-to-Noise Ratio

    Returns:
    str: Hex color code
    """
    color_map = mcolors.LinearSegmentedColormap.from_list('custom', ['green', 'yellow', 'red'])
    return mcolors.to_hex(color_map(snr / 30))

def get_band(freq):
    """
    Determine the band based on frequency.

    Parameters:
    freq (float): Frequency

    Returns:
    str: Band name
    """
    try:
        freq = float(freq)
    except ValueError:
        return 'unknown'
    
    bands = {
        (1800, 2000): '160m',
        (3500, 4000): '80m',
        (7000, 7300): '40m',
        (10100, 10150): '30m',
        (14000, 14350): '20m',
        (18068, 18168): '17m',
        (21000, 21450): '15m',
        (24890, 24990): '12m',
        (28000, 29700): '10m',
        (50000, 54000): '6m'
    }
    
    for (low, high), band in bands.items():
        if low <= freq <= high:
            return band
    return 'unknown'

def create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column, callsign, stats):
    """
    Create a Folium map with filtered data.

    Parameters:
    filtered_df (pd.DataFrame): Filtered DataFrame with RBN data
    spotter_coords (dict): Dictionary of spotter coordinates
    grid_square_coords (tuple): Coordinates of the grid square
    show_all_beacons (bool): Flag to show all beacons
    grid_square (str): Grid square location
    use_band_column (bool): Flag to use band column in DataFrame
    callsign (str): Callsign of the user
    stats (dict): Statistics of the filtered data

    Returns:
    folium.Map: Generated map
    """
    m = folium.Map(location=[39.8283, -98.5795], zoom_start=4)

    if show_all_beacons:
        for coords in spotter_coords.values():
            folium.CircleMarker(location=coords, radius=1, color='black', fill=True, fill_color='black').add_to(m)

    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            snr = row['snr']
            time = row['time']
            time = time.split()[1][:5] if ' ' in time else time
            folium.CircleMarker(
                location=coords,
                radius=snr / 2,
                popup=f'Spotter: {spotter}<br>SNR: {snr} dB<br>Time: {time}',
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
        '160m': '#FFFF00', '80m': '#003300', '40m': '#FFA500',
        '30m': '#FF4500', '20m': '#0000FF', '17m': '#800080',
        '15m': '#696969', '12m': '#00FFFF', '10m': '#FF00FF', '6m': '#F5DEB3'
    }

    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            band = row['band'] if use_band_column else get_band(row['freq'])
            color = band_colors.get(band, 'blue')
            folium.PolyLine(locations=[grid_square_coords, coords], color=color, weight=1).add_to(m)

    band_stats = "<br>".join([f"{band}: {count}" for band, count in stats['bands'].items()])
    stats_html = f'''
        <div style="position: fixed; top: 20px; right: 20px; width: 150px; height: auto; 
        border:1px solid grey; z-index:9999; font-size:10px; background-color:white; padding: 10px;">
        <b>Callsign: {callsign}</b><br>
        Total Spots: {stats['spots']}<br>
        Max Distance: {stats['max_distance']:.2f} mi<br>
        Max SNR: {stats['max_snr']} dB<br>
        Average SNR: {stats['avg_snr']:.2f} dB<br>
        <b>Bands:</b><br>{band_stats}
        </div>
    '''
    m.get_root().html.add_child(folium.Element(stats_html))

    legend_html = '''
        <div style="position: fixed; bottom: 20px; left: 20px; width: 80px; height: auto; 
        border:1px solid grey; z-index:9999; font-size:10px; background-color:white; padding: 5px;">
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
    """
    Convert grid square to latitude and longitude.

    Parameters:
    grid_square (str): Grid square

    Returns:
    tuple: Latitude and longitude
    """
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
    """
    Process pasted RBN data.

    Parameters:
    pasted_data (str): Pasted RBN data

    Returns:
    pd.DataFrame: Processed DataFrame
    """
    lines = pasted_data.split('\n')
    data = []
    for line in lines:
        parts = line.strip().split()
        if len(parts) < 14:
            continue
        data.append(parts[:14])

    columns = ['spotter', 'dx', 'distance', 'freq', 'mode', 'type', 'snr', 'speed', 'time', 'seen']
    df = pd.DataFrame(data, columns=columns[:len(data[0])])
    df['snr'] = df['snr'].str.split().str[0].astype(float)
    df['freq'] = df['freq'].astype(float)
    df['band'] = df['freq'].apply(get_band)
    return df

def process_downloaded_data(filename):
    """
    Process downloaded RBN data.

    Parameters:
    filename (str): Filename of the downloaded data

    Returns:
    pd.DataFrame: Processed DataFrame
    """
    df = pd.read_csv(filename)
    df = df.rename(columns={'callsign': 'spotter', 'dx': 'dx', 'db': 'snr', 'freq': 'freq', 'band': 'band', 'date': 'time'})
    df['snr'] = pd.to_numeric(df['snr'], errors='coerce')
    df['freq'] = pd.to_numeric(df['freq'], errors='coerce')
    return df

def calculate_statistics(filtered_df, grid_square_coords, spotter_coords):
    """
    Calculate statistics for the filtered data.

    Parameters:
    filtered_df (pd.DataFrame): Filtered DataFrame with RBN data
    grid_square_coords (tuple): Coordinates of the grid square
    spotter_coords (dict): Dictionary of spotter coordinates

    Returns:
    dict: Statistics of the filtered data
    """
    spots = len(filtered_df)
    avg_snr = filtered_df['snr'].mean()
    max_snr = filtered_df['snr'].max()
    bands = filtered_df['band'].value_counts().to_dict()
    
    max_distance = 0
    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            distance = geodesic(grid_square_coords, coords).miles
            max_distance = max(max_distance, distance)

    return {
        'spots': spots,
        'avg_snr': avg_snr,
        'max_distance': max_distance,
        'max_snr': max_snr,
        'bands': bands
    }

def main():
    """
    Main function to run the Streamlit app.
    """
    st.set_page_config(layout="wide", page_title="RBN Signal Mapper", page_icon=":radio:")
    st.markdown("<h1 style='text-align: center;'>RBN Signal Mapper</h1>", unsafe_allow_html=True)

    if 'map_html' not in st.session_state:
        st.session_state.map_html = None
    if 'filtered_df' not in st.session_state:
        st.session_state.filtered_df = None

    with st.sidebar:
        st.header("Input Data")
        callsign = st.text_input("Enter Callsign:")
        grid_square = st.text_input("Enter Grid Square (optional):")
        show_all_beacons = st.checkbox("Show all reverse beacons")
        data_source = st.radio("Select data source", ('Paste RBN data', 'Download RBN data by date'))

        if data_source == 'Paste RBN data':
            pasted_data = st.text_area("Paste RBN data here:")
        else:
            date = st.text_input("Enter the date (YYYYMMDD):")

        generate_map = st.button("Generate Map")

        band_colors = {
            '160m': '#FFFF00', '80m': '#003300', '40m': '#FFA500',
            '30m': '#FF4500', '20m': '#0000FF', '17m': '#800080',
            '15m': '#696969', '12m': '#00FFFF', '10m': '#FF00FF', '6m': '#F5DEB3'
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
                    st.error("Please provide the necessary data.")
                    return

                if data_source == 'Paste RBN data' and pasted_data.strip():
                    df = process_pasted_data(pasted_data)
                    st.write("Using pasted data.")
                    file_date = datetime.now(timezone.utc).strftime("%Y%m%d")
                elif data_source == 'Download RBN data by date':
                    if not date.strip():
                        date = (datetime.now(timezone.utc) - timedelta(1)).strftime('%Y%m%d')
                        st.write(f"Using latest available date: {date}")
                    csv_filename = download_and_extract_rbn_data(date)
                    df = process_downloaded_data(csv_filename)
                    os.remove(csv_filename)
                    use_band_column = True
                    file_date = date
                    st.write("Using downloaded data.")
                else:
                    st.error("Please provide the necessary data.")
                    return

                filtered_df = df[df['dx'] == callsign].copy()
                st.session_state.filtered_df = filtered_df.copy()

                spotter_coords_df = pd.read_csv('spotter_coords.csv')
                spotter_coords = {
                    row['callsign']: (row['latitude'], row['longitude']) for _, row in spotter_coords_df.iterrows()
                }

                grid_square_coords = grid_square_to_latlon(grid_square)
                stats = calculate_statistics(filtered_df, grid_square_coords, spotter_coords)

                m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column, callsign, stats)
                st.session_state.map_html = m._repr_html_()
                st.session_state.file_date = file_date
                st.write("Map generated successfully!")
        except Exception as e:
            st.error(f"Error: {e}")

    elif st.session_state.filtered_df is not None:
        try:
            with st.spinner("Filtering data..."):
                filtered_df = st.session_state.filtered_df.copy()
                if selected_band != 'All':
                    filtered_df = filtered_df[filtered_df['band'] == selected_band]

                spotter_coords_df = pd.read_csv('spotter_coords.csv')
                spotter_coords = {
                    row['callsign']: (row['latitude'], row['longitude']) for _, row in spotter_coords_df.iterrows()
                }

                grid_square_coords = grid_square_to_latlon(grid_square)
                stats = calculate_statistics(filtered_df, grid_square_coords, spotter_coords)

                m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, True, callsign, stats)
                st.session_state.map_html = m._repr_html_()
                st.write("Data filtered successfully!")
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
