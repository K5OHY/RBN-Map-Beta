import requests
import xml.etree.ElementTree as ET

def get_grid_square(callsign):
    url = f'https://xmldata.qrz.com/xml/current/?s=YOUR_API_SESSION_KEY;callsign={callsign}'
    response = requests.get(url)
    if response.status_code == 200:
        root = ET.fromstring(response.content)
        grid_square = root.find('Callsign/grid').text
        return grid_square
    else:
        raise Exception("Error looking up callsign information")

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
            if not grid_square:
                grid_square = get_grid_square(callsign)
                st.write(f"Retrieved Grid Square: {grid_square}")
            if data_source == 'Paste RBN data' and pasted_data.strip():
                df = process_pasted_data(pasted_data)
                st.write("Using pasted data.")
            elif data_source == 'Download RBN data by date' and date.strip():
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
