"""Nutzer-Overlays für die Routenwahl: Bereiche zum Meiden und Lieblingswege.

Beides wirkt als Kostenfaktor je Kante (wie die übrigen Regler) und ist damit *weich*: Ein gemiedener Bereich wird nur
betreten, wenn es keinen zumutbaren Umweg gibt (z. B. Start oder Ziel liegen darin); ein Lieblingsweg wird bevorzugt,
aber nicht um jeden Preis befahren.

* ``AvoidArea``  – Kreis (Mittelpunkt + Radius) oder Polygon; Stärke 0..1 (1 = Faktor 50, 0,5 = ~7).
* ``Favorite``   – Linie (z. B. aus einer GPX-Datei); Kanten im Abstand von ≤ 12 m bekommen einen Bonus
                   (bei Stärke 1: Kosten × 0,3).

Der Anteil einer Kante, der im Bereich liegt bzw. am Lieblingsweg verläuft, wird über die Segmente der Kantengeometrie
bestimmt; dadurch wirken Bereiche auch auf lange Kanten mit wenigen Stützpunkten korrekt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

from .graph import E7, Graph

AVOID_MAX_FACTOR = 50.0  # Kostenfaktor bei Stärke 1
FAVORITE_MIN_FACTOR = 0.3  # Kostenfaktor bei Stärke 1 und voller Übereinstimmung
FAVORITE_RADIUS_M = 12.0
MAX_AREAS = 20
MAX_POLYGON_POINTS = 300
MAX_RADIUS_M = 50_000.0
MAX_FAVORITES = 10
MAX_FAVORITE_POINTS = 20_000


@dataclass(frozen=True)
class AvoidArea:
    kind: str  # "circle" | "polygon"
    strength: float = 1.0
    lat: float = 0.0
    lon: float = 0.0
    radius_m: float = 0.0
    polygon: tuple[tuple[float, float], ...] = ()  # (lat, lon)

    def __post_init__(self) -> None:
        if self.kind == "circle":
            if not 0 < self.radius_m <= MAX_RADIUS_M:
                raise ValueError(f"Radius muss zwischen 0 und {MAX_RADIUS_M:.0f} m liegen")
        elif self.kind == "polygon":
            if not 3 <= len(self.polygon) <= MAX_POLYGON_POINTS:
                raise ValueError(f"Ein Polygon braucht 3 bis {MAX_POLYGON_POINTS} Punkte")
        else:
            raise ValueError(f"Unbekannte Bereichsart {self.kind!r}")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("Stärke muss zwischen 0 und 1 liegen")


@dataclass(frozen=True)
class Favorite:
    coords: tuple[tuple[float, float], ...]  # (lat, lon)
    strength: float = 1.0

    def __post_init__(self) -> None:
        if len(self.coords) < 2:
            raise ValueError("Ein Lieblingsweg braucht mindestens 2 Punkte")
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError("Stärke muss zwischen 0 und 1 liegen")


@dataclass(frozen=True)
class Overlays:
    avoid: tuple[AvoidArea, ...] = ()
    favorites: tuple[Favorite, ...] = field(default=())

    def __post_init__(self) -> None:
        if len(self.avoid) > MAX_AREAS:
            raise ValueError(f"Höchstens {MAX_AREAS} Bereiche")
        if len(self.favorites) > MAX_FAVORITES:
            raise ValueError(f"Höchstens {MAX_FAVORITES} Lieblingswege")
        if sum(len(f.coords) for f in self.favorites) > MAX_FAVORITE_POINTS:
            raise ValueError(f"Lieblingswege dürfen zusammen höchstens {MAX_FAVORITE_POINTS} Punkte haben")

    def empty(self) -> bool:
        return not self.avoid and not self.favorites


def points_in_polygon(x: np.ndarray, y: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Vektorisierter Ray-Casting-Test. poly: (M, 2) in denselben Einheiten wie x/y."""
    inside = np.zeros(len(x), dtype=bool)
    px, py = poly[:, 0], poly[:, 1]
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi, xj, yj = px[i], py[i], px[j], py[j]
        cond = (yi > y) != (yj > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xcross = (xj - xi) * (y - yi) / (yj - yi) + xi
        inside ^= cond & (x < xcross)
        j = i
    return inside


class SegmentIndex:
    """Segmente (Vertex i → i+1) aller Kanten mit Mittelpunkt in lokalen Metern; einmal je Graph aufgebaut."""

    def __init__(self, g: Graph):
        E = g.n_edges
        vs, ve = g.e_vs.astype(np.int64), g.e_ve.astype(np.int64)
        counts = ve - vs
        total = int(counts.sum())
        starts = np.cumsum(counts) - counts
        self.seg = np.repeat(vs, counts) + (np.arange(total, dtype=np.int64) - np.repeat(starts, counts))  # Vertex i
        self.edge = np.repeat(np.arange(E, dtype=np.int64), counts)
        min_lon, min_lat, max_lon, max_lat = g.bbox()
        self.lat0 = (min_lat + max_lat) / 2
        self.lon0 = min_lon
        self.kx = 111_320.0 * math.cos(math.radians(self.lat0))
        self.ky = 110_574.0
        la0, la1 = g.v_lat[self.seg] / E7, g.v_lat[self.seg + 1] / E7
        lo0, lo1 = g.v_lon[self.seg] / E7, g.v_lon[self.seg + 1] / E7
        self.x = ((lo0 + lo1) / 2 - self.lon0) * self.kx
        self.y = ((la0 + la1) / 2 - self.lat0) * self.ky
        self.len = np.hypot((lo1 - lo0) * self.kx, (la1 - la0) * self.ky)
        self.edge_len = np.bincount(self.edge, weights=self.len, minlength=E)
        self.n_edges = E

    def to_xy(self, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return (np.asarray(lon) - self.lon0) * self.kx, (np.asarray(lat) - self.lat0) * self.ky

    def fraction(self, mask: np.ndarray) -> np.ndarray:
        """Anteil (0..1) der Kantenlänge, dessen Segmente in ``mask`` liegen."""
        tot = np.bincount(self.edge[mask], weights=self.len[mask], minlength=self.n_edges)
        return np.clip(tot / np.maximum(self.edge_len, 1e-6), 0.0, 1.0)


def _densify(xy: np.ndarray, step: float = 5.0) -> np.ndarray:
    out = [xy[:1]]
    for a, b in zip(xy[:-1], xy[1:]):
        d = float(np.hypot(*(b - a)))
        n = max(1, int(math.ceil(d / step)))
        t = (np.arange(1, n + 1) / n)[:, None]
        out.append(a + t * (b - a))
    return np.vstack(out)


def edge_multipliers(idx: SegmentIndex, overlays: Overlays) -> np.ndarray:
    """Kostenfaktor je Kante (Länge E): > 1 in gemiedenen Bereichen, < 1 an Lieblingswegen."""
    mult = np.ones(idx.n_edges, dtype=np.float64)
    for a in overlays.avoid:
        if a.strength <= 0:
            continue
        if a.kind == "circle":
            cx, cy = idx.to_xy(np.array([a.lat]), np.array([a.lon]))
            near = (np.abs(idx.x - cx[0]) <= a.radius_m + 50) & (np.abs(idx.y - cy[0]) <= a.radius_m + 50)
            mask = np.zeros(len(idx.x), dtype=bool)
            mask[near] = np.hypot(idx.x[near] - cx[0], idx.y[near] - cy[0]) <= a.radius_m
        else:
            px, py = idx.to_xy(np.array([p[0] for p in a.polygon]), np.array([p[1] for p in a.polygon]))
            poly = np.column_stack([px, py])
            near = (idx.x >= px.min()) & (idx.x <= px.max()) & (idx.y >= py.min()) & (idx.y <= py.max())
            mask = np.zeros(len(idx.x), dtype=bool)
            mask[near] = points_in_polygon(idx.x[near], idx.y[near], poly)
        if mask.any():
            factor = AVOID_MAX_FACTOR**a.strength
            mult *= 1.0 + (factor - 1.0) * idx.fraction(mask)
    for f in overlays.favorites:
        if f.strength <= 0:
            continue
        fx, fy = idx.to_xy(np.array([c[0] for c in f.coords]), np.array([c[1] for c in f.coords]))
        line = _densify(np.column_stack([fx, fy]))
        pad = FAVORITE_RADIUS_M + 30
        near = (idx.x >= fx.min() - pad) & (idx.x <= fx.max() + pad) & (idx.y >= fy.min() - pad) & (idx.y <= fy.max() + pad)
        mask = np.zeros(len(idx.x), dtype=bool)
        if near.any():
            d, _ = cKDTree(line).query(np.column_stack([idx.x[near], idx.y[near]]), distance_upper_bound=FAVORITE_RADIUS_M)
            mask[near] = np.isfinite(d)
        if mask.any():
            mult *= 1.0 - (1.0 - FAVORITE_MIN_FACTOR) * f.strength * idx.fraction(mask)
    return mult
