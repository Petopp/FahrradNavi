"""Bereiche zum Meiden und Lieblingswege."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fahrradnavi import overlays as ov
from fahrradnavi.api import create_app
from fahrradnavi.auth import AuthConfig
from fahrradnavi.graph import build_graph
from fahrradnavi.importer import read_osm
from fahrradnavi.overlays import AvoidArea, Favorite, Overlays
from fahrradnavi.profiles import Options
from fahrradnavi.router import Router

from fixture import OsmBuilder, to_ll

CYCLE = {"highway": "cycleway", "surface": "asphalt"}


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    """S(0,0) -> T(3000,0): "Kurz" 3000 m gerade (eine lange Kante) oder "Lang" 3800 m über y=400."""
    d = tmp_path_factory.mktemp("ovl")
    b = OsmBuilder()
    b.way([(0, 0), (3000, 0)], {**CYCLE, "name": "Kurz"}, step=100)
    b.way([(0, 0), (0, 400), (3000, 400), (3000, 0)], {**CYCLE, "name": "Lang"}, step=100)
    b.write(str(d / "o.osm"))
    return build_graph(read_osm(str(d / "o.osm")))


P = [to_ll(0, 0), to_ll(3000, 0)]


def names(net, res):
    return {net.names[int(net.e_name[p.edge])] for l in res.legs for p in l.pieces if len(p.lat) > 1}


def circle(x, y, r, strength=1.0):
    la, lo = to_ll(x, y)
    return AvoidArea("circle", strength, la, lo, r)


def test_baseline_takes_short_way(net):
    assert names(net, Router(net).route(P, "trekking", Options())) == {"Kurz"}


def test_avoid_circle_moves_route(net):
    r = Router(net)
    res = r.route(P, "trekking", Options(), overlays=Overlays((circle(1500, 0, 200),)))
    assert names(net, res) == {"Lang"} and res.stats["distance_m"] == pytest.approx(3800, abs=30)


def test_avoid_strength_scales_softly(net):
    r = Router(net)
    weak = r.route(P, "trekking", Options(), overlays=Overlays((circle(1500, 0, 200, strength=0.1),)))
    strong = r.route(P, "trekking", Options(), overlays=Overlays((circle(1500, 0, 200, strength=1.0),)))
    assert names(net, weak) == {"Kurz"} and names(net, strong) == {"Lang"}


def test_avoid_polygon(net):
    la = [to_ll(x, y) for x, y in ((1200, -100), (1800, -100), (1800, 100), (1200, 100))]
    r = Router(net).route(P, "trekking", Options(), overlays=Overlays((AvoidArea("polygon", 1.0, polygon=tuple(la)),)))
    assert names(net, r) == {"Lang"}


def test_area_far_away_or_off_route_has_no_effect(net):
    r = Router(net)
    res = r.route(P, "trekking", Options(), overlays=Overlays((circle(1500, 2000, 300), circle(-5000, -5000, 100))))
    assert names(net, res) == {"Kurz"}


def test_area_on_start_is_not_a_hard_block(net):
    """Liegt der Start im gemiedenen Bereich, wird trotzdem geroutet (weicher Malus)."""
    res = Router(net).route(P, "trekking", Options(), overlays=Overlays((circle(0, 0, 150),)))
    assert res.stats["distance_m"] > 2900


def test_partial_coverage_uses_edge_fraction(net):
    """Der Bereich deckt nur 400 m der 3000-m-Kante ab (Kante hat 30 Stützpunkte): Malus wirkt anteilig."""
    idx = ov.SegmentIndex(net)
    m = ov.edge_multipliers(idx, Overlays((circle(1500, 0, 200),)))
    names_ = {n: i for i, n in enumerate(net.names)}
    e = next(e for e in range(net.n_edges) if net.e_name[e] == names_["Kurz"])
    assert m[e] == pytest.approx(1 + 49 * 400 / 3000, rel=0.08)
    e_lang = next(e for e in range(net.n_edges) if net.e_name[e] == names_["Lang"])
    assert m[e_lang] == 1.0


def test_favorite_prefers_longer_way(net):
    r = Router(net)
    la, lo = to_ll(0, 400)
    fav_coords = tuple(to_ll(x, y) for x, y in ((0, 0), (0, 400), (3000, 400), (3000, 0)))
    strong = r.route(P, "trekking", Options(), overlays=Overlays(favorites=(Favorite(fav_coords, 1.0),)))
    weak = r.route(P, "trekking", Options(), overlays=Overlays(favorites=(Favorite(fav_coords, 0.1),)))
    assert names(net, strong) == {"Lang"} and names(net, weak) == {"Kurz"}


def test_favorite_needs_to_be_close(net):
    """Ein Lieblingsweg 30 m neben dem Radweg bekommt keinen Bonus (Radius 12 m)."""
    r = Router(net)
    fav = tuple(to_ll(x, 430) for x in (0, 1500, 3000))
    res = r.route(P, "trekking", Options(), overlays=Overlays(favorites=(Favorite(fav, 1.0),)))
    assert names(net, res) == {"Kurz"}


def test_overlays_do_not_change_cached_base_costs(net):
    r = Router(net)
    r.route(P, "trekking", Options(), overlays=Overlays((circle(1500, 0, 200),)))
    assert names(net, r.route(P, "trekking", Options())) == {"Kurz"}


def test_overlay_costs_are_cached(net):
    r = Router(net)
    o = Overlays((circle(1500, 0, 200),))
    prof = __import__("fahrradnavi.profiles", fromlist=["x"]).get_profile("trekking")
    assert r.effective_costs(prof, Options(), o) is r.effective_costs(prof, Options(), Overlays((circle(1500, 0, 200),)))


def test_alternatives_respect_overlays(net):
    r = Router(net)
    alts = r.alternatives(P, "trekking", Options(), n=2, overlays=Overlays((circle(1500, 0, 200),)), max_extra=5)
    assert names(net, alts[0]) == {"Lang"}


def test_points_in_polygon_concave():
    poly = np.array([[0, 0], [10, 0], [10, 10], [5, 4], [0, 10]], dtype=float)  # "U"-förmig eingedellt
    x = np.array([2.0, 5.0, 8.0, 12.0])
    y = np.array([2.0, 8.0, 5.0, 5.0])  # (5,8) liegt in der Einbuchtung, (8,5) darunter im Polygon
    assert ov.points_in_polygon(x, y, poly).tolist() == [True, False, True, False]


@pytest.mark.parametrize("bad", [
    lambda: AvoidArea("circle", 1.0, 48.0, 11.0, 0.0),
    lambda: AvoidArea("circle", 1.0, 48.0, 11.0, 1e9),
    lambda: AvoidArea("polygon", 1.0, polygon=((48.0, 11.0), (48.1, 11.0))),
    lambda: AvoidArea("kreis", 1.0),
    lambda: AvoidArea("circle", 1.5, 48.0, 11.0, 100.0),
    lambda: Favorite(((48.0, 11.0),)),
    lambda: Overlays(tuple(AvoidArea("circle", 1.0, 48.0, 11.0, 10.0) for _ in range(ov.MAX_AREAS + 1))),
])
def test_invalid_overlays_are_rejected(bad):
    with pytest.raises(ValueError):
        bad()


# --- API --------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(net):
    return TestClient(create_app(graph=net, auth=AuthConfig.disabled()))


def pts_body(**kw):
    return {"points": [{"lat": P[0][0], "lon": P[0][1]}, {"lat": P[1][0], "lon": P[1][1]}], **kw}


def test_api_avoid_area_and_favorite(client):
    base = client.post("/api/route", json=pts_body()).json()["stats"]["distance_m"]
    la, lo = to_ll(1500, 0)
    av = client.post("/api/route", json=pts_body(avoid_areas=[{"kind": "circle", "lat": la, "lon": lo, "radius_m": 200}])).json()
    assert av["stats"]["distance_m"] > base + 700
    poly = [{"lat": a, "lon": b} for a, b in (to_ll(1200, -100), to_ll(1800, -100), to_ll(1800, 100), to_ll(1200, 100))]
    av2 = client.post("/api/route", json=pts_body(avoid_areas=[{"kind": "polygon", "points": poly}])).json()
    assert av2["stats"]["distance_m"] > base + 700
    fav = [{"lat": a, "lon": b} for a, b in (to_ll(0, 0), to_ll(0, 400), to_ll(3000, 400), to_ll(3000, 0))]
    f = client.post("/api/route", json=pts_body(favorites=[{"coords": fav, "strength": 1}])).json()
    assert f["stats"]["distance_m"] > base + 700


def test_api_validation_limits(client):
    la, lo = to_ll(1500, 0)
    ok = {"kind": "circle", "lat": la, "lon": lo, "radius_m": 100}
    assert client.post("/api/route", json=pts_body(avoid_areas=[{**ok, "radius_m": 0}])).status_code == 422
    assert client.post("/api/route", json=pts_body(avoid_areas=[{**ok, "radius_m": 999999}])).status_code == 422
    assert client.post("/api/route", json=pts_body(avoid_areas=[{**ok, "strength": 2}])).status_code == 422
    assert client.post("/api/route", json=pts_body(avoid_areas=[{**ok, "kind": "dreieck"}])).status_code == 422
    assert client.post("/api/route", json=pts_body(avoid_areas=[{"kind": "polygon", "points": [{"lat": 1, "lon": 1}] * 2}])).status_code == 422
    assert client.post("/api/route", json=pts_body(avoid_areas=[ok] * (ov.MAX_AREAS + 1))).status_code == 422
    assert client.post("/api/route", json=pts_body(favorites=[{"coords": [{"lat": 1, "lon": 1}]}])).status_code == 422
    assert client.post("/api/route", json=pts_body(favorites=[{"coords": [{"lat": 1, "lon": 1}] * 3}] * (ov.MAX_FAVORITES + 1))).status_code == 422
    assert client.post("/api/route", json=pts_body(avoid_areas=[ok] * 3)).status_code == 200


def test_api_rejects_huge_bodies(client):
    big = pts_body(favorites=[{"coords": [{"lat": 48.0, "lon": 11.0}] * 19_000}] * 10)  # ~ 5 MB, auch über Punktegrenze
    r = client.post("/api/route", json=big)
    assert r.status_code in (413, 422)


def test_api_loop_and_leg_ends(client):
    body = pts_body()
    body["points"].append({"lat": to_ll(1500, 400)[0], "lon": to_ll(1500, 400)[1]})  # Zwischenpunkt -> zurück zum Start
    d = client.post("/api/route", json={**body, "loop": True}).json()
    assert len(d["leg_ends"]) == 3  # A->B, B->C, C->A
    assert d["coordinates"][0][:2] == pytest.approx(d["coordinates"][-1][:2], abs=1e-4)
    assert d["leg_ends"] == sorted(d["leg_ends"]) and d["leg_ends"][-1] == len(d["coordinates"]) - 1
    assert client.post("/api/route", json={"points": body["points"][:1], "loop": True}).status_code == 422
