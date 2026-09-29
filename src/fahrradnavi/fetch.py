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
    "oberbayern": {"bbox": None, "pbf": "oberbayern", "label": "Regierungsbezirk Oberbayern"},
    "bayern": {"bbox": None, "pbf": None, "label": "Freistaat Bayern"},
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


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass-QL: alle Wege mit highway=*, Ampeln, benannte Orte und Radrouten-Relationen im Gebiet."""
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    b = f"{s},{w},{n},{e}"
    return f"""[out:xml][timeout:900];
(
  way["highway"]({b});
  node["highway"="traffic_signals"]({b});
  node["place"]["name"]({b});
  node["tourism"]["name"]({b});
  node["historic"]["name"]({b});
  node["amenity"~"^(monastery|place_of_worship|restaurant|cafe|biergarten|pub|townhall|bicycle_repair_station)$"]["name"]({b});
  node["railway"~"^(station|halt)$"]["name"]({b});
);
(._;>;);
out body;
rel["route"="bicycle"]({b});
out body;
"""


def download_overpass(bbox: tuple[float, float, float, float], dest: str, url: str = OVERPASS_URL) -> str:
    """Lädt ein (kleines) Gebiet direkt per Overpass-API als .osm. Für große Gebiete besser Geofabrik nutzen."""
    data = urllib.parse.urlencode({"data": overpass_query(bbox)}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    log.info("Overpass-Abfrage für %s (kann mehrere Minuten dauern)", bbox)
    with urllib.request.urlopen(req, timeout=1200) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
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
