"""Synthetisches OSM-Testnetz (kein echtes Kartenmaterial!).

Koordinaten liegen in der Nähe von Starnberg, damit Projektion/Höhen realistisch sind,
die Geometrie ist aber frei erfunden. 1 Einheit im lokalen Raster = 1 Meter (x nach Osten, y nach Norden).

Szenarien
---------
A  "Umweg"        S1 -> T1: 4 km direkt auf der Hauptstraße (primary) ODER 7 km über einen Radweg.
B  "Unvermeidbar" S2 -> T2: nur eine Brücke auf einer secondary-Straße verbindet die Ufer.
C  "Radweg an B"  S3 -> T3: primary mit baulich getrenntem Radweg (cycleway=track) ist erlaubt.
D  "Berg"         S4 -> T4: kurz + steiler Hügel  vs.  länger + flach.
E  "Belag"        S5 -> T5: kurz auf Schotter (gravel) vs. länger auf Asphalt.
F  "Einbahn"      S6 -> T6: Radweg nur in eine Richtung.
"""

from __future__ import annotations

import math
import os

import numpy as np

LAT0, LON0 = 47.95, 11.30  # Ursprung des lokalen Rasters


def to_ll(x: float, y: float) -> tuple[float, float]:
    lat = LAT0 + y / 110_574.0
    lon = LON0 + x / (111_320.0 * math.cos(math.radians(LAT0)))
    return lat, lon


class OsmBuilder:
    def __init__(self):
        self.nodes: dict[int, tuple[float, float, dict]] = {}
        self.ways: list[tuple[int, list[int], dict]] = []
        self.relations: list[tuple[int, list[int], dict]] = []
        self._nid = 1000
        self._wid = 5000
        self.by_xy: dict[tuple[float, float], int] = {}

    def node(self, x: float, y: float, tags: dict | None = None) -> int:
        key = (round(x, 3), round(y, 3))
        if key in self.by_xy:
            nid = self.by_xy[key]
            if tags:
                self.nodes[nid][2].update(tags)
            return nid
        self._nid += 1
        lat, lon = to_ll(x, y)
        self.nodes[self._nid] = (lat, lon, dict(tags or {}))
        self.by_xy[key] = self._nid
        return self._nid

    def way(self, pts: list[tuple[float, float]], tags: dict, step: float = 100.0) -> int:
        """Polylinie; wird alle ``step`` Meter mit Zwischenknoten versehen."""
        ids: list[int] = []
        for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
            d = math.hypot(x1 - x0, y1 - y0)
            n = max(1, int(round(d / step)))
            for i in range(n):
                ids.append(self.node(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n))
        ids.append(self.node(*pts[-1]))
        self._wid += 1
        self.ways.append((self._wid, ids, tags))
        return self._wid

    def write(self, path: str) -> None:
        out = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="fahrradnavi-tests">']
        for nid in sorted(self.nodes):
            lat, lon, tags = self.nodes[nid]
            if tags:
                out.append(f'<node id="{nid}" version="1" lat="{lat:.7f}" lon="{lon:.7f}">')
                out += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
                out.append("</node>")
            else:
                out.append(f'<node id="{nid}" version="1" lat="{lat:.7f}" lon="{lon:.7f}"/>')
        for wid, refs, tags in sorted(self.ways):
            out.append(f'<way id="{wid}" version="1">')
            out += [f'<nd ref="{r}"/>' for r in refs]
            out += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
            out.append("</way>")
        for rid, members, tags in self.relations:
            out.append(f'<relation id="{rid}" version="1">')
            out += [f'<member type="way" ref="{m}" role=""/>' for m in members]
            out += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
            out.append("</relation>")
        out.append("</osm>")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))


PRIMARY = {"highway": "primary", "name": "Hauptstraße", "surface": "asphalt", "maxspeed": "70"}
CYCLE = {"highway": "cycleway", "name": "Radweg", "surface": "asphalt"}
RESI = {"highway": "residential", "surface": "asphalt", "maxspeed": "30"}


def build_scenarios() -> OsmBuilder:
    b = OsmBuilder()

    # --- A: Umweg (Ursprung x=0..4000, y=0) ---------------------------------
    b.way([(0, 0), (100, 0)], {**RESI, "name": "Startstraße"})
    b.way([(100, 0), (3900, 0)], PRIMARY)
    b.way([(3900, 0), (4000, 0)], {**RESI, "name": "Zielstraße"})
    b.way([(0, 0), (0, 1500), (4000, 1500), (4000, 0)], CYCLE)  # 1500+4000+1500 = 7000 m

    # --- B: Unvermeidbar (y=-5000) --------------------------------------------
    b.way([(0, -5000), (1000, -5000)], {**RESI, "name": "Ufer West"})
    b.way([(1000, -5000), (1400, -5000)], {"highway": "secondary", "bridge": "yes", "name": "Brücke", "surface": "asphalt"})
    b.way([(1400, -5000), (2400, -5000)], {**RESI, "name": "Ufer Ost"})

    # --- C: Primary mit Radweg (y=-10000) ---------------------------------------
    b.way([(0, -10000), (3000, -10000)], {**PRIMARY, "cycleway:right": "track", "name": "Bundesstraße mit Radweg"})
    b.way([(0, -10000), (0, -11500), (3000, -11500), (3000, -10000)], CYCLE)  # 6 km Umweg als Versuchung

    # --- D: Berg (y=-15000, Hügel um x=1500) -------------------------------------
    b.way([(0, -15000), (3000, -15000)], {**CYCLE, "name": "Bergweg"})  # direkt, über den Hügel
    b.way([(0, -15000), (0, -16500), (3000, -16500), (3000, -15000)], {**CYCLE, "name": "Talweg"})  # 6 km, flach

    # --- E: Belag (y=-20000) -------------------------------------------------------
    b.way([(0, -20000), (3000, -20000)], {"highway": "track", "surface": "gravel", "name": "Schotterweg"})
    b.way([(0, -20000), (0, -20800), (3000, -20800), (3000, -20000)], {**CYCLE, "name": "Asphaltweg"})  # 4.6 km

    # --- F: Einbahn (y=-25000) -------------------------------------------------------
    b.way([(0, -25000), (3000, -25000)], {**CYCLE, "oneway": "yes", "name": "Einbahn-Radweg"})
    b.way([(0, -25000), (0, -26000), (3000, -26000), (3000, -25000)], {**CYCLE, "name": "Rückweg"})  # 5 km
    return b


# Testpunkte (lat, lon)
POINTS = {
    "S1": to_ll(0, 0), "T1": to_ll(4000, 0),
    "S2": to_ll(0, -5000), "T2": to_ll(2400, -5000),
    "S3": to_ll(0, -10000), "T3": to_ll(3000, -10000),
    "S4": to_ll(0, -15000), "T4": to_ll(3000, -15000),
    "S5": to_ll(0, -20000), "T5": to_ll(3000, -20000),
    "S6": to_ll(3000, -25000), "T6": to_ll(0, -25000),
}


def write_hgt(directory: str) -> None:
    """Synthetische SRTM-Kachel N47E011 (1201²): Hügel bei Szenario D, sonst flach 600 m."""
    n = 1201
    lat = 47 + 1 - np.arange(n) / (n - 1)
    lon = 11 + np.arange(n) / (n - 1)
    LON, LAT = np.meshgrid(lon, lat)
    x = (LON - LON0) * 111_320.0 * math.cos(math.radians(LAT0))
    y = (LAT - LAT0) * 110_574.0
    # Hügel: Gauß bei (1500, -15000), Höhe +120 m, sigma 500 m; der Talweg (y=-16500) bleibt flach
    z = 600.0 + 120.0 * np.exp(-(((x - 1500) ** 2) + ((y + 15000) ** 2)) / (2 * 500.0**2))
    os.makedirs(directory, exist_ok=True)
    z.astype(">i2").tofile(os.path.join(directory, "N47E011.hgt"))
