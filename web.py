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
    """Download and extract RBN data for a given date."""
    url = f'https://data.reversebeacon.net/rbn_history/{date}.zip'
    response = requests.get(url)
    if response.status_code == 200:
        with zipfile.ZipFile(BytesIO(response.content)) as z:
            for file_info in z.infolist():
                if file_info.filename.endswith('.csv'):
                    z.extract(file_info.filename)
                    return file_info.filename
            raise Exception("No CSV file found in the ZIP archive")
    else:
        raise Exception(f"Error downloading RBN data: {response.status_code}")

def get_color(snr):
    """Get color based on SNR value."""
    color_map = mcolors.LinearSegmentedColormap.from_list('custom', ['green', 'yellow', 'red'])
    return mcolors.to_hex(color_map(snr / 30))

def create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square):
    """Create a folium map with the given parameters."""
    m = folium.Map(location=[39.8283, -98.5795], zoom_start=4)

    if show_all_beacons:
        for coords in spotter_coords.values():
            folium.CircleMarker(
                location=coords,
                radius=1,  # Small black dot
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
        else:
            st.warning(f"Spotter {spotter} not found in coordinates list")

    folium.Marker(
        location=grid_square_coords,
        icon=folium.Icon(icon='star', color='red'),
        popup=f'Your Location: {grid_square}'
    ).add_to(m)
    
    band_colors = {
        '160m': 'blue',
        '80m': 'green',
        '40m': 'teal',
        '30m': 'purple',
        '20m': 'darkblue',
        '17m': 'orange',
        '15m': 'lime',
        '12m': 'pink',
        '10m': 'red',
        '6m': 'magenta'
    }

    for _, row in filtered_df.iterrows():
        spotter = row['spotter']
        if spotter in spotter_coords:
            coords = spotter_coords[spotter]
            band = row['band']
            color = band_colors.get(band, 'blue')
            folium.PolyLine(
                locations=[grid_square_coords, coords],
                color=color,
                weight=1
            ).add_to(m)
        else:
            st.warning(f"Spotter {spotter} not found in coordinates list for band {band}")

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

def parse_pasted_data(pasted_data):
    """Parse pasted data into a DataFrame."""
    lines = pasted_data.strip().split('\n')
    data = []
    for line in lines[1:]:  # Skip header
        parts = line.split()
        spotter = parts[0]
        dx = parts[1]
        distance = parts[2] + " " + parts[3]  # Combine distance and unit
        freq = parts[4]
        mode = parts[5]
        type_ = parts[6]
        snr = parts[7] + " " + parts[8]  # Combine SNR value and unit
        speed = parts[9] + " " + parts[10]  # Combine speed value and unit
        time = parts[11]
        date = " ".join(parts[12:15])  # Combine date with spaces
        seen = " ".join(parts[15:])  # Remaining parts for 'seen' field
        data.append([spotter, dx, distance, freq, mode, type_, snr, speed, time, date, seen])
    df = pd.DataFrame(data, columns=['spotter', 'dx', 'distance', 'freq', 'mode', 'type', 'snr', 'speed', 'time', 'date', 'seen'])
    return df

def main():
    st.title("RBN Signal Map Generator")

    callsign = st.text_input("Enter Callsign:")
    date = st.text_input("Enter the date (YYYYMMDD):")
    grid_square = st.text_input("Enter Grid Square:")
    show_all_beacons = st.checkbox("Show all reverse beacons")
    pasted_data = st.text_area("Paste RBN data here:")

    if st.button("Generate Map"):
        try:
            if pasted_data.strip():
                df = parse_pasted_data(pasted_data)
                st.write("Using pasted data.")
            else:
                csv_filename = download_and_extract_rbn_data(date)
                df = pd.read_csv(csv_filename)
                os.remove(csv_filename)
                st.write("Using downloaded data.")

            filtered_df = df[df['dx'] == callsign].copy()
            filtered_df['snr'] = pd.to_numeric(filtered_df['snr'].str.replace('dB', ''), errors='coerce')
            
            spotter_coords_df = pd.read_csv('spotter_coords.csv')
            spotter_coords = {
                row['callsign']: (row['latitude'], row['longitude']) for _, row in spotter_coords_df.iterrows()
            }
            
            grid = Grid(grid_square)
            grid_square_coords = (grid.lat, grid.long)
            
            m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square)
            m.save('map.html')
            st.write("Map generated successfully!")
            
            st.components.v1.html(open('map.html', 'r').read(), height=700)

            with open("map.html", "rb") as file:
                st.download_button(
                    label="Download Map",
                    data=file,
                    file_name="RBN_signal_map_with_snr.html",
                    mime="text/html"
                )
        except Exception as e:
            st.error(f"Error: {e}")

if __name__ == "__main__":
    main()
