import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fixture import build_scenarios, write_hgt  # noqa: E402

from fahrradnavi.dem import Dem  # noqa: E402
from fahrradnavi.graph import build_graph  # noqa: E402
from fahrradnavi.importer import read_osm  # noqa: E402
from fahrradnavi.router import Router  # noqa: E402


@pytest.fixture(scope="session")
def testdir(tmp_path_factory):
    d = tmp_path_factory.mktemp("fixture")
    build_scenarios().write(str(d / "test.osm"))
    write_hgt(str(d / "dem"))
    return d


@pytest.fixture(scope="session")
def raw(testdir):
    return read_osm(str(testdir / "test.osm"))


@pytest.fixture(scope="session")
def graph(raw, testdir):
    return build_graph(raw, Dem(str(testdir / "dem")))


@pytest.fixture(scope="session")
def graph_flat(raw):
    return build_graph(raw, None)


@pytest.fixture(scope="session")
def router(graph):
    return Router(graph)
