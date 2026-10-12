"""Builds world_land.npz, the land/ocean grid the azimuthal map is drawn from. Run once by hand; the app only reads the result.

Source: Natural Earth 1:50m land outlines (public domain), https://www.naturalearthdata.com/
    curl -L -o ne_50m_land.geojson https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_land.geojson
    python build_world_mask.py ne_50m_land.geojson

Every grid cell is land or ocean by an even-odd scanline fill of the outlines, then the grid is bit-packed and compressed.
"""
import json
import sys

import numpy as np

RES = 0.1  # degrees per cell: 3600 x 1800 cells


def build(path, res=RES):
    width, height = int(round(360 / res)), int(round(180 / res))
    edges = []
    for feature in json.load(open(path, encoding="utf-8"))["features"]:
        geom = feature["geometry"]
        polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for polygon in polygons:
            for ring in polygon:  # exterior first, then any holes: even-odd takes care of both
                pts = np.asarray(ring, dtype=float)[:, :2]
                edges.append(np.hstack([pts[:-1], pts[1:]]))
    e = np.vstack(edges)
    x1, y1, x2, y2 = e[:, 0], e[:, 1], e[:, 2], e[:, 3]
    keep = y1 != y2
    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    ymin, ymax = np.minimum(y1, y2), np.maximum(y1, y2)
    j_lo = np.floor((90 - ymax) / res - 0.5).astype(int) + 1  # first row whose centre latitude is below ymax
    j_hi = np.floor((90 - ymin) / res - 0.5).astype(int)      # last row whose centre latitude is at or above ymin
    count = np.clip(j_hi - j_lo + 1, 0, None)
    idx = np.repeat(np.arange(len(x1)), count)
    j = np.concatenate([np.arange(lo, lo + n) for lo, n in zip(j_lo, count) if n > 0])
    lat = 90 - (j + 0.5) * res
    lon = x1[idx] + (lat - y1[idx]) * (x2[idx] - x1[idx]) / (y2[idx] - y1[idx])
    col = np.ceil((lon + 180) / res - 0.5).astype(int)  # first cell whose centre is east of the crossing
    ok = (j >= 0) & (j < height) & (col >= 0) & (col < width)
    toggles = np.zeros((height, width), dtype=np.uint8)
    np.bitwise_xor.at(toggles, (j[ok], col[ok]), 1)
    land = (np.cumsum(toggles, axis=1, dtype=np.uint32) % 2).astype(bool)
    return land, res


if __name__ == "__main__":
    land, res = build(sys.argv[1])
    np.savez_compressed("world_land.npz", bits=np.packbits(land), shape=np.array(land.shape), res=np.array(res))
    print(f"land fraction {land.mean():.1%} (the Earth is about 29% land); grid {land.shape[1]} x {land.shape[0]}")
