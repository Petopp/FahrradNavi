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


# --- Stichwege (Hin und zurück zu einem Zwischenpunkt) ------------------------------------------------


def uturns(coords):
    c = [(round(x[0], 7), round(x[1], 7)) for x in coords]
    return sum(1 for i in range(1, len(c) - 1) if c[i - 1] == c[i + 1])


@pytest.fixture(scope="module")
def grid_with_spurs(tmp_path_factory):
    """Gitter wie oben, zusätzlich an jedem Knoten ein 200-m-Sackgassen-Stich nach Nordosten (typische Stichwege)."""
    d = tmp_path_factory.mktemp("spurs")
    b = OsmBuilder()
    for i in range(N):
        b.way([(0, i * STEP), ((N - 1) * STEP, i * STEP)], CYCLE, step=STEP)
        b.way([(i * STEP, 0), (i * STEP, (N - 1) * STEP)], CYCLE, step=STEP)
    for i in range(N):
        for j in range(N):
            b.way([(i * STEP, j * STEP), (i * STEP + 140, j * STEP + 140)], {"highway": "track", "name": "Stich"}, step=50)
    b.write(str(d / "s.osm"))
    return build_graph(read_osm(str(d / "s.osm")))


def test_remove_spurs_cuts_out_and_back(grid_with_spurs):
    """Route mit Zwischenpunkt am Ende eines Stichs: danach keine Kehrtwende mehr, Länge um 2x Stich kürzer."""
    r = Router(grid_with_spurs)
    via = to_ll(5000 + 140, 5000 + 140)  # Ende eines Stichs
    res = r.route([to_ll(4000, 5000), via, to_ll(6000, 5000)], "trekking", Options())
    assert uturns(res.coords) >= 1
    leg, overlap = r._remove_spurs(res.legs, r.costs(res.profile, res.options))
    from fahrradnavi.router import RouteResult

    clean = RouteResult(legs=[leg], snaps=res.snaps, profile=res.profile, options=res.options, cost=leg.cost)
    r._summarize(clean, r.costs(res.profile, res.options), [to_ll(4000, 5000), to_ll(6000, 5000)])
    assert uturns(clean.coords) == 0
    assert res.stats["distance_m"] - clean.stats["distance_m"] == pytest.approx(2 * 198, abs=15)
    assert clean.cost < res.cost and overlap == 0


def test_round_trips_have_no_out_and_back_spurs(grid_with_spurs):
    r = Router(grid_with_spurs)
    for km in (8, 12, 16):
        for c in r.round_trips(CENTER, km * 1000, "trekking", Options(), n=3):
            assert uturns(c.coords) == 0
            assert c.coords[0][:2] == pytest.approx(c.coords[-1][:2], abs=1e-6)
            assert len(c.legs) == 1


def test_waypoints_outside_data_are_pulled_in(grid):
    """Wunschlänge größer als das Gebiet: Zwischenpunkte außerhalb werden Richtung Start gezogen statt Fehler."""
    r = Router(grid)
    loops = r.round_trips(to_ll(1000, 1000), 30_000, "trekking", Options(), n=2)  # Start in der Ecke
    assert loops and all(c.coords[0][:2] == pytest.approx(c.coords[-1][:2], abs=1e-6) for c in loops)
    assert loops[0].stats["distance_m"] > 5000


def test_spike_metric():
    from fahrradnavi.router import spike_total_m

    lat0, lon0 = 48.0, 11.0
    m_lat, m_lon = 1 / 110_574.0, 1 / (111_320.0 * math.cos(math.radians(48.0)))
    # Quadrat 1 km: keine Spitze
    sq = [(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)]
    assert spike_total_m([[lon0 + x * m_lon, lat0 + y * m_lat] for x, y in sq]) == 0
    # 800 m hinauf und auf einem Parallelweg 60 m daneben zurück, dann weiter: eine Spitze von ~800 m
    spike = [(0, 0), (1000, 0), (1000, 800), (1060, 800), (1060, 0), (2000, 0), (2000, -1000), (0, -1000), (0, 0)]
    v = spike_total_m([[lon0 + x * m_lon, lat0 + y * m_lat] for x, y in spike])
    assert 650 <= v <= 900


def test_spike_length_detects_parallel_return(tmp_path):
    """Hin auf einem Weg, 60 m daneben auf einem Parallelweg zurück: als Spitze erkannt; eine echte Schleife nicht."""
    b = OsmBuilder()
    b.way([(0, 0), (2000, 0), (2060, 0), (4000, 0)], CYCLE, step=100)
    b.way([(2000, 0), (2000, 1500), (2030, 1500), (2060, 1500), (2060, 0)], CYCLE, step=100)  # Stich mit Parallelweg
    b.way([(0, 0), (0, -2000), (4000, -2000), (4000, 0)], CYCLE, step=200)  # große Schleife südlich
    b.write(str(tmp_path / "p.osm"))
    r = Router(build_graph(read_osm(str(tmp_path / "p.osm"))))
    spiky = r.route([to_ll(0, 0), to_ll(2030, 1500), to_ll(4000, 0)], "trekking", Options())
    assert r._spike_length(spiky.legs[0], spiky.legs[1]) >= 1400
    loop = r.route([to_ll(0, 0), to_ll(2000, -2000), to_ll(4000, 0)], "trekking", Options())
    assert r._spike_length(loop.legs[0], loop.legs[1]) < 400
