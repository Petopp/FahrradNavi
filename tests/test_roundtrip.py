"""Rundreisen: Schleifen mit Wunschlänge, Richtung, Überlappung, Overlays, API."""

import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fahrradnavi.api import create_app
from fahrradnavi.auth import AuthConfig
from fahrradnavi.graph import build_graph
from fahrradnavi.importer import read_osm
from fahrradnavi.overlays import AvoidArea, Overlays
from fahrradnavi.profiles import Options
from fahrradnavi.router import NoRouteError, Router

from fixture import OsmBuilder, to_ll

CYCLE = {"highway": "cycleway", "surface": "asphalt"}
N, STEP = 21, 500.0  # 21x21 Gitter, 500 m Raster -> 10 km x 10 km
CENTER = to_ll(5000, 5000)


@pytest.fixture(scope="module")
def grid(tmp_path_factory):
    d = tmp_path_factory.mktemp("grid")
    b = OsmBuilder()
    for i in range(N):
        b.way([(0, i * STEP), ((N - 1) * STEP, i * STEP)], CYCLE, step=STEP)
        b.way([(i * STEP, 0), (i * STEP, (N - 1) * STEP)], CYCLE, step=STEP)
    b.write(str(d / "g.osm"))
    return build_graph(read_osm(str(d / "g.osm")))


def edge_set(r, res):
    return set(r._edges(res))


def test_round_trips_have_target_length_and_close_the_loop(grid):
    r = Router(grid)
    loops = r.round_trips(CENTER, 12_000, "trekking", Options(), n=3)
    assert 1 <= len(loops) <= 3
    for c in loops:
        assert abs(c.stats["distance_m"] - 12_000) / 12_000 < 0.35
        assert c.coords[0][:2] == pytest.approx(c.coords[-1][:2], abs=1e-6)  # Start = Ziel
        assert c.stats["roundtrip"]["overlap"] < 0.35
    best = min(abs(c.stats["distance_m"] - 12_000) for c in loops)
    assert best / 12_000 < 0.15


def test_round_trips_are_distinct(grid):
    r = Router(grid)
    loops = r.round_trips(CENTER, 12_000, "trekking", Options(), n=3)
    assert len(loops) >= 2
    for i in range(len(loops)):
        for j in range(i + 1, len(loops)):
            a, b = edge_set(r, loops[i]), edge_set(r, loops[j])
            assert len(a & b) / len(a) < 0.6


def test_heading_places_loop_in_that_direction(grid):
    r = Router(grid)
    north = r.round_trips(CENTER, 12_000, "trekking", Options(), n=1, heading=0)[0]
    south = r.round_trips(CENTER, 12_000, "trekking", Options(), n=1, heading=180)[0]
    lat_c = CENTER[0]
    mean_lat = lambda c: np.mean([p[1] for p in c.coords])
    assert mean_lat(north) > lat_c + 0.005 and mean_lat(south) < lat_c - 0.005


def test_round_trip_avoids_overlay_area(grid):
    """Ein gemiedener Bereich nördlich des Starts: die Schleife weicht aus (Richtung beliebig)."""
    r = Router(grid)
    la, lo = to_ll(5000, 7500)
    area = Overlays((AvoidArea("circle", 1.0, la, lo, 1200),))
    free = r.round_trips(CENTER, 12_000, "trekking", Options(), n=1, heading=0)[0]
    avoid = r.round_trips(CENTER, 12_000, "trekking", Options(), overlays=area, n=1, heading=0)[0]

    def inside(c):
        return sum(1 for p in c.coords if math.hypot((p[0] - lo) * 76_000, (p[1] - la) * 110_574) < 1200)

    assert inside(avoid) < inside(free)


def test_round_trip_errors(grid):
    r = Router(grid)
    with pytest.raises(NoRouteError):
        r.round_trips(CENTER, 300_000, "trekking", Options())  # viel größer als das Gebiet
    with pytest.raises(NoRouteError):
        r.round_trips((10.0, 10.0), 12_000, "trekking", Options())  # Start weit außerhalb


def test_progressive_routing_penalizes_reuse(grid):
    """Hin-und-zurück auf demselben Weg wird für die Rückfahrt teurer: Punkte A -> B -> A ergeben keine identische Strecke."""
    r = Router(grid)
    A = to_ll(5000, 5000)
    B = to_ll(5000, 8000)
    res, overlap = r._route_progressive([A, B, A], r.costs.__self__ and __import__("fahrradnavi.profiles", fromlist=["x"]).get_profile("trekking"), Options(), None)
    assert overlap < 0.5  # ohne Penalty wäre es 100 %
    assert res.stats["distance_m"] > 6000


# --- API -----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(grid):
    return TestClient(create_app(graph=grid, auth=AuthConfig.disabled()))


def rt_body(km=12, **kw):
    return {"points": [{"lat": CENTER[0], "lon": CENTER[1]}], "roundtrip": {"distance_km": km}, "alternatives": 2, **kw}


def test_api_roundtrip(client):
    r = client.post("/api/route", json=rt_body())
    assert r.status_code == 200
    d = r.json()
    assert d["stats"]["roundtrip"]["target_m"] == 12000
    assert d["coordinates"][0][:2] == pytest.approx(d["coordinates"][-1][:2], abs=1e-6)
    if "routes" in d:
        assert d["recommended"] == 0 and d["routes"][0]["label"] == "Rundreise 1"
        assert all(x["stats"]["roundtrip"] for x in d["routes"])


def test_api_roundtrip_heading_and_overlays(client):
    la, lo = to_ll(5000, 7500)
    d = client.post("/api/route", json=rt_body(roundtrip={"distance_km": 12, "heading": 0},
                                                 avoid_areas=[{"kind": "circle", "lat": la, "lon": lo, "radius_m": 1200}])).json()
    assert d["stats"]["roundtrip"]["heading"] in (0, 325, 35, 290, 70)


def test_api_roundtrip_gpx_pick(client):
    body = rt_body()
    g0 = client.post("/api/gpx", json={**body, "pick": 0})
    g1 = client.post("/api/gpx", json={**body, "pick": 1})
    assert g0.status_code == 200 and "Rundreise" in g0.text and "<trkpt" in g0.text
    assert g1.status_code == 200 and "<trkpt" in g1.text


def test_api_roundtrip_validation(client):
    assert client.post("/api/route", json=rt_body(km=1)).status_code == 422
    assert client.post("/api/route", json=rt_body(km=500)).status_code == 422
    assert client.post("/api/route", json=rt_body(roundtrip={"distance_km": 12, "heading": 400})).status_code == 422
    assert client.post("/api/route", json={"points": [], "roundtrip": {"distance_km": 12}}).status_code == 422
    assert client.post("/api/route", json={"points": [{"lat": 10.0, "lon": 10.0}], "roundtrip": {"distance_km": 12}}).status_code == 422
    assert client.post("/api/route", json={"points": [{"lat": CENTER[0], "lon": CENTER[1]}]}).status_code == 422  # ohne Rundreise/Ziel
