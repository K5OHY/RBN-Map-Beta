# gridtools.py

class Grid:
    def __init__(self, grid_square):
        self.grid_square = grid_square
        self.lat, self.long = self._convert_grid_to_coords(grid_square)
    
    def _convert_grid_to_coords(self, grid_square):
        """
        Convert a grid square (Maidenhead Locator) to latitude and longitude.
        """
        grid_square = grid_square.strip().upper()
        if len(grid_square) < 4 or len(grid_square) % 2 != 0:
            raise ValueError("Grid square locator must be 4, 6, or 8 characters long.")
        
        # Longitude
        lon = -180 + (ord(grid_square[0]) - ord('A')) * 20
        lon += int(grid_square[2]) * 2
        if len(grid_square) >= 6:
            lon += (ord(grid_square[4]) - ord('A')) * 5 / 60
        if len(grid_square) == 8:
            lon += int(grid_square[6]) * 5 / 600
        lon += 1  # Center of the grid square
        
        # Latitude
        lat = -90 + (ord(grid_square[1]) - ord('A')) * 10
        lat += int(grid_square[3])
        if len(grid_square) >= 6:
            lat += (ord(grid_square[5]) - ord('A')) * 2.5 / 60
        if len(grid_square) == 8:
            lat += int(grid_square[7]) * 2.5 / 600
        lat += 0.5  # Center of the grid square
        
        return lat, lon
