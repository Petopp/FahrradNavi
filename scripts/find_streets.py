"""Findet Straßen nach Namen – im fertigen Graphen und (optional) in den Rohdaten inklusive ausgeschlossener Wege.

    python scripts/find_streets.py data/graph.npz Leutstettener Gautinger
    python scripts/find_streets.py data/graph.npz Leutstettener --osm data/starnberg.osm.pbf

Mit --osm wird für jeden Weg angezeigt, ob er in den Graphen übernommen wurde oder warum nicht
(z. B. Fußweg ohne Radfreigabe, ``access=private``).
"""
from __future__ import annotations

import argparse
from collections import Counter

import numpy as np

from fahrradnavi import tags as T
from fahrradnavi.graph import Graph

E7 = 1e7
SHOW_TAGS = ("highway", "bicycle", "foot", "access", "vehicle", "motor_vehicle", "surface", "oneway", "cycleway",
             "cycleway:left", "cycleway:right", "cycleway:both", "segregated", "tracktype", "smoothness", "maxspeed")


def in_graph(g: Graph, queries: list[str]) -> None:
    for q in queries:
        idx = [i for i, n in enumerate(g.names) if q.lower() in n.lower()]
        print(f"\n== Graph: {q}")
        for i in idx[:20]:
            es = np.flatnonzero(g.e_name == i)
            if len(es) == 0:
                continue
            lat = g.v_lat[g.e_vs[es]] / E7
            lon = g.v_lon[g.e_vs[es]] / E7
            hw = Counter(T.HW_NAMES[int(h)] for h in g.e_hwc[es])
            print(
                f"  {g.names[i]:32s} {len(es):3d} Kanten  lat {lat.min():.4f}-{lat.max():.4f} "
                f"lon {lon.min():.4f}-{lon.max():.4f}  {dict(hw)}"
            )


def in_raw(path: str, queries: list[str]) -> None:
    import osmium

    qs = [q.lower() for q in queries]
    fp = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(osmium.filter.KeyFilter("highway"))
    for w in fp:
        if not w.is_way():
            continue
        name = w.tags.get("name", "")
        if not any(q in name.lower() for q in qs):
            continue
        tags = {t.k: t.v for t in w.tags}
        info = T.classify_way(tags)
        pts = [(n.lat, n.lon) for n in w.nodes if n.location.valid()]
        if not pts:
            continue
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        verdict = "AUSGESCHLOSSEN" if info is None else f"ok ({T.HW_NAMES[info.hwc]})"
        shown = ", ".join(f"{k}={tags[k]}" for k in SHOW_TAGS if k in tags)
        print(f"way {w.id:>11d} {name[:28]:28s} {lat:.4f},{lon:.4f} {verdict:22s} {shown}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("graph")
    ap.add_argument("names", nargs="+")
    ap.add_argument("--osm", help="Roh-PBF: zeigt auch ausgeschlossene Wege")
    a = ap.parse_args()
    in_graph(Graph.load(a.graph), a.names)
    if a.osm:
        print("\n== Rohdaten")
        in_raw(a.osm, a.names)


if __name__ == "__main__":
    main()
