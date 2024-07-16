import requests
import pandas as pd
import folium
import matplotlib.colors as mcolors
from gridtools import Grid
import zipfile
import os
from io import BytesIO
import streamlit as st
from datetime import datetime, timedelta, timezone

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

def create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column):
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
            folium.CircleMarker(
                location=coords,
                radius=snr / 2,
                popup=f'Spotter: {spotter}<br>SNR: {snr} dB',
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
    
    legend_html = '''
     <div style="position: fixed; 
     bottom: 20px; left: 20px; width: 120px; height: 180px; 
     border:1px solid grey; z-index:9999; font-size:10px;
     background-color:white;
     ">
     &nbsp; <b>Legend</b> <br>
     &nbsp; 160m &nbsp; <i class="fa fa-circle" style="color:#FFFF00"></i><br>
     &nbsp; 80m &nbsp; <i class="fa fa-circle" style="color:#003300"></i><br>
     &nbsp; 40m &nbsp; <i class="fa fa-circle" style="color:#FFA500"></i><br>
     &nbsp; 30m &nbsp; <i class="fa fa-circle" style="color:#FF4500"></i><br>
     &nbsp; 20m &nbsp; <i class="fa fa-circle" style="color:#0000FF"></i><br>
     &nbsp; 17m &nbsp; <i class="fa fa-circle" style="color:#800080"></i><br>
     &nbsp; 15m &nbsp; <i class="fa fa-circle" style="color:#696969"></i><br>
     &nbsp; 12m &nbsp; <i class="fa fa-circle" style="color:#00FFFF"></i><br>
     &nbsp; 10m &nbsp; <i class="fa fa-circle" style="color:#FF00FF"></i><br>
     &nbsp; 6m &nbsp; <i class="fa fa-circle" style="color:#F5DEB3"></i><br>
     </div>
     '''
    m.get_root().html.add_child(folium.Element(legend_html))

    return m

def process_pasted_data(pasted_data):
    lines = pasted_data.split('\n')
    lines = [line.strip() for line in lines if line.strip()]
    
    data = []
    for line in lines:
        parts = line.split()
        # Ensure there are enough parts in the line
        if len(parts) < 14:
            print(f"Skipping incomplete row: {line}")
            continue  # Skip incomplete rows
        
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
        
        # Only add rows with all required fields
        if all([spotter, dx, distance, freq, mode, type_, snr, speed, time]):
            data.append([spotter, dx, distance, freq, mode, type_, snr, speed, time, seen])
    
    df = pd.DataFrame(data, columns=['spotter', 'dx', 'distance', 'freq', 'mode', 'type', 'snr', 'speed', 'time', 'seen'])
    
    df['snr'] = df['snr'].str.split().str[0].astype(float)
    df['freq'] = df['freq'].astype(float)
    
    return df

def process_downloaded_data(filename):
    df = pd.read_csv(filename)
    df = df.rename(columns={'callsign': 'spotter', 'dx': 'dx', 'db': 'snr', 'freq': 'freq', 'band': 'band'})
    df['snr'] = pd.to_numeric(df['snr'], errors='coerce')
    df['freq'] = pd.to_numeric(df['freq'], errors='coerce')
    return df

def main():
    st.title("RBN Signal Mapper")

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
    
    if st.button("Generate Map"):
        try:
            use_band_column = False
            file_date = ""
            
            # Convert callsign to uppercase
            if callsign:
                callsign = callsign.upper()
                
            # Convert grid square to proper format
            if grid_square:
                grid_square = grid_square[:2].upper() + grid_square[2:]
            
            if not grid_square:
                st.warning(f"No grid square provided, using default: {DEFAULT_GRID_SQUARE}")
                grid_square = DEFAULT_GRID_SQUARE
            
            # Debugging information
            st.write(f"callsign: {callsign}")
            st.write(f"grid_square: {grid_square}")
            st.write(f"data_source: {data_source}")
            st.write(f"pasted_data: {pasted_data.strip() if data_source == 'Paste RBN data' else 'N/A'}")
            st.write(f"date: {date if data_source == 'Download RBN data by date' else 'N/A'}")
            
            if data_source == 'Paste RBN data' and pasted_data.strip():
                df = process_pasted_data(pasted_data)
                st.write("Using pasted data.")
            else:
                if data_source == 'Download RBN data by date' or not pasted_data.strip():
                    if data_source == 'Download RBN data by date' and (not date or not date.strip()):
                        # Calculate yesterday's date
                        yesterday = datetime.now(timezone.utc) - timedelta(1)
                        date = yesterday.strftime('%Y%m%d')
                        st.write(f"Using latest available date: {date}")
                    elif not date or not date.strip():
                        # Calculate yesterday's date
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
            
            spotter_coords_df = pd.read_csv('spotter_coords.csv')
            spotter_coords = {
                row['callsign']: (row['latitude'], row['longitude']) for _, row in spotter_coords_df.iterrows()
            }
            
            grid = Grid(grid_square)
            grid_square_coords = (grid.lat, grid.long)
            
            m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column)
            map_filename = f"RBN_signal_map_{file_date}.html" if file_date else "RBN_signal_map.html"
            m.save(map_filename)
            st.write("Map generated successfully!")
            
            st.components.v1.html(open(map_filename, 'r').read(), height=700)

            with open(map_filename, "rb") as file:
                st.download_button(
                    label="Download Map",
                    data=file,
                    file_name=map_filename,
                    mime="text/html"
                )
        except Exception as e:
            st.error(f"Error: {e}")

if __name__ == "__main__":
    main()
