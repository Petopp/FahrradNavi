import pytest

from fahrradnavi import explain
from fahrradnavi.profiles import Options

from fixture import POINTS, to_ll


def test_explain_rows_add_up_to_route_cost(router):
    """Die Kostenaufschlüsselung muss mit den Routenkosten und -längen übereinstimmen."""
    res = router.route([POINTS["S1"], POINTS["T1"]], "trekking", Options())
    rows = explain.explain(router, res)
    assert rows
    assert sum(r.total_cost for r in rows) == pytest.approx(res.cost, rel=0.02)
    assert sum(r.length_m for r in rows) == pytest.approx(res.stats["distance_m"], rel=0.02)


def test_compare_free_vs_forced_via(router):
    """Wer die Hauptstraße erzwingt (Zwischenpunkt darauf), zahlt einen deutlichen Aufpreis."""
    on_primary = to_ll(2000, 0)
    out = explain.compare(router, [POINTS["S1"], POINTS["T1"]], [POINTS["S1"], on_primary, POINTS["T1"]])
    assert out["extra_pct"] > 100
    worst = max(explain.merge_rows(out["via_rows"]), key=lambda r: r.excess_cost)
    assert worst.name == "Hauptstraße" and worst.road_penalty > 100


def test_merge_rows_combines_same_street(router):
    res = router.route([POINTS["S1"], POINTS["T1"]], "trekking", Options())
    rows = explain.explain(router, res)
    merged = explain.merge_rows(rows)
    assert len(merged) <= len(rows)
    assert sum(r.length_m for r in merged) == pytest.approx(sum(r.length_m for r in rows))
    assert "SUMME" in explain.format_rows(rows)
