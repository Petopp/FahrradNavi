import pytest
from fastapi.testclient import TestClient

from fahrradnavi.auth import AuthConfig
from fahrradnavi.api import create_app, exposure_m, label_routes
from fahrradnavi.graph import build_graph
from fahrradnavi.importer import read_osm
from fahrradnavi.profiles import Options
from fahrradnavi.router import Router

from fixture import OsmBuilder, to_ll

CYCLE = {"highway": "cycleway", "surface": "asphalt"}


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    """S(0,0) -> T(2000,0) über drei getrennte Radwege (2000 m, ~2090 m, ~2330 m) + eine Wohnstraße-Abkürzung weiter südlich."""
    d = tmp_path_factory.mktemp("alt")
    b = OsmBuilder()
    b.way([(0, 0), (2000, 0)], {**CYCLE, "name": "Direkt"})
    b.way([(0, 0), (1000, 300), (2000, 0)], {**CYCLE, "name": "Nord"})
    b.way([(0, 0), (1000, -600), (2000, 0)], {**CYCLE, "name": "Süd"})
    # zweiter Ort: Wohnstraße 500 m vs. Radweg-Umweg 900 m
    b.way([(0, -5000), (500, -5000)], {"highway": "residential", "name": "Wohnstraße", "surface": "asphalt"})
    b.way([(0, -5000), (250, -5250), (500, -5000)], {**CYCLE, "name": "Radweg-Umweg"})  # ~707 m
    b.write(str(d / "a.osm"))
    return build_graph(read_osm(str(d / "a.osm")))


def test_alternatives_are_distinct_and_best_first(net):
    r = Router(net)
    alts = r.alternatives([to_ll(0, 0), to_ll(2000, 0)], "trekking", Options(), n=2)
    assert len(alts) == 3
    names = [{net.names[int(net.e_name[p.edge])] for l in a.legs for p in l.pieces} for a in alts]
    assert names[0] == {"Direkt"}
    assert {"Nord"} in names and {"Süd"} in names
    assert alts[0].cost <= min(a.cost for a in alts[1:])


def test_alternatives_drop_much_worse_routes(net):
    r = Router(net)
    alts = r.alternatives([to_ll(0, 0), to_ll(2000, 0)], "trekking", Options(), n=3, max_extra=0.02)
    assert len(alts) == 1  # Nord (+4,4 %) und Süd (+16,6 %) sind teurer als 2 %


def test_calm_slider_controls_residential_streets(net):
    r = Router(net)
    p = [to_ll(0, -5000), to_ll(500, -5000)]
    egal = r.route(p, "trekking", Options(calm=0.0, avoid_roads=1.0))
    normal = r.route(p, "trekking", Options(calm=1.0))
    assert egal.stats["distance_m"] == pytest.approx(500, abs=20)
    assert normal.stats["distance_m"] > 650  # Radweg-Umweg statt Wohnstraße
    assert normal.stats["calm_m"] < 5


def test_labels_and_ranking_prefer_fewer_roads_over_short(net):
    r = Router(net)
    alts = r.alternatives([to_ll(0, 0), to_ll(2000, 0)], "trekking", Options(), n=2)
    labels = label_routes(alts)
    assert labels[0] == "Straßenärmste & kürzeste"
    assert "Alternative" in " ".join(labels)
    assert all(exposure_m(a) == 0 for a in alts)


def test_api_alternatives_and_pick(net):
    c = TestClient(create_app(graph=net, auth=AuthConfig.disabled()))
    body = {"points": [{"lat": to_ll(0, 0)[0], "lon": to_ll(0, 0)[1]}, {"lat": to_ll(2000, 0)[0], "lon": to_ll(2000, 0)[1]}],
            "alternatives": 2, "compare": True}
    d = c.post("/api/route", json=body).json()
    assert len(d["routes"]) == 3 and d["recommended"] == 0
    assert d["routes"][1]["label"].startswith(("Alternative", "Kürzeste", "Straßen"))
    assert d["routes"][0]["stats"]["distance_m"] < d["routes"][2]["stats"]["distance_m"]
    g0 = c.post("/api/gpx", json={**body, "pick": 0}).text
    g2 = c.post("/api/gpx", json={**body, "pick": 2}).text
    assert g0 != g2 and "<trkpt" in g2
    single = c.post("/api/route", json={**body, "alternatives": 0}).json()
    assert "routes" not in single


def test_api_calm_parameter_validated(net):
    c = TestClient(create_app(graph=net, auth=AuthConfig.disabled()))
    pts = [{"lat": to_ll(0, 0)[0], "lon": to_ll(0, 0)[1]}, {"lat": to_ll(2000, 0)[0], "lon": to_ll(2000, 0)[1]}]
    assert c.post("/api/route", json={"points": pts, "calm": 5}).status_code == 422
    assert c.post("/api/route", json={"points": pts, "calm": 2}).status_code == 200
