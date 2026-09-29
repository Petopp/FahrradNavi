"""Siedlungsflächen (OSM landuse) als Raster – zum Bestimmen, welcher Anteil einer Kante in Bebauung liegt."""

from __future__ import annotations

import math

import numpy as np

E7 = 10_000_000


class UrbanRaster:
    """Bool-Raster über dem Kartengebiet; ``True`` = Siedlungsfläche."""

    def __init__(self, rings: list, bbox: tuple[float, float, float, float], max_cells: float = 40e6, min_cell_m: float = 20.0):
        from PIL import Image, ImageDraw

        min_lon, min_lat, max_lon, max_lat = bbox
        lat0 = (min_lat + max_lat) / 2
        self.kx = 111_320.0 * math.cos(math.radians(lat0))
        self.ky = 110_574.0
        w_m = (max_lon - min_lon) * self.kx
        h_m = (max_lat - min_lat) * self.ky
        self.cell = max(min_cell_m, math.sqrt(w_m * h_m / max_cells))
        self.lon0, self.lat1 = min_lon, max_lat
        self.nx = int(w_m / self.cell) + 2
        self.ny = int(h_m / self.cell) + 2
        img = Image.new("L", (self.nx, self.ny), 0)
        d = ImageDraw.Draw(img)

        def px(ring: np.ndarray) -> list[tuple[float, float]]:
            x = (ring[:, 0] - self.lon0) * self.kx / self.cell
            y = (self.lat1 - ring[:, 1]) * self.ky / self.cell
            return list(zip(x.tolist(), y.tolist()))

        for outer, _holes in rings:
            d.polygon(px(outer), fill=1)
        for _outer, holes in rings:  # Löcher (Parks, Felder) nach allen Außenringen ausstanzen
            for h in holes:
                d.polygon(px(h), fill=0)
        self.grid = np.asarray(img, dtype=bool)

    def contains(self, lat_e7: np.ndarray, lon_e7: np.ndarray) -> np.ndarray:
        x = ((lon_e7 / E7 - self.lon0) * self.kx / self.cell).astype(np.int64)
        y = ((self.lat1 - lat_e7 / E7) * self.ky / self.cell).astype(np.int64)
        ok = (x >= 0) & (x < self.nx) & (y >= 0) & (y < self.ny)
        out = np.zeros(len(lat_e7), dtype=bool)
        out[ok] = self.grid[y[ok], x[ok]]
        return out
