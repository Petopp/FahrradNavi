"""Datenbeschaffung: OSM-Extrakte (Geofabrik/Overpass) und SRTM-Höhenkacheln."""

from __future__ import annotations

import logging
import os
import shutil
import urllib.parse
import urllib.request

from .dem import tiles_for_bbox

log = logging.getLogger(__name__)

USER_AGENT = "FahrradNavi/0.1 (self-hosted bicycle router; https://github.com/petopp/fahrradnavi)"

GEOFABRIK = "https://download.geofabrik.de/europe/germany/bayern/{region}-latest.osm.pbf"
GEOFABRIK_BAYERN = "https://download.geofabrik.de/europe/germany/bayern-latest.osm.pbf"
BAYERN_REGIONS = (
    "oberbayern", "niederbayern", "oberpfalz", "oberfranken", "mittelfranken", "unterfranken", "schwaben",
)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Frei nutzbare SRTM-Kacheln (AWS Open Data "Terrain Tiles", Skadi-Layout)
SRTM_URL = "https://elevation-tiles-prod.s3.amazonaws.com/skadi/{ns}{lat:02d}/{name}.hgt.gz"

# Vordefinierte Gebiete (min_lon, min_lat, max_lon, max_lat) – bewusst großzügig für Umwege.
REGIONS: dict[str, dict] = {
    "starnberg": {"bbox": (11.00, 47.70, 11.65, 48.20), "pbf": "oberbayern", "label": "Landkreis Starnberg + Umland"},
    "oberbayern": {"bbox": None, "dem_bbox": (10.6, 47.1, 13.1, 48.95), "pbf": "oberbayern", "label": "Regierungsbezirk Oberbayern"},
    "bayern": {"bbox": None, "dem_bbox": (8.9, 47.2, 13.9, 50.6), "pbf": None, "label": "Freistaat Bayern"},
}


def download(url: str, dest: str, chunk: int = 1 << 20) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    log.info("Lade %s", url)
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            buf = r.read(chunk)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            if total:
                print(f"\r  {done / 1e6:8.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()
    os.replace(tmp, dest)
    return dest


def geofabrik_url(region: str) -> str:
    if region == "bayern":
        return GEOFABRIK_BAYERN
    if region in BAYERN_REGIONS:
        return GEOFABRIK.format(region=region)
    raise ValueError(f"Unbekannte Geofabrik-Region {region!r}; erlaubt: bayern, {', '.join(BAYERN_REGIONS)}")


LANDUSE_URBAN = "residential|commercial|retail|industrial"


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass-QL: Wege (highway=*), Ampeln, benannte Orte, Siedlungsflächen (landuse) und Radrouten-Relationen."""
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    b = f"{s},{w},{n},{e}"
    return f"""[out:xml][timeout:900];
(
  way["highway"]({b});
  way["landuse"~"^({LANDUSE_URBAN})$"]({b});
  rel["landuse"~"^({LANDUSE_URBAN})$"]({b});
  node["highway"="traffic_signals"]({b});
  node["place"]["name"]({b});
  node["tourism"]["name"]({b});
  node["historic"]["name"]({b});
  node["amenity"~"^(monastery|place_of_worship|restaurant|cafe|biergarten|pub|townhall|bicycle_repair_station)$"]["name"]({b});
  node["railway"~"^(station|halt)$"]["name"]({b});
);
(._;>>;);
out body;
rel["route"="bicycle"]({b});
out body;
"""


def _overpass_fetch(bbox, url: str, dest: str, retries: int = 4) -> None:
    import time
    import urllib.error

    data = urllib.parse.urlencode({"data": overpass_query(bbox)}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            wait = 5 * 2**attempt
            log.warning("Overpass-Kachel %s fehlgeschlagen (%s), neuer Versuch in %d s", bbox, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Overpass-Kachel {bbox} nach {retries} Versuchen nicht ladbar")


def download_overpass(bbox: tuple[float, float, float, float], dest: str, url: str = OVERPASS_URL, tile: float = 0.1) -> str:
    """Lädt ein Gebiet per Overpass-API in Kacheln (je ``tile`` Grad) und schreibt eine .osm.pbf.

    Gedacht für Landkreis-große Gebiete; für ganz Oberbayern/Bayern besser den Geofabrik-Extrakt nutzen.
    """
    import math
    import tempfile
    import time

    import osmium

    min_lon, min_lat, max_lon, max_lat = bbox
    nx = max(1, math.ceil((max_lon - min_lon) / tile))
    ny = max(1, math.ceil((max_lat - min_lat) / tile))
    nodes: dict[int, tuple] = {}
    ways: dict[int, tuple] = {}
    rels: dict[int, tuple] = {}
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for iy in range(ny):
            for ix in range(nx):
                tb = (
                    min_lon + ix * (max_lon - min_lon) / nx, min_lat + iy * (max_lat - min_lat) / ny,
                    min_lon + (ix + 1) * (max_lon - min_lon) / nx, min_lat + (iy + 1) * (max_lat - min_lat) / ny,
                )
                n = iy * nx + ix + 1
                path = os.path.join(tmp, f"t{n}.osm")
                print(f"\r  Kachel {n}/{nx * ny}", end="", flush=True)
                _overpass_fetch(tb, url, path)
                for o in osmium.FileProcessor(path):
                    tags = {t.k: t.v for t in o.tags}
                    if o.is_node():
                        nodes[o.id] = (o.location.lon, o.location.lat, tags)
                    elif o.is_way():
                        ways[o.id] = ([n.ref for n in o.nodes], tags)
                    elif o.is_relation():
                        rels[o.id] = ([(m.type, m.ref, m.role) for m in o.members], tags)
                os.remove(path)
                time.sleep(1.0)  # höflich zum öffentlichen Server
    print()
    log.info("Schreibe %d Nodes, %d Ways, %d Relationen nach %s", len(nodes), len(ways), len(rels), dest)
    with osmium.SimpleWriter(dest, overwrite=True) as w:
        for i in sorted(nodes):
            lon, lat, tags = nodes[i]
            w.add_node(osmium.osm.mutable.Node(id=i, location=(lon, lat), tags=tags))
        for i in sorted(ways):
            refs, tags = ways[i]
            w.add_way(osmium.osm.mutable.Way(id=i, nodes=refs, tags=tags))
        for i in sorted(rels):
            members, tags = rels[i]
            w.add_relation(osmium.osm.mutable.Relation(id=i, members=members, tags=tags))
    return dest


def download_srtm(bbox: tuple[float, float, float, float], out_dir: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    got = []
    for name in tiles_for_bbox(*bbox):
        dest = os.path.join(out_dir, name + ".hgt.gz")
        if os.path.exists(dest) or os.path.exists(os.path.join(out_dir, name + ".hgt")):
            got.append(dest)
            continue
        url = SRTM_URL.format(ns=name[0], lat=int(name[1:3]), name=name)
        try:
            download(url, dest)
            got.append(dest)
        except Exception as exc:  # Kachel fehlt (z. B. Meer) oder Netz-Problem
            log.warning("Kachel %s nicht geladen: %s", name, exc)
    return got
