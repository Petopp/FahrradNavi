"""Routing-Szenarien auf dem synthetischen Testnetz (siehe fixture.py)."""

import numpy as np
import pytest

from fahrradnavi.profiles import Options
from fahrradnavi.router import NoRouteError, Router

from fixture import POINTS


def run(router, a, b, profile="trekking", **opt):
    return router.route([POINTS[a], POINTS[b]], profile, Options(**opt), compare=True)


def test_graph_shape(graph):
    assert graph.n_nodes > 0 and graph.n_edges > 0
    assert graph.has_elevation
    # Kontraktion: viel weniger Kanten als Vertices
    assert graph.n_edges < len(graph.v_lat) / 5


def test_avoids_primary_road_with_big_detour(router):
    """A: 4 km Hauptstraße vs. 7 km Radweg -> Radweg (+3 km)."""
    r = run(router, "S1", "T1")
    assert r.stats["road_m"] < 1.0
    assert 6900 < r.stats["distance_m"] < 7100
    assert 2900 < r.stats["detour_m"] < 3100
    # Standard-Routing (ohne Meidung) wäre direkt über die Hauptstraße
    assert 3900 < r.stats["baseline"]["distance_m"] < 4100
    assert r.stats["baseline"]["road_m"] > 3500


def test_no_avoidance_takes_direct_road(router):
    r = run(router, "S1", "T1", avoid_roads=0.0)
    assert 3900 < r.stats["distance_m"] < 4100


def test_strength_slider_is_monotonic(router):
    """Bei sehr schwacher Meidung (150^0.05 ≈ 1.3) lohnt 3 km Umweg nicht, bei voller schon."""
    weak = run(router, "S1", "T1", avoid_roads=0.05)
    full = run(router, "S1", "T1", avoid_roads=1.0)
    assert weak.stats["distance_m"] < full.stats["distance_m"]
    assert weak.stats["road_m"] > full.stats["road_m"]


def test_unavoidable_road_is_used_and_flagged(router):
    """B: einzige Brücke -> trotz Meidung befahren, aber als Straße markiert."""
    r = run(router, "S2", "T2")
    assert 2350 < r.stats["distance_m"] < 2450
    assert 380 < r.stats["road_m"] < 420
    roads = [s for s in r.segments if s["kind"] == "road"]
    assert roads and roads[0]["name"] == "Brücke"


def test_road_with_separate_cycleway_is_ok(router):
    """C: primary mit cycleway:right=track zählt als eigener Radweg -> direkte Route."""
    r = run(router, "S3", "T3")
    assert 2950 < r.stats["distance_m"] < 3100
    assert r.stats["road_m"] < 1.0
    assert r.stats["detour_m"] == pytest.approx(0, abs=50)


def test_hills_slider(router):
    """D: 3 km über Hügel (+120 m) vs. 6 km flach."""
    normal = run(router, "S4", "T4", "trekking", hills=1.0)
    ignore = run(router, "S4", "T4", "trekking", hills=0.0)
    steep = run(router, "S4", "T4", "trekking", hills=3.0)
    assert ignore.stats["distance_m"] < 3100 and ignore.stats["ascent_m"] > 100
    assert steep.stats["distance_m"] > 5900 and steep.stats["ascent_m"] < 20
    assert normal.stats["ascent_m"] >= steep.stats["ascent_m"]


def test_ebike_climbs_more_readily_than_trekking(router):
    tre = run(router, "S4", "T4", "trekking", hills=3.0)
    eb = run(router, "S4", "T4", "ebike", hills=1.0)
    assert eb.stats["distance_m"] < tre.stats["distance_m"]


def test_surface_preferences(router):
    """E: Schotter (3 km) vs. Asphalt (4.6 km)."""
    road = run(router, "S5", "T5", "road")
    gravel = run(router, "S5", "T5", "gravel")
    assert road.stats["unpaved_m"] < 1.0 and 4500 < road.stats["distance_m"] < 4700
    assert gravel.stats["unpaved_m"] > 2900 and gravel.stats["distance_m"] < 3100


def test_oneway_respected(router):
    """F: Einbahn-Radweg nur in Gegenrichtung -> Umweg über den Rückweg."""
    fwd = router.route([POINTS["T6"], POINTS["S6"]], "trekking", Options())  # T6 (0,-25000) -> S6 (3000): erlaubt
    back = router.route([POINTS["S6"], POINTS["T6"]], "trekking", Options())
    assert fwd.stats["distance_m"] < 3100
    assert back.stats["distance_m"] > 4900


def test_via_point(router):
    r = router.route([POINTS["S1"], POINTS["T1"], POINTS["S1"]], "trekking", Options())
    assert len(r.legs) == 2
    assert 13900 < r.stats["distance_m"] < 14100


def test_route_geometry_is_continuous(router):
    r = run(router, "S1", "T1")
    xs = np.array([[c[0], c[1]] for c in r.coords])
    steps = np.hypot(np.diff(xs[:, 0]) * 76_000, np.diff(xs[:, 1]) * 110_574)
    assert steps.max() < 150  # keine Sprünge (Zwischenknoten alle 100 m)
    # Segmente ergeben zusammen die Gesamtlänge
    assert sum(s["length_m"] for s in r.segments) == pytest.approx(r.stats["distance_m"], abs=1.0)


def test_unreachable_point_far_away(router):
    with pytest.raises(NoRouteError):
        router.route([POINTS["S1"], (48.5, 12.5)], "trekking", Options())


def test_no_route_between_components(router):
    with pytest.raises(NoRouteError):
        router.route([POINTS["S1"], POINTS["S2"]], "trekking", Options())


def test_start_and_end_on_same_edge(router):
    from fixture import to_ll

    r = router.route([to_ll(1000, 0), to_ll(1900, 0)], "trekking", Options(avoid_roads=0))
    assert 850 < r.stats["distance_m"] < 950


def test_snap_prefers_main_network_over_tiny_islands(tmp_path):
    """Kleine isolierte Wegstücke (z. B. private Zufahrten) dürfen den Start nicht blockieren."""
    from fixture import OsmBuilder, to_ll
    from fahrradnavi.importer import read_osm
    from fahrradnavi.graph import build_graph

    b = OsmBuilder()
    # Hauptnetz: Gitter 10x10 aus Radwegen, 100 m Raster
    for i in range(11):
        b.way([(0, i * 100), (1000, i * 100)], {"highway": "cycleway"}, step=100)
        b.way([(i * 100, 0), (i * 100, 1000)], {"highway": "cycleway"}, step=100)
    # Insel (nicht verbunden) direkt neben dem Startpunkt, 5 m entfernt
    b.way([(-205, 500), (-205, 560)], {"highway": "service"}, step=30)
    b.write(str(tmp_path / "i.osm"))
    r = Router(build_graph(read_osm(str(tmp_path / "i.osm"))))
    res = r.route([to_ll(-200, 530), to_ll(1000, 500)], "trekking", Options())
    assert res.stats["distance_m"] > 990  # Route existiert, Start wurde ins Hauptnetz gesnappt
    assert res.stats["snap_distance_m"][0] < 250  # nicht auf die Insel (5 m), sondern ins Gitter
    assert res.coords[0][0] == pytest.approx(to_ll(-200, 530)[1])  # Geometrie beginnt am gewünschten Punkt
