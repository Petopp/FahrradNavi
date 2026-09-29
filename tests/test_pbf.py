"""PBF-Import (Standardformat der Geofabrik-Extrakte) muss dasselbe Ergebnis liefern wie XML."""

import osmium

from fahrradnavi.graph import build_graph
from fahrradnavi.importer import _location_store, read_osm


def test_pbf_roundtrip(testdir):
    pbf = str(testdir / "test.osm.pbf")
    with osmium.SimpleWriter(pbf, overwrite=True) as w:
        for o in osmium.FileProcessor(str(testdir / "test.osm")):
            w.add(o)
    a = read_osm(str(testdir / "test.osm"))
    b = read_osm(pbf)
    assert len(a.refs) == len(b.refs) and len(a.way_hwc) == len(b.way_hwc)
    assert (a.vlat == b.vlat).all()
    ga, gb = build_graph(a), build_graph(b)
    assert ga.n_edges == gb.n_edges and abs(float(ga.e_len.sum()) - float(gb.e_len.sum())) < 1


def test_bbox_clip(testdir):
    from fixture import to_ll

    lat, lon = to_ll(2000, 750)
    d = 0.02
    raw = read_osm(str(testdir / "test.osm"), bbox=(lon - d, lat - d, lon + d, lat + d))
    full = read_osm(str(testdir / "test.osm"))
    assert 0 < len(raw.way_hwc) < len(full.way_hwc)


def test_location_store_choice(tmp_path):
    small = tmp_path / "s.osm"
    small.write_text("x")
    assert _location_store(str(small)) == "flex_mem"
    # die Platten-Variante muss von osmium akzeptiert werden
    idx = osmium.index.create_map(f"sparse_file_array,{tmp_path / 'cache.bin'}")
    assert idx is not None
