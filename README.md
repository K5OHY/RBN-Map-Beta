
# RBN Signal Mapper

This application visualizes Reverse Beacon Network (RBN) signal data on an interactive map. It allows users to either paste RBN data or download it by a specified date, and then generates a map displaying signal spots with various features.

## Features
- Download and extract RBN data by date.
- Process pasted or downloaded RBN data.
- Visualize RBN spots on an interactive map using Folium.
- Display signal-to-noise ratio (SNR) and band information with color coding.
- Option to show all reverse beacons on the map.

## Installation

### Prerequisites
- Python 3.7+
- pip (Python package installer)

### Required Python Packages
- pandas
- folium
- matplotlib
- requests
- zipfile
- streamlit
- gridtools

You can install the required packages using the following command:
```bash
pip install pandas folium matplotlib requests streamlit
```

### Setting up the Environment
1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/rbn-signal-mapper.git
   cd rbn-signal-mapper
   ```

2. Create a virtual environment (optional but recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

### Running the Application
To start the Streamlit application, run the following command:
```bash
streamlit run app.py
```

### Application Instructions
1. Enter your callsign and grid square.
2. Select the data source:
   - Paste RBN data manually.
   - Download RBN data by date.
3. Optionally, choose to show all reverse beacons.
4. Click 'Generate Map' to visualize the signal map.
5. You can download the generated map using the provided download button.

## Code Overview

### Main Functions

#### `download_and_extract_rbn_data(date)`
Downloads and extracts RBN data for the specified date.

#### `get_color(snr)`
Returns a color hex code based on the SNR value.

#### `get_band(freq)`
Determines the band based on the frequency.

#### `create_map(filtered_df, spotter_coords, grid_square_coords, show_all_beacons, grid_square, use_band_column)`
Generates an interactive map with the filtered RBN data.

#### `process_pasted_data(pasted_data)`
Processes the manually pasted RBN data.

#### `process_downloaded_data(filename)`
Processes the downloaded RBN data file.

### Streamlit Application
The Streamlit application is defined in the `main()` function. It provides the user interface for entering the callsign, grid square, selecting the data source, and generating the map.

## License
This project is licensed under the MIT License.

## Acknowledgements
- [Reverse Beacon Network](https://reversebeacon.net/)
- [Folium](https://python-visualization.github.io/folium/)
- [Streamlit](https://streamlit.io/)

## Contributing
Contributions are welcome! Please open an issue or submit a pull request for any changes.
