"""Innenstadt meiden: POIs (Geschäfte, Gastronomie, Fußgängerzone) -> Innenstadt-Wert -> Kostenfaktor."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fahrradnavi import fetch
from fahrradnavi.api import create_app, exposure_m
from fahrradnavi.auth import AuthConfig
from fahrradnavi.graph import Graph, build_graph
from fahrradnavi.importer import poi_weight, read_osm
from fahrradnavi.profiles import Options
from fahrradnavi.router import Router
from fahrradnavi.urban import CenterField

from fixture import OsmBuilder, to_ll

CYCLE = {"highway": "cycleway", "surface": "asphalt"}


def market_town(b: OsmBuilder, y0: float = 0.0) -> None:
    """Radweg durch einen Marktplatz (x 1000..2000) mit ~500 Geschäften, plus Umgehung 500 m nördlich."""
    b.way([(0, y0), (3000, y0)], {**CYCLE, "name": "Durch den Markt"})
    b.way([(0, y0), (0, y0 + 500), (3000, y0 + 500), (3000, y0)], {**CYCLE, "name": "Umgehung"})  # 4000 m
    for i in range(500):
        b.node(1000 + i * 2, y0 + (25 if i % 2 else -25), {"shop": "clothes" if i % 3 else "bakery"})


@pytest.fixture(scope="module")
def town(tmp_path_factory):
    d = tmp_path_factory.mktemp("center")
    b = OsmBuilder()
    market_town(b)
    b.write(str(d / "c.osm"))
    raw = read_osm(str(d / "c.osm"))
    return raw, build_graph(raw)


def test_poi_weights():
    assert poi_weight({"shop": "clothes"}) == 1.0
    assert poi_weight({"shop": "vacant"}) < 1.0
    assert poi_weight({"amenity": "cafe"}) == 1.0 and poi_weight({"amenity": "bank"}) < 1.0
    assert poi_weight({"tourism": "hotel"}) == 0.5
    for none in ({"amenity": "bench"}, {"amenity": "parking"}, {"highway": "bus_stop"}, {}):
        assert poi_weight(none) == 0.0


def test_importer_collects_pois_and_pedestrian_zone(tmp_path):
    b = OsmBuilder()
    b.node(10, 10, {"shop": "shoes"})
    b.node(20, 20, {"amenity": "cafe"})
    b.node(30, 30, {"amenity": "bench"})  # zählt nicht
    b.way([(0, 0), (400, 0)], {"highway": "pedestrian", "name": "Fußgängerzone"}, step=100)
    b.way([(0, 300), (400, 300)], {"highway": "cycleway"}, step=100)
    b.write(str(tmp_path / "p.osm"))
    raw = read_osm(str(tmp_path / "p.osm"))
    n_ped = len(raw.pois) - 2
    assert len(raw.pois) >= 2 + 10  # 400 m Fußgängerzone alle ~25 m
    assert n_ped >= 10 and (raw.pois[:, 2] > 0).all()
    assert build_graph(raw).has_center


def test_edge_center_values(town):
    _, g = town
    names = {n: i for i, n in enumerate(g.names)}
    e_mark = [e for e in range(g.n_edges) if g.e_name[e] == names["Durch den Markt"]]
    e_umg = [e for e in range(g.n_edges) if g.e_name[e] == names["Umgehung"]]
    mark = sum(float(g.e_len[e]) * g.e_center[e] / 255 for e in e_mark)
    assert mark > 500  # über die Hälfte der 1000 m im Kern sind "voll Innenstadt"
    assert mark < 1500
    assert all(g.e_center[e] < 5 for e in e_umg)  # 500 m entfernt: kein Trubel mehr


def test_route_avoids_center_only_when_asked(town):
    _, g = town
    r = Router(g)
    p = [to_ll(0, 0), to_ll(3000, 0)]
    off = r.route(p, "trekking", Options(center=0.0))
    strong = r.route(p, "trekking", Options(center=3.0))
    assert off.stats["distance_m"] == pytest.approx(3000, abs=20) and off.stats["center_m"] > 500
    assert strong.stats["distance_m"] > 3900 and strong.stats["center_m"] < 30


def test_center_slider_is_monotonic(town):
    _, g = town
    r = Router(g)
    p = [to_ll(0, 0), to_ll(3000, 0)]
    vals = [r.route(p, "trekking", Options(center=c)).stats["center_m"] for c in (0, 0.5, 1, 2, 3)]
    assert vals == sorted(vals, reverse=True)


def test_center_field_thresholds():
    pois = np.array([[11.30, 47.95, 1.0]] * 250)  # 250 Geschäfte an einem Punkt
    f = CenterField(pois, (11.29, 47.94, 11.32, 47.97))
    at = f.sample(np.array([47.95e7]), np.array([11.30e7]))[0]
    far = f.sample(np.array([47.9615e7]), np.array([11.3115e7]))[0]  # ~1,4 km weg
    assert at == 1.0 and far == 0.0
    empty = CenterField(np.zeros((0, 3)), (11.29, 47.94, 11.32, 47.97))
    assert empty.sample(np.array([47.95e7]), np.array([11.30e7]))[0] == 0.0


def test_no_poi_data_disables_slider(graph):
    assert not graph.has_center
    from fixture import POINTS

    r = Router(graph)
    a = r.route([POINTS["S1"], POINTS["T1"]], "trekking", Options(center=0.0))
    b = r.route([POINTS["S1"], POINTS["T1"]], "trekking", Options(center=3.0))
    assert a.stats["distance_m"] == b.stats["distance_m"]


def test_save_load_and_api(town, tmp_path):
    _, g = town
    p = str(tmp_path / "g.npz")
    g.save(p)
    g2 = Graph.load(p)
    assert g2.has_center and (g2.e_center == g.e_center).all()
    c = TestClient(create_app(graph=g2, auth=AuthConfig.disabled()))
    assert c.get("/api/config").json()["has_center"] is True
    pts = [{"lat": to_ll(0, 0)[0], "lon": to_ll(0, 0)[1]}, {"lat": to_ll(3000, 0)[0], "lon": to_ll(3000, 0)[1]}]
    a = c.post("/api/route", json={"points": pts, "center": 0}).json()["stats"]
    b = c.post("/api/route", json={"points": pts, "center": 3}).json()["stats"]
    assert b["center_m"] < a["center_m"] and b["distance_m"] > a["distance_m"]
    assert c.post("/api/route", json={"points": pts, "center": 7}).status_code == 422


def test_exposure_counts_center_when_active(town):
    _, g = town
    r = Router(g)
    p = [to_ll(0, 0), to_ll(3000, 0)]
    assert exposure_m(r.route(p, "trekking", Options(center=0.0))) == 0
    b = r.route(p, "trekking", Options(center=0.5))
    assert exposure_m(b) == pytest.approx(b.stats["center_m"])


def test_merge_osm_dedupes(tmp_path):
    b1, b2 = OsmBuilder(), OsmBuilder()
    b1.way([(0, 0), (300, 0)], CYCLE)
    b1.node(5, 5, {"shop": "x"})
    b2.way([(0, 0), (300, 0)], CYCLE)  # gleiche IDs
    b2.node(5, 5, {"shop": "x"})
    b2.node(50, 50, {"amenity": "cafe"})
    b1.write(str(tmp_path / "a.osm"))
    b2.write(str(tmp_path / "b.osm"))
    out = fetch.merge_osm([str(tmp_path / "a.osm"), str(tmp_path / "b.osm")], str(tmp_path / "m.osm.pbf"))
    raw = read_osm(out)
    assert len(raw.way_hwc) == 1 and len(raw.pois) == 2
