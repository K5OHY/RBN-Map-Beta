import pandas as pd

# Function to convert grid square to latitude and longitude
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

# Read the existing spotter CSV
existing_spotters_df = pd.read_csv('existing_spotters.csv')

# Read the new spotter information
new_spotters_df = pd.read_csv('new_spotters.csv')

# Convert grid squares to latitude and longitude in the new data
new_spotters_df['latitude'], new_spotters_df['longitude'] = zip(*new_spotters_df['grid'].map(grid_square_to_latlon))

# Merge the dataframes, updating the existing lat/lon with new data
updated_spotters_df = existing_spotters_df.set_index('callsign').combine_first(new_spotters_df.set_index('callsign')).reset_index()

# Save the updated DataFrame to a new CSV file
updated_spotters_df.to_csv('updated_spotters.csv', index=False)
