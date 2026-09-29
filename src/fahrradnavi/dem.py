"""Höhenmodell aus SRTM-.hgt-Kacheln (1°×1°, 3601² oder 1201² Werte, big-endian int16)."""

from __future__ import annotations

import gzip
import math
import os

import numpy as np

VOID = -32768


def tile_name(lat_floor: int, lon_floor: int) -> str:
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"{ns}{abs(lat_floor):02d}{ew}{abs(lon_floor):03d}"


def tiles_for_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[str]:
    return [
        tile_name(la, lo)
        for la in range(math.floor(min_lat), math.floor(max_lat) + 1)
        for lo in range(math.floor(min_lon), math.floor(max_lon) + 1)
    ]


class Dem:
    def __init__(self, directory: str):
        self.directory = directory
        self._tiles: dict[tuple[int, int], np.ndarray | None] = {}

    def _load(self, la: int, lo: int) -> np.ndarray | None:
        key = (la, lo)
        if key in self._tiles:
            return self._tiles[key]
        base = os.path.join(self.directory, tile_name(la, lo))
        arr = None
        for path, opener in ((base + ".hgt", open), (base + ".hgt.gz", gzip.open)):
            if os.path.exists(path):
                with opener(path, "rb") as f:
                    raw = f.read()
                n = int(round(math.sqrt(len(raw) / 2)))
                if n * n * 2 != len(raw):
                    raise ValueError(f"{path}: unerwartete Dateigröße {len(raw)}")
                arr = np.frombuffer(raw, dtype=">i2").reshape(n, n).astype(np.float32)
                arr[arr == VOID] = np.nan
                break
        self._tiles[key] = arr
        return arr

    def available(self) -> bool:
        return any(
            f.endswith(".hgt") or f.endswith(".hgt.gz") for f in os.listdir(self.directory)
        ) if os.path.isdir(self.directory) else False

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Bilinear interpolierte Höhe in Metern; NaN, wo keine Daten vorliegen."""
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        out = np.full(lat.shape, np.nan, dtype=np.float32)
        if lat.size == 0:
            return out
        la_f = np.floor(lat).astype(np.int64)
        lo_f = np.floor(lon).astype(np.int64)
        tile_key = la_f * 1000 + lo_f + 500  # eindeutig für Bayern-große Bereiche
        for key in np.unique(tile_key):
            sel = np.flatnonzero(tile_key == key)
            la, lo = int(la_f[sel[0]]), int(lo_f[sel[0]])
            t = self._load(la, lo)
            if t is None:
                continue
            n = t.shape[0]
            # Zeile 0 = Nordrand (la+1), Spalte 0 = Westrand
            y = (1.0 - (lat[sel] - la)) * (n - 1)
            x = (lon[sel] - lo) * (n - 1)
            y0 = np.clip(np.floor(y).astype(np.int64), 0, n - 2)
            x0 = np.clip(np.floor(x).astype(np.int64), 0, n - 2)
            fy = (y - y0).astype(np.float32)
            fx = (x - x0).astype(np.float32)
            a = t[y0, x0]
            b = t[y0, x0 + 1]
            c = t[y0 + 1, x0]
            d = t[y0 + 1, x0 + 1]
            out[sel] = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy
        return out
