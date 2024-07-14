import pandas as pd
import folium
import matplotlib.colors as mcolors
from gridtools import Grid
import streamlit as st
from datetime import datetime

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
        return 'Unknown'

def parse_pasted_data(pasted_data):
    lines = pasted_data.strip().split('\n')
    headers = lines[0].replace('● ', '').split()
    data = []
    for line in lines[1:]:
        columns = line.replace('● ', '').split()
        spotter = columns[0]
        spotted = columns[1]
        distance = columns[2].replace('mi', '')
        freq = float(columns[3])
        mode = columns[4]
        type_ = columns[5]
        snr = int(columns[6].replace('dB', ''))
        speed = columns[7]
        time = datetime.strptime(columns[8], '%H%Mz').time()
        seen = ' '.join(columns[9:])
        band = get_band(freq)
        data.append({
            'spotter': spotter,
            'spotted': spotted,
            'distance': distance,
            'freq': freq,
            'mode': mode,
            'type': type_,
            'snr': snr,
            'speed': speed,
            'time': time,
            'seen': seen,
            'band': band
        })
    return pd.DataFrame(data)

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
            band = row['band']
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
grid_square = st.text_input("Enter Grid Square:")
show_all_beacons = st.checkbox("Show all reverse beacons")
pasted_data = st.text_area("Paste your data here:")

if st.button("Generate Map"):
    try:
        filtered_df = parse_pasted_data(pasted_data)
        
        spotter_coords = {
            'OZ1AAB': (55.7, 12.6),
            'HA1VHF': (47.9, 19.2),
            'W6YX': (37.4, -122.2),
            'KV4TT': (36.0, -79.8),
            'W4AX': (34.2, -84.0)
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
            st.download_button(
                label="Download Map",
                data=file,
                file_name="RBN_signal_map_with_snr.html",
                mime="text/html"
            )
    except Exception as e:
        st.error(f"Error: {e}")
