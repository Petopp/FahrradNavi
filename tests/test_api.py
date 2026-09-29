import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from fahrradnavi.api import create_app
from fahrradnavi.geocoder import Geocoder, normalize
from fahrradnavi.graph import Graph
from fahrradnavi.gpx import route_to_gpx

from fixture import POINTS


@pytest.fixture(scope="module")
def client(graph):
    return TestClient(create_app(graph=graph))


def body(a, b, **kw):
    la, lo = POINTS[a]
    lb, lob = POINTS[b]
    return {"points": [{"lat": la, "lon": lo}, {"lat": lb, "lon": lob}], **kw}


def test_config(client):
    r = client.get("/api/config").json()
    assert {p["id"] for p in r["profiles"]} >= {"trekking", "road", "gravel", "ebike"}
    assert r["has_elevation"] is True


def test_route_endpoint(client):
    r = client.post("/api/route", json=body("S1", "T1"))
    assert r.status_code == 200
    d = r.json()
    assert d["stats"]["road_m"] < 1
    assert d["stats"]["detour_m"] > 2900
    assert d["baseline"]["type"] == "LineString"
    assert d["segments"]["features"][0]["properties"]["kind"] == "own"
    assert len(d["coordinates"][0]) == 3
    assert d["elevation"]


def test_route_endpoint_options(client):
    r = client.post("/api/route", json=body("S1", "T1", avoid_roads=0.0)).json()
    assert r["stats"]["distance_m"] < 4100
    assert "baseline" not in r  # ohne Meidung kein Vergleich nötig


def test_route_errors(client):
    assert client.post("/api/route", json=body("S1", "T1", profile="zeppelin")).status_code == 400
    assert client.post("/api/route", json=body("S1", "S2")).status_code == 422
    assert client.post("/api/route", json={"points": [{"lat": 1, "lon": 1}]}).status_code == 422
    far = {"points": [{"lat": 47.95, "lon": 11.3}, {"lat": 10.0, "lon": 10.0}]}
    r = client.post("/api/route", json=far)
    assert r.status_code == 422 and "km" in r.json()["detail"]


def test_gpx_endpoint(client):
    r = client.post("/api/gpx", json=body("S1", "T1"))
    assert r.status_code == 200
    assert "gpx" in r.headers["content-type"]
    root = ET.fromstring(r.text)
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    pts = root.findall(".//g:trkpt", ns)
    assert len(pts) > 50
    assert pts[0].find("g:ele", ns) is not None
    assert len(root.findall("g:wpt", ns)) == 2


def test_gpx_escapes_and_handles_missing_elevation():
    x = route_to_gpx([[11.0, 48.0, None], [11.1, 48.1, 500.0]], name="A & B <c>")
    root = ET.fromstring(x)  # muss wohlgeformt sein
    assert "A &amp; B" in x
    assert len(root.findall(".//{http://www.topografix.com/GPX/1/1}trkpt")) == 2


def test_geocoder_normalization_and_ranking():
    assert normalize("Schlößchen Größe") == "schloesschen groesse"
    places = [
        ["Andechs", "village", 8, 47.97, 11.18],
        ["Kloster Andechs", "monastery", 6, 47.975, 11.183],
        ["Andechser Straße", "street", 0, 48.0, 11.3],
        ["Starnberg", "town", 9, 48.0, 11.34],
    ]
    g = Geocoder(places)
    names = [r["name"] for r in g.search("andechs")]
    assert names[0] == "Andechs" and "Kloster Andechs" in names and "Starnberg" not in names
    assert g.search("kloster andechs")[0]["name"] == "Kloster Andechs"
    assert g.search("starnb")[0]["name"] == "Starnberg"
    assert g.search("gibtesnicht") == []


def test_geocode_endpoint(client):
    r = client.get("/api/geocode", params={"q": "hauptstr"})
    assert r.status_code == 200
    assert any(x["name"] == "Hauptstraße" for x in r.json())


def test_save_load_roundtrip(graph, tmp_path):
    p = str(tmp_path / "g.npz")
    graph.save(p)
    g2 = Graph.load(p)
    assert g2.n_edges == graph.n_edges and g2.names == graph.names and g2.places == graph.places
    from fahrradnavi.router import Router
    from fahrradnavi.profiles import Options

    a = Router(graph).route([POINTS["S1"], POINTS["T1"]], "trekking", Options())
    b = Router(g2).route([POINTS["S1"], POINTS["T1"]], "trekking", Options())
    assert a.stats["distance_m"] == b.stats["distance_m"]
