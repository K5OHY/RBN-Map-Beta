def main():
    st.set_page_config(layout="wide", page_title="RBN Signal Mapper", page_icon=":radio:")

    # Center the title
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

        # Adding the time slider
        st.subheader("Filter by UTC Time")
        start_time, end_time = st.slider(
            "Select time range",
            value=(datetime.strptime("00:00", "%H:%M"), datetime.strptime("23:59", "%H:%M")),
            format="HH:mm"
        )

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
                st.session_state.filtered_df = filtered_df.copy()  # Store the filtered dataframe in session state

                # Convert time to datetime and filter by the selected time range
                filtered_df['time'] = pd.to_datetime(filtered_df['time'], format="%H:%M")
                start_time = datetime.combine(datetime.today(), start_time.time())
                end_time = datetime.combine(datetime.today(), end_time.time())
                filtered_df = filtered_df[(filtered_df['time'] >= start_time) & (filtered_df['time'] <= end_time)]

                spotter_coords_df = pd.read_csv('spotter_coords.csv')
                spotter_coords = {
                    row['callsign']: (row['latitude'], row['longitude']) for _, row in spotter_coords_df.iterrows()
                }

                if grid_square:
                    grid_square_coords = grid_square_to_latlon(grid_square)
                else:
                    grid_square_coords = grid_square_to_latlon(DEFAULT_GRID_SQUARE)

                if selected_band != 'All':
                    filtered_df = filtered_df[filtered_df['band'] == selected_band]

                stats = calculate_statistics(filtered_df, grid_square_coords, spotter_coords)

                m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column, callsign, stats)
                map_html = m._repr_html_()
                st.session_state.map_html = map_html
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

                # Convert time to datetime and filter by the selected time range
                filtered_df['time'] = pd.to_datetime(filtered_df['time'], format="%H:%M")
                start_time = datetime.combine(datetime.today(), start_time.time())
                end_time = datetime.combine(datetime.today(), end_time.time())
                filtered_df = filtered_df[(filtered_df['time'] >= start_time) & (filtered_df['time'] <= end_time)]

                spotter_coords_df = pd.read_csv('spotter_coords.csv')
                spotter_coords = {
                    row['callsign']: (row['latitude'], row['longitude']) for _, row in spotter_coords_df.iterrows()
                }

                if grid_square:
                    grid_square_coords = grid_square_to_latlon(grid_square)
                else:
                    grid_square_coords = grid_square_to_latlon(DEFAULT_GRID_SQUARE)

                stats = calculate_statistics(filtered_df, grid_square_coords, spotter_coords)

                m = create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, True, callsign, stats)
                map_html = m._repr_html_()
                st.session_state.map_html = map_html
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
