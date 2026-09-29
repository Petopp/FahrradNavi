"""Liest OSM-Daten (.osm.pbf, .osm, .osm.bz2 ...) und extrahiert Wege, Ampeln und Orte."""

from __future__ import annotations

import logging
from array import array
from dataclasses import dataclass, field

import numpy as np

from . import tags as T

log = logging.getLogger(__name__)

E7 = 10_000_000

_PLACE_RANK = {
    "city": 10, "town": 9, "village": 8, "suburb": 7, "quarter": 6, "neighbourhood": 5,
    "hamlet": 6, "locality": 4, "isolated_dwelling": 4, "square": 3,
}
_AMENITY_OK = {
    "monastery": 6, "place_of_worship": 4, "restaurant": 3, "cafe": 3, "biergarten": 5, "pub": 3,
    "bicycle_repair_station": 3, "ice_cream": 2, "fast_food": 1, "drinking_water": 1,
    "bus_station": 3, "ferry_terminal": 3, "theatre": 3, "townhall": 4, "marketplace": 3,
    "university": 4, "school": 1, "hospital": 3, "library": 2, "cinema": 2,
}
_NATURAL_OK = {"peak": 4, "beach": 3, "spring": 2, "cave_entrance": 2}

# Diese Schlüssel werden beim Lesen vorgefiltert (OR-Verknüpfung, schnell in C++).
INTERESTING_KEYS = ("highway", "place", "tourism", "historic", "amenity", "railway", "natural", "leisure")


@dataclass
class RawData:
    refs: np.ndarray  # int64, OSM-Node-ID je Vertex (Ways hintereinander)
    vlat: np.ndarray  # int32, Grad * 1e7
    vlon: np.ndarray
    way_off: np.ndarray  # int64, Länge W+1
    way_hwc: np.ndarray  # uint8
    way_fwd: np.ndarray  # bool
    way_bwd: np.ndarray
    way_infra: np.ndarray  # uint8
    way_surface: np.ndarray
    way_smooth: np.ndarray
    way_maxspeed: np.ndarray
    way_flags: np.ndarray
    way_name: np.ndarray  # int32, Index in names
    names: list[str]
    signal_ids: np.ndarray  # int64
    places: list[tuple[str, str, int, float, float]] = field(default_factory=list)
    # Siedlungsflächen: [(äußerer Ring (N,2) lon/lat, [Löcher])]
    urban_rings: list = field(default_factory=list)


URBAN_LANDUSE = ("residential", "commercial", "retail", "industrial")


def read_urban_areas(path: str, bbox: tuple[float, float, float, float] | None = None) -> list:
    """Siedlungsflächen (landuse=residential/commercial/retail/industrial) als Polygonringe.

    Nutzt die Flächen-Zusammensetzung von osmium (geschlossene Wege und Multipolygon-Relationen).
    """
    import osmium

    tag_filter = osmium.filter.TagFilter(*[("landuse", v) for v in URBAN_LANDUSE])
    fp = osmium.FileProcessor(path).with_locations(_location_store(path)).with_areas(tag_filter).with_filter(tag_filter)
    out = []
    for o in fp:
        if not o.is_area():
            continue
        for outer in o.outer_rings():
            ring = np.array([(n.lon, n.lat) for n in outer if n.location.valid()], dtype=np.float64)
            if len(ring) < 4:
                continue
            if bbox is not None and (
                ring[:, 0].max() < bbox[0] or ring[:, 0].min() > bbox[2]
                or ring[:, 1].max() < bbox[1] or ring[:, 1].min() > bbox[3]
            ):
                continue
            holes = []
            for inner in o.inner_rings(outer):
                h = np.array([(n.lon, n.lat) for n in inner if n.location.valid()], dtype=np.float64)
                if len(h) >= 4:
                    holes.append(h)
            out.append((ring, holes))
    log.info("  %d Siedlungsflächen", len(out))
    return out


def place_info(tags: dict[str, str]) -> tuple[str, int] | None:
    """(Art, Rang) für suchbare Orte, sonst None."""
    if "name" not in tags:
        return None
    p = tags.get("place")
    if p in _PLACE_RANK:
        return (p, _PLACE_RANK[p])
    a = tags.get("amenity")
    if a in _AMENITY_OK:
        return (a, _AMENITY_OK[a])
    t = tags.get("tourism")
    if t and t not in ("hotel", "guest_house", "motel", "apartment", "hostel", "chalet"):
        return (t, 5 if t in ("attraction", "viewpoint", "museum") else 3)
    h = tags.get("historic")
    if h and h not in ("yes", "boundary_stone", "wayside_cross", "wayside_shrine", "memorial"):
        return (h, 5)
    r = tags.get("railway")
    if r in ("station", "halt"):
        return (r, 5)
    n = tags.get("natural")
    if n in _NATURAL_OK:
        return (n, _NATURAL_OK[n])
    l = tags.get("leisure")
    if l in ("park", "swimming_area", "nature_reserve", "water_park"):
        return (l, 3)
    return None


def _location_store(path: str) -> str:
    """Node-Cache: im RAM für kleine Dateien, sonst auf Platte (Bayern hat >250 Mio. Nodes)."""
    import os
    import tempfile

    if os.path.getsize(path) < 400 * 1024 * 1024:
        return "flex_mem"
    tmp = os.path.join(tempfile.gettempdir(), "fahrradnavi-nodecache.bin")
    log.info("Große Datei: Node-Cache auf Platte (%s)", tmp)
    return f"sparse_file_array,{tmp}"


def _tags(o) -> dict[str, str]:
    return {t.k: t.v for t in o.tags}


def read_osm(path: str, bbox: tuple[float, float, float, float] | None = None, urban: bool = True) -> RawData:
    """Liest die Datei und gibt die rohen Wege zurück.

    bbox = (min_lon, min_lat, max_lon, max_lat): Wege, die komplett außerhalb liegen, werden verworfen.
    """
    import osmium

    log.info("Pass 1: Radrouten-Relationen in %s", path)
    route_ways: set[int] = set()
    for r in osmium.FileProcessor(path, osmium.osm.RELATION):
        if r.tags.get("route") == "bicycle":
            for m in r.members:
                if m.type == "w":
                    route_ways.add(m.ref)
    log.info("  %d Wege in Radrouten", len(route_ways))

    log.info("Pass 2: Wege, Ampeln, Orte")
    refs = array("q")
    vlat = array("i")
    vlon = array("i")
    off = array("q", [0])
    cols: dict[str, array] = {k: array("i") for k in ("hwc", "fwd", "bwd", "infra", "surface", "smooth", "maxspeed", "flags", "name")}
    names: list[str] = [""]
    name_idx: dict[str, int] = {"": 0}
    signals = array("q")
    places: list[tuple[str, str, int, float, float]] = []
    seen_streets: set[tuple[str, int, int]] = set()

    fp = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_locations(_location_store(path)).with_filter(
        osmium.filter.KeyFilter(*INTERESTING_KEYS)
    )
    n_ways = 0
    for o in fp:
        if o.is_node():
            tg = _tags(o)
            if tg.get("highway") == "traffic_signals" or tg.get("crossing") == "traffic_signals":
                signals.append(o.id)
            pi = place_info(tg)
            if pi and o.location.valid():
                places.append((tg["name"], pi[0], pi[1], o.location.lat, o.location.lon))
            continue

        tg = _tags(o)
        info = T.classify_way(tg)
        if info is None:
            pi = place_info(tg)
            if pi:
                pts = [(n.lat, n.lon) for n in o.nodes if n.location.valid()]
                if pts:
                    la = sum(p[0] for p in pts) / len(pts)
                    lo = sum(p[1] for p in pts) / len(pts)
                    places.append((tg["name"], pi[0], pi[1], la, lo))
            continue

        r_ids: list[int] = []
        r_lat: list[int] = []
        r_lon: list[int] = []
        inside = bbox is None
        for n in o.nodes:
            loc = n.location
            if not loc.valid():
                continue
            la, lo = loc.lat, loc.lon
            if bbox is not None and bbox[0] <= lo <= bbox[2] and bbox[1] <= la <= bbox[3]:
                inside = True
            r_ids.append(n.ref)
            r_lat.append(int(round(la * E7)))
            r_lon.append(int(round(lo * E7)))
        if len(r_ids) < 2 or not inside:
            continue

        flags = info.flags | (T.FLAG_ROUTE if o.id in route_ways else 0)
        nm = info.name
        ni = name_idx.get(nm)
        if ni is None:
            ni = name_idx[nm] = len(names)
            names.append(nm)
        refs.extend(r_ids)
        vlat.extend(r_lat)
        vlon.extend(r_lon)
        off.append(len(refs))
        cols["hwc"].append(info.hwc)
        cols["fwd"].append(int(info.fwd))
        cols["bwd"].append(int(info.bwd))
        cols["infra"].append(info.infra)
        cols["surface"].append(info.surface)
        cols["smooth"].append(info.smooth)
        cols["maxspeed"].append(info.maxspeed)
        cols["flags"].append(flags)
        cols["name"].append(ni)
        n_ways += 1

        # Straßennamen als Suchtreffer (je Name und ~2 km Zelle einmal)
        if nm and info.hwc <= T.HW_LIVING_STREET and tg.get("name"):
            mid = len(r_ids) // 2
            key = (nm, r_lat[mid] // 200_000, r_lon[mid] // 300_000)
            if key not in seen_streets:
                seen_streets.add(key)
                places.append((nm, "street", 0, r_lat[mid] / E7, r_lon[mid] / E7))

    log.info("  %d Wege, %d Vertices, %d Ampeln, %d Orte", n_ways, len(refs), len(signals), len(places))
    urban_rings: list = []
    if urban:
        log.info("Pass 3: Siedlungsflächen")
        urban_rings = read_urban_areas(path, bbox)

    def np_(a: array, dt) -> np.ndarray:
        return np.frombuffer(a, dtype=a.typecode).astype(dt) if len(a) else np.zeros(0, dt)

    return RawData(
        refs=np_(refs, np.int64),
        vlat=np_(vlat, np.int32),
        vlon=np_(vlon, np.int32),
        way_off=np_(off, np.int64),
        way_hwc=np_(cols["hwc"], np.uint8),
        way_fwd=np_(cols["fwd"], bool),
        way_bwd=np_(cols["bwd"], bool),
        way_infra=np_(cols["infra"], np.uint8),
        way_surface=np_(cols["surface"], np.uint8),
        way_smooth=np_(cols["smooth"], np.uint8),
        way_maxspeed=np_(cols["maxspeed"], np.uint8),
        way_flags=np_(cols["flags"], np.uint8),
        way_name=np_(cols["name"], np.int32),
        names=names,
        signal_ids=np.unique(np_(signals, np.int64)),
        places=places,
        urban_rings=urban_rings,
    )
