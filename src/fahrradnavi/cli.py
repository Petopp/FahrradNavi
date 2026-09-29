"""Kommandozeile: download | dem | build | serve | route | info"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import fetch
from .profiles import DEFAULT_PROFILE, PROFILES, Options


def _bbox(s: str) -> tuple[float, float, float, float]:
    parts = [float(x) for x in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox = min_lon,min_lat,max_lon,max_lat")
    return tuple(parts)  # type: ignore[return-value]


def _place(s: str) -> str:
    return s


def _resolve(s: str, geocoder) -> tuple[float, float]:
    """'lat,lon' oder ein Ortsname aus den Kartendaten (z. B. "Kloster Andechs")."""
    try:
        a, b = s.split(",")
        return float(a), float(b)
    except ValueError:
        pass
    hits = geocoder.search(s, limit=1)
    if not hits:
        sys.exit(f"Ort {s!r} nicht in den Kartendaten gefunden – lat,lon angeben oder anders schreiben.")
    print(f"  {s!r} -> {hits[0]['name']} ({hits[0]['kind']}) {hits[0]['lat']:.5f},{hits[0]['lon']:.5f}")
    return hits[0]["lat"], hits[0]["lon"]


def cmd_download(a: argparse.Namespace) -> None:
    region = fetch.REGIONS.get(a.region)
    if a.overpass:
        bbox = a.bbox or (region or {}).get("bbox")
        if not bbox:
            sys.exit("Für --overpass wird --bbox oder eine Region mit fester bbox (z. B. starnberg) benötigt.")
        dest = os.path.join(a.out, f"{a.region}.osm.pbf")
        fetch.download_overpass(bbox, dest, url=a.overpass_url)
    else:
        pbf = (region or {}).get("pbf", a.region) if a.region != "bayern" else None
        url = fetch.geofabrik_url(pbf or "bayern")
        dest = os.path.join(a.out, os.path.basename(url))
        fetch.download(url, dest)
    print("Fertig:", dest)


def cmd_dem(a: argparse.Namespace) -> None:
    reg = fetch.REGIONS.get(a.region or "") or {}
    bbox = a.bbox or reg.get("bbox") or reg.get("dem_bbox")
    if not bbox:
        sys.exit("--bbox oder --region angeben.")
    files = fetch.download_srtm(bbox, a.out)
    print(f"{len(files)} Höhenkacheln in {a.out}")


def cmd_build(a: argparse.Namespace) -> None:
    from .dem import Dem
    from .graph import build_graph
    from .importer import read_osm

    region = fetch.REGIONS.get(a.region or "") or {}
    bbox = a.bbox or region.get("bbox")
    t0 = time.time()
    raw = read_osm(a.osm, bbox=bbox)
    dem = Dem(a.dem) if a.dem and os.path.isdir(a.dem) else None
    if a.dem and dem is None:
        logging.warning("Höhenverzeichnis %s nicht gefunden – baue ohne Höhen.", a.dem)
    g = build_graph(raw, dem, bbox=bbox)
    g.save(a.out)
    print(f"Graph gespeichert: {a.out} ({g.n_nodes} Knoten, {g.n_edges} Kanten) in {time.time() - t0:.0f} s")


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    if a.no_auth:
        os.environ["FAHRRADNAVI_AUTH"] = "off"
    # Proxy-Header wertet FahrradNavi selbst aus (nur von FAHRRADNAVI_TRUSTED_PROXIES), nicht uvicorn
    uvicorn.run(create_app(graph_path=a.graph), host=a.host, port=a.port, proxy_headers=False, server_header=False)


def cmd_hash_password(a: argparse.Namespace) -> None:
    import getpass

    from . import auth

    if a.stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("Neues Passwort: ")
        if pw != getpass.getpass("Wiederholen: "):
            sys.exit("Die Passwörter stimmen nicht überein.")
    if len(pw) < auth.MIN_PASSWORD_LEN:
        sys.exit(f"Passwort zu kurz (mindestens {auth.MIN_PASSWORD_LEN} Zeichen).")
    print(auth.hash_password(pw))
    print("\nIn .env eintragen:  FAHRRADNAVI_PASSWORD_HASH=<obige Zeile>  (in .env in einfache Anführungszeichen setzen: FAHRRADNAVI_PASSWORD_HASH='...')",
          file=sys.stderr)


def cmd_setup(a: argparse.Namespace) -> None:
    """Alles für ein Gebiet in einem Schritt: OSM laden, Höhen laden, Graph bauen (überspringt Vorhandenes)."""
    reg = fetch.REGIONS[a.region]
    os.makedirs(a.data, exist_ok=True)
    out = os.path.join(a.data, "graph.npz")
    if os.path.exists(out) and not a.force:
        print(f"{out} existiert bereits (mit --force neu bauen).")
        return
    if a.overpass:
        bbox = reg.get("bbox")
        if not bbox:
            sys.exit("--overpass geht nur für kleine Gebiete (z. B. starnberg); für große bitte Geofabrik verwenden.")
        osm = os.path.join(a.data, f"{a.region}.osm.pbf")
        if not os.path.exists(osm) or a.force:
            fetch.download_overpass(bbox, osm, url=a.overpass_url)
    else:
        url = fetch.geofabrik_url(reg.get("pbf") or "bayern")
        osm = os.path.join(a.data, os.path.basename(url))
        if not os.path.exists(osm) or a.force:
            fetch.download(url, osm)
    bbox = reg.get("bbox")
    dem_dir = os.path.join(a.data, "dem")
    if not a.no_dem:
        fetch.download_srtm(bbox or reg["dem_bbox"], dem_dir)
    ns = argparse.Namespace(osm=osm, out=out, dem=dem_dir if not a.no_dem else None, region=a.region, bbox=None)
    cmd_build(ns)


def cmd_route(a: argparse.Namespace) -> None:
    from .graph import Graph
    from .gpx import route_to_gpx
    from .router import Router

    from .geocoder import Geocoder

    g = Graph.load(a.graph)
    r = Router(g)
    gc = Geocoder(g.places)
    pts = [_resolve(x, gc) for x in [a.start] + list(a.via or []) + [a.end]]
    opts = Options(avoid_roads=a.avoid, calm=a.calm, urban=a.urban, hills=a.hills, surface=a.surface)
    res = r.route(pts, a.profile, opts, compare=True)
    s = res.stats
    print(f"Strecke:      {s['distance_m'] / 1000:.2f} km")
    print(f"Dauer:        {s['duration_s'] / 60:.0f} min")
    print(f"Höhenmeter:   +{s['ascent_m']} m / -{s['descent_m']} m")
    print(f"Autostraße:   {s['road_m'] / 1000:.2f} km  (ruhig: {s['calm_m'] / 1000:.2f} km, eigener Weg: {s['own_m'] / 1000:.2f} km)")
    if g.has_urban:
        print(f"Bebauung:     {s['urban_m'] / 1000:.2f} km innerhalb von Siedlungsflächen")
    if "detour_m" in s:
        print(f"Umweg gegenüber Standard-Routing: {s['detour_m'] / 1000:+.2f} km "
              f"(Standard hätte {s['baseline']['road_m'] / 1000:.2f} km Autostraße)")
    if a.gpx:
        with open(a.gpx, "w", encoding="utf-8") as f:
            f.write(route_to_gpx(res.coords))
        print("GPX:", a.gpx)


def cmd_explain(a: argparse.Namespace) -> None:
    """Vergleicht die frei berechnete Route mit einer Route durch feste Zwischenpunkte."""
    from . import explain
    from .geocoder import Geocoder
    from .graph import Graph
    from .router import Router

    g = Graph.load(a.graph)
    r = Router(g)
    gc = Geocoder(g.places)
    start, end = _resolve(a.start, gc), _resolve(a.end, gc)
    vias = [_resolve(x, gc) for x in (a.via or [])]
    opts = Options(avoid_roads=a.avoid, calm=a.calm, urban=a.urban, hills=a.hills, surface=a.surface)
    out = explain.compare(r, [start, end], [start] + vias + [end], a.profile, opts)
    for label, key in (("Vom Router gewählte Route", "free"), ("Route durch deine Zwischenpunkte", "via")):
        res = out[key]
        s = res.stats
        print(f"\n=== {label}: {s['distance_m'] / 1000:.1f} km, Kosten {res.cost / 1000:.1f}k, "
              f"Autostraße {s['road_m'] / 1000:.2f} km, ruhig {s['calm_m'] / 1000:.2f} km, +{s['ascent_m']} m")
        print(explain.format_rows(out[key + "_rows"], min_length=a.min_length))
    print(f"\nDeine Route kostet {out['extra_cost'] / 1000:+.1f}k Meter-Äquivalente ({out['extra_pct']:+.0f} %) gegenüber der gewählten.")
    worst = sorted(explain.merge_rows(out["via_rows"]), key=lambda r_: r_.excess_cost, reverse=True)[:6]
    print("Größte Kostentreiber auf deiner Route (Aufpreis über der reinen Länge):")
    for w in worst:
        print(f"  {w.name[:34]:34s} {w.highway:12s} {w.surface:10s} {w.length_m:5.0f} m  Malus {w.road_penalty:5.1f}  Aufpreis {w.excess_cost:6.0f}")
    if a.gpx:
        from .gpx import route_to_gpx

        with open(a.gpx, "w", encoding="utf-8") as f:
            f.write(route_to_gpx(out["via"].coords, name="FahrradNavi Wunschroute"))
        print("GPX deiner Route:", a.gpx)


def cmd_info(a: argparse.Namespace) -> None:
    from .graph import Graph

    g = Graph.load(a.graph)
    print(g.meta)
    print(f"{len(g.places)} suchbare Orte, {len(g.names)} Namen")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="fahrradnavi", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="OSM-Daten laden (Geofabrik-PBF oder Overpass)")
    d.add_argument("region", choices=sorted(fetch.REGIONS) + list(fetch.BAYERN_REGIONS))
    d.add_argument("--out", default="data")
    d.add_argument("--overpass", action="store_true", help="statt PBF ein kleines Gebiet per Overpass-API laden")
    d.add_argument("--bbox", type=_bbox)
    d.add_argument("--overpass-url", default=os.environ.get("FAHRRADNAVI_OVERPASS_URL", fetch.OVERPASS_URL),
                   help="Overpass-Endpunkt (Standard: overpass-api.de/api/interpreter)")
    d.set_defaults(func=cmd_download)

    h = sub.add_parser("dem", help="SRTM-Höhenkacheln laden")
    h.add_argument("--region", choices=sorted(fetch.REGIONS))
    h.add_argument("--bbox", type=_bbox)
    h.add_argument("--out", default="data/dem")
    h.set_defaults(func=cmd_dem)

    b = sub.add_parser("build", help="Routing-Graph bauen")
    b.add_argument("osm", help=".osm.pbf oder .osm")
    b.add_argument("--out", default="data/graph.npz")
    b.add_argument("--dem", default="data/dem")
    b.add_argument("--region", choices=sorted(fetch.REGIONS))
    b.add_argument("--bbox", type=_bbox, help="min_lon,min_lat,max_lon,max_lat zuschneiden")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("serve", help="Weboberfläche + API starten")
    s.add_argument("--graph", default=os.environ.get("FAHRRADNAVI_GRAPH", "data/graph.npz"))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-auth", action="store_true", help="Zugriffsschutz abschalten (nur lokal/vertrauenswürdiges Netz!)")
    s.set_defaults(func=cmd_serve)

    hp = sub.add_parser("hash-password", help="Passwort-Hash für FAHRRADNAVI_PASSWORD_HASH erzeugen")
    hp.add_argument("--stdin", action="store_true", help="Passwort aus stdin lesen (für Skripte)")
    hp.set_defaults(func=cmd_hash_password)

    su = sub.add_parser("setup", help="Gebiet einrichten: OSM + Höhen laden und Graph bauen (für Docker)")
    su.add_argument("region", choices=sorted(fetch.REGIONS))
    su.add_argument("--data", default=os.environ.get("FAHRRADNAVI_DATA_DIR", "data"))
    su.add_argument("--overpass", action="store_true", help="kleines Gebiet per Overpass statt Geofabrik laden")
    su.add_argument("--overpass-url", default=os.environ.get("FAHRRADNAVI_OVERPASS_URL", fetch.OVERPASS_URL))
    su.add_argument("--no-dem", action="store_true", help="ohne Höhendaten bauen")
    su.add_argument("--force", action="store_true", help="vorhandene Dateien neu laden/bauen")
    su.set_defaults(func=cmd_setup)

    r = sub.add_parser("route", help="Route auf der Kommandozeile berechnen")
    r.add_argument("--graph", default="data/graph.npz")
    r.add_argument("--from", dest="start", type=_place, required=True, metavar="LAT,LON|ORT")
    r.add_argument("--to", dest="end", type=_place, required=True, metavar="LAT,LON|ORT")
    r.add_argument("--via", type=_place, action="append", metavar="LAT,LON|ORT")
    r.add_argument("--profile", choices=list(PROFILES), default=DEFAULT_PROFILE)
    r.add_argument("--avoid", type=float, default=1.0, help="Straßen-Meidung 0..2 (Standard 1)")
    r.add_argument("--calm", type=float, default=1.0, help="Wohnstraßen zusätzlich meiden 0..3 (Standard 1)")
    r.add_argument("--urban", type=float, default=0.0, help="Bebauung meiden 0..3 (Standard 0 = aus)")
    r.add_argument("--hills", type=float, default=1.0)
    r.add_argument("--surface", type=float, default=1.0)
    r.add_argument("--gpx", help="GPX-Datei schreiben")
    r.set_defaults(func=cmd_route)

    x = sub.add_parser("explain", help="Route erklären: gewählte Route vs. Route durch feste Zwischenpunkte")
    x.add_argument("--graph", default="data/graph.npz")
    x.add_argument("--from", dest="start", type=_place, required=True, metavar="LAT,LON|ORT")
    x.add_argument("--to", dest="end", type=_place, required=True, metavar="LAT,LON|ORT")
    x.add_argument("--via", type=_place, action="append", metavar="LAT,LON|ORT")
    x.add_argument("--profile", choices=list(PROFILES), default=DEFAULT_PROFILE)
    x.add_argument("--avoid", type=float, default=1.0)
    x.add_argument("--calm", type=float, default=1.0)
    x.add_argument("--urban", type=float, default=0.0)
    x.add_argument("--hills", type=float, default=1.0)
    x.add_argument("--surface", type=float, default=1.0)
    x.add_argument("--min-length", type=float, default=0.0, help="kürzere Abschnitte (m) ausblenden")
    x.add_argument("--gpx", help="GPX der Route durch die Zwischenpunkte schreiben")
    x.set_defaults(func=cmd_explain)

    i = sub.add_parser("info", help="Graph-Infos")
    i.add_argument("--graph", default="data/graph.npz")
    i.set_defaults(func=cmd_info)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
