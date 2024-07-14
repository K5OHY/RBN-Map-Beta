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
    from io import StringIO
    import pandas as pd

    data = StringIO(pasted_data)
    df = pd.read_csv(data, sep=r'\s+', engine='python', skiprows=1)
    df.columns = ['spotter', 'spotted', 'distance', 'freq', 'mode', 'type', 'snr', 'speed', 'time', 'seen']
    df['snr'] = df['snr'].str.replace(' dB', '').astype(float)
    return df

def get_color(snr):
    color_map = mcolors.LinearSegmentedColormap.from_list('custom', ['green', 'yellow', 'red'])
    return mcolors.to_hex(color_map(snr / 30))

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
            band = row['freq']
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

option = st.radio("Choose data input method:", ("Download from RBN", "Paste data"))

callsign = st.text_input("Enter Callsign:")
grid_square = st.text_input("Enter Grid Square:")
show_all_beacons = st.checkbox("Show all reverse beacons")

if option == "Download from RBN":
    date = st.text_input("Enter the date (YYYYMMDD):")
    if st.button("Generate Map"):
        try:
            csv_filename = download_and_extract_rbn_data(date)
            df = pd.read_csv(csv_filename)
            os.remove(csv_filename)

            filtered_df = df[df['dx'] == callsign].copy()
            filtered_df['snr'] = pd.to_numeric(filtered_df['db'], errors='coerce')
            
            spotter_coords = {
                'WA7LNW': (40.8, -111.9),
                'W3OA': (35.2, -78.7),
                'VE3EID': (43.7, -79.4),
                'VE6JY': (53.5, -113.5),
                'W6YX': (37.4, -122.2)
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

elif option == "Paste data":
    pasted_data = st.text_area("Paste your data here:")
    if st.button("Generate Map"):
        try:
            df = process_pasted_data(pasted_data)
            filtered_df = df[df['spotted'] == callsign].copy()

            spotter_coords = {
                'WA7LNW': (40.8, -111.9),
                'W3OA': (35.2, -78.7),
                'VE3EID': (43.7, -79.4),
                'VE6JY': (53.5, -113.5),
                'W6YX': (37.4, -122.2)
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
