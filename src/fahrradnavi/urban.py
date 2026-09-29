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

        for ring in rings:
            d.polygon(px(ring[0]), fill=1)
        for ring in rings:  # Löcher (Parks, Felder) nach allen Außenringen ausstanzen
            for h in ring[1]:
                d.polygon(px(h), fill=0)
        self.grid = np.asarray(img, dtype=bool)

    def contains(self, lat_e7: np.ndarray, lon_e7: np.ndarray) -> np.ndarray:
        x = ((lon_e7 / E7 - self.lon0) * self.kx / self.cell).astype(np.int64)
        y = ((self.lat1 - lat_e7 / E7) * self.ky / self.cell).astype(np.int64)
        ok = (x >= 0) & (x < self.nx) & (y >= 0) & (y < self.ny)
        out = np.zeros(len(lat_e7), dtype=bool)
        out[ok] = self.grid[y[ok], x[ok]]
        return out


# ---------------------------------------------------------------------------
# Innenstadt-Wert ("Trubel")
# ---------------------------------------------------------------------------

# POI-Gewicht pro Hektar (geglättet), ab dem ein Ort als Innenstadt zu zählen beginnt bzw. voll gilt.
# Kalibriert an Starnberg: Kern (Hauptstraße/Seepromenade) ≈ 5-10, Tutzing 1,5, Percha 0,5, Söcking 0 (POI-Gewicht pro ha).
CENTER_DENSITY_LOW = 1.0
CENTER_DENSITY_HIGH = 4.5
RETAIL_WEIGHT = 0.3  # Einzelhandels-/Gewerbeflächen zählen zusätzlich (höchstens so stark)


class CenterField:
    """Innenstadt-Wert 0..1 je Ort: geglättete Dichte von Geschäften, Gastronomie, Fußgängerzonen (+ Handelsflächen)."""

    def __init__(self, pois: np.ndarray, bbox: tuple[float, float, float, float], retail_rings: list | None = None,
                 sigma_m: float = 120.0, cell_m: float = 25.0, max_cells: float = 40e6,
                 low: float = CENTER_DENSITY_LOW, high: float = CENTER_DENSITY_HIGH):
        from scipy.ndimage import gaussian_filter

        min_lon, min_lat, max_lon, max_lat = bbox
        lat0 = (min_lat + max_lat) / 2
        self.kx = 111_320.0 * math.cos(math.radians(lat0))
        self.ky = 110_574.0
        w_m, h_m = (max_lon - min_lon) * self.kx, (max_lat - min_lat) * self.ky
        self.cell = max(cell_m, math.sqrt(w_m * h_m / max_cells))
        self.lon0, self.lat1 = min_lon, max_lat
        self.nx, self.ny = int(w_m / self.cell) + 2, int(h_m / self.cell) + 2
        grid = np.zeros((self.ny, self.nx), dtype=np.float32)
        if len(pois):
            ix = ((pois[:, 0] - self.lon0) * self.kx / self.cell).astype(np.int64)
            iy = ((self.lat1 - pois[:, 1]) * self.ky / self.cell).astype(np.int64)
            ok = (ix >= 0) & (ix < self.nx) & (iy >= 0) & (iy < self.ny)
            np.add.at(grid, (iy[ok], ix[ok]), pois[ok, 2].astype(np.float32))
        per_ha = gaussian_filter(grid, sigma=sigma_m / self.cell, mode="constant") * (10_000.0 / self.cell**2)
        score = np.clip((per_ha - low) / (high - low), 0.0, 1.0)
        if retail_rings:
            mask = UrbanRaster(retail_rings, bbox, max_cells=max_cells, min_cell_m=self.cell)
            if mask.grid.shape == score.shape:
                soft = gaussian_filter(mask.grid.astype(np.float32), sigma=40.0 / self.cell)
                score = np.maximum(score, RETAIL_WEIGHT * soft)
        self.score = score.astype(np.float32)

    def sample(self, lat_e7: np.ndarray, lon_e7: np.ndarray) -> np.ndarray:
        x = ((lon_e7 / E7 - self.lon0) * self.kx / self.cell).astype(np.int64)
        y = ((self.lat1 - lat_e7 / E7) * self.ky / self.cell).astype(np.int64)
        ok = (x >= 0) & (x < self.nx) & (y >= 0) & (y < self.ny)
        out = np.zeros(len(lat_e7), dtype=np.float32)
        out[ok] = self.score[y[ok], x[ok]]
        return out
