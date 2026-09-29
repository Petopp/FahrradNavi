import os

import numpy as np
import pytest

from fahrradnavi.dem import Dem, tile_name, tiles_for_bbox
from fahrradnavi import tags as T


def test_tile_names():
    assert tile_name(47, 11) == "N47E011"
    assert tile_name(-1, -3) == "S01W003"
    assert tiles_for_bbox(11.0, 47.7, 11.65, 48.2) == ["N47E011", "N48E011"]


def test_dem_bilinear(tmp_path):
    n = 1201
    z = np.zeros((n, n), dtype=">i2")
    z[:, :] = np.arange(n)[None, :]  # steigt nach Osten: 0..1200 m über 1°
    z.tofile(tmp_path / "N47E011.hgt")
    d = Dem(str(tmp_path))
    v = d.sample(np.array([47.5, 47.5]), np.array([11.0, 11.5]))
    assert v[0] == pytest.approx(0, abs=0.01)
    assert v[1] == pytest.approx(600, abs=0.5)
    # fehlende Kachel -> NaN
    assert np.isnan(d.sample(np.array([49.5]), np.array([11.5]))[0])


def test_dem_void_is_nan(tmp_path):
    z = np.full((1201, 1201), -32768, dtype=">i2")
    z.tofile(tmp_path / "N47E011.hgt")
    assert np.isnan(Dem(str(tmp_path)).sample(np.array([47.5]), np.array([11.5]))[0])


def test_relations_mark_cycle_routes(tmp_path):
    """Ausgeschilderte Radrouten erhalten FLAG_ROUTE und werden leicht bevorzugt."""
    import sys

    sys.path.insert(0, os.path.dirname(__file__))
    from fixture import OsmBuilder
    from fahrradnavi.importer import read_osm
    from fahrradnavi.graph import build_graph

    b = OsmBuilder()
    w1 = b.way([(0, 0), (500, 0)], {"highway": "cycleway"})
    b.way([(500, 0), (1000, 0)], {"highway": "cycleway"})
    b.relations.append((9001, [w1], {"type": "route", "route": "bicycle", "network": "rcn"}))
    b.write(str(tmp_path / "r.osm"))
    raw = read_osm(str(tmp_path / "r.osm"))
    g = build_graph(raw)
    flags = sorted(int(f) & T.FLAG_ROUTE for f in g.e_flags)
    assert flags == [0, T.FLAG_ROUTE]
