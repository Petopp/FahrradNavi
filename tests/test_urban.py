"""Bebauung meiden: Siedlungsflächen (landuse) -> Kantenanteil -> Kostenfaktor."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fahrradnavi.api import create_app, exposure_m
from fahrradnavi.graph import Graph, build_graph
from fahrradnavi.importer import read_osm
from fahrradnavi.profiles import Options
from fahrradnavi.router import Router
from fahrradnavi.urban import UrbanRaster

from fixture import OsmBuilder, to_ll

CYCLE = {"highway": "cycleway", "surface": "asphalt"}
LU = {"landuse": "residential"}


def square(b, x0, y0, x1, y1, tags):
    return b.way([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], tags, step=1000)


@pytest.fixture(scope="module")
def town(tmp_path_factory):
    """S(0,0) -> T(3000,0): 3000 m gerade durch ein Wohngebiet (x 800..2200) ODER 3200 m Umweg um das Gebiet."""
    d = tmp_path_factory.mktemp("urban")
    b = OsmBuilder()
    b.way([(0, 0), (3000, 0)], {**CYCLE, "name": "Durch die Stadt"})
    b.way([(0, 0), (0, 400), (3000, 400), (3000, 0)], {**CYCLE, "name": "Umgehung"})  # 3800 m, Gebiet reicht bis y=300
    square(b, 800, -300, 2200, 300, LU)
    b.write(str(d / "u.osm"))
    raw = read_osm(str(d / "u.osm"))
    return raw, build_graph(raw)


def test_importer_reads_closed_way_area(town):
    raw, g = town
    assert len(raw.urban_rings) == 1
    assert g.has_urban


def test_edge_urban_fraction(town):
    _, g = town
    names = {n: i for i, n in enumerate(g.names)}
    e_stadt = [e for e in range(g.n_edges) if g.e_name[e] == names["Durch die Stadt"]]
    e_umg = [e for e in range(g.n_edges) if g.e_name[e] == names["Umgehung"]]
    total = sum(float(g.e_len[e]) for e in e_stadt)
    inside = sum(float(g.e_len[e]) * g.e_urban[e] / 255 for e in e_stadt)
    assert inside == pytest.approx(1400, abs=60)  # x 800..2200
    assert total == pytest.approx(3000, abs=5)
    assert all(g.e_urban[e] == 0 for e in e_umg)


def test_route_avoids_urban_area_only_when_asked(town):
    _, g = town
    r = Router(g)
    p = [to_ll(0, 0), to_ll(3000, 0)]
    off = r.route(p, "trekking", Options(urban=0.0))
    on = r.route(p, "trekking", Options(urban=1.0))
    strong = r.route(p, "trekking", Options(urban=3.0))
    assert off.stats["distance_m"] == pytest.approx(3000, abs=20) and off.stats["urban_m"] == pytest.approx(1400, abs=60)
    # Regler 1: Faktor 1+1,5*(1400/3000)=1,7 -> Kosten 5100+1600 < Umgehung 3800*0.9... prüfen, welche gewinnt
    assert on.stats["urban_m"] <= off.stats["urban_m"]
    assert strong.stats["distance_m"] > 3700 and strong.stats["urban_m"] < 5


def test_urban_slider_is_monotonic(town):
    _, g = town
    r = Router(g)
    p = [to_ll(0, 0), to_ll(3000, 0)]
    urb = [r.route(p, "trekking", Options(urban=u)).stats["urban_m"] for u in (0, 0.5, 1, 2, 3)]
    assert urb == sorted(urb, reverse=True)


def test_no_urban_data_means_slider_has_no_effect(graph):
    assert not graph.has_urban
    r = Router(graph)
    from fixture import POINTS

    a = r.route([POINTS["S1"], POINTS["T1"]], "trekking", Options(urban=0.0))
    b = r.route([POINTS["S1"], POINTS["T1"]], "trekking", Options(urban=3.0))
    assert a.stats["distance_m"] == b.stats["distance_m"]


def test_multipolygon_with_hole(tmp_path):
    b = OsmBuilder()
    outer = square(b, 0, 0, 1000, 1000, {})
    inner = square(b, 400, 400, 600, 600, {})
    b.relations.append((9100, [(outer, "outer"), (inner, "inner")], {"type": "multipolygon", **LU}))
    b.way([(-100, 500), (1100, 500)], CYCLE, step=50)  # Radweg mitten durch, kreuzt das Loch
    b.write(str(tmp_path / "m.osm"))
    raw = read_osm(str(tmp_path / "m.osm"))
    assert len(raw.urban_rings) == 1 and len(raw.urban_rings[0][1]) == 1
    g = build_graph(raw)
    e = np.flatnonzero(g.e_urban)
    inside = float((g.e_len * g.e_urban / 255.0).sum())
    assert inside == pytest.approx(1000 - 200, abs=60)  # 1000 m im Polygon minus 200 m im Loch


def test_urban_raster_basic():
    ring = np.array([[11.30, 47.95], [11.31, 47.95], [11.31, 47.96], [11.30, 47.96], [11.30, 47.95]])
    r = UrbanRaster([(ring, [])], (11.29, 47.94, 11.32, 47.97))
    lat = np.array([47.955, 47.945, 47.955], dtype=float) * 1e7
    lon = np.array([11.305, 11.305, 11.315], dtype=float) * 1e7
    assert r.contains(lat, lon).tolist() == [True, False, False]


def test_save_load_keeps_urban(town, tmp_path):
    _, g = town
    p = str(tmp_path / "g.npz")
    g.save(p)
    g2 = Graph.load(p)
    assert g2.has_urban and (g2.e_urban == g.e_urban).all()


def test_old_format_is_rejected(town, tmp_path):
    import json

    _, g = town
    p = str(tmp_path / "old.npz")
    g.meta["format"] = 1
    try:
        g.save(p)
    finally:
        g.meta["format"] = 2
    with pytest.raises(ValueError, match="neu bauen"):
        Graph.load(p)


def test_api_urban_parameter_and_config(town):
    _, g = town
    c = TestClient(create_app(graph=g))
    assert c.get("/api/config").json()["has_urban"] is True
    pts = [{"lat": to_ll(0, 0)[0], "lon": to_ll(0, 0)[1]}, {"lat": to_ll(3000, 0)[0], "lon": to_ll(3000, 0)[1]}]
    a = c.post("/api/route", json={"points": pts, "urban": 0}).json()["stats"]
    b = c.post("/api/route", json={"points": pts, "urban": 3}).json()["stats"]
    assert b["urban_m"] < a["urban_m"] and b["distance_m"] > a["distance_m"]
    assert c.post("/api/route", json={"points": pts, "urban": 9}).status_code == 422


def test_exposure_counts_urban_only_when_active(town):
    _, g = town
    r = Router(g)
    p = [to_ll(0, 0), to_ll(3000, 0)]
    a = r.route(p, "trekking", Options(urban=0.0))
    b = r.route(p, "trekking", Options(urban=0.5))
    assert exposure_m(a) == 0
    assert exposure_m(b) == pytest.approx(0.5 * b.stats["urban_m"])
