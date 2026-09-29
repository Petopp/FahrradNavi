"""Erklärt eine Route: welche Faktoren bestimmen die Kosten jedes Wegabschnitts?

Nützlich, um zu verstehen, warum der Router eine bestimmte Strecke wählt (oder eine bekannte Route
ablehnt), z. B. ``fahrradnavi explain --from ... --via ... --to ...``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import tags as T
from .costing import KIND_LABELS_DE, road_penalty
from .graph import Graph
from .profiles import Options, Profile, get_profile
from .router import RouteResult, Router


@dataclass
class Row:
    name: str
    highway: str
    surface: str
    kind: str
    length_m: float
    class_factor: float
    road_penalty: float
    surface_factor: float
    route_bonus: float
    hill_cost: float
    total_cost: float  # Meter-Äquivalente
    excess_cost: float  # Kosten über der reinen Länge (Ursache des Umwegs-Aufpreises)
    urban: float = 0.0  # Anteil in Siedlungsfläche 0..1
    center: float = 0.0  # Innenstadt-Wert 0..1

    @property
    def factor(self) -> float:
        return self.total_cost / self.length_m if self.length_m else 0.0


def explain(router: Router, res: RouteResult) -> list[Row]:
    """Kostenaufschlüsselung je Kanten-Stück der Route (Teilstücke anteilig)."""
    g: Graph = router.g
    prof: Profile = res.profile
    opts: Options = res.options
    pen = road_penalty(g, opts)
    cf = np.array(prof.class_factor)
    sf = np.array(prof.surface_factor)
    smf = np.array(prof.smooth_factor)
    rows: list[Row] = []
    costs = router.costs(prof, opts)
    from .router import SURFACE_LABELS

    for leg in res.legs:
        for p in leg.pieces:
            e = p.edge
            length = float(g.e_len[e]) * p.share
            if length <= 0:
                continue
            surf = float(sf[min(int(g.e_surf[e]), 6)]) ** opts.surface * float(smf[min(int(g.e_smooth[e]), 3)]) ** opts.surface
            bonus = opts.route_bonus if int(g.e_flags[e]) & T.FLAG_ROUTE else 1.0
            arc = 2 * e + (1 if p.reverse else 0)
            total = float(costs.cost[arc]) * p.share
            rows.append(
                Row(
                    name=g.names[int(g.e_name[e])] or "(ohne Namen)",
                    highway=T.HW_NAMES.get(int(g.e_hwc[e]), "?"),
                    surface=SURFACE_LABELS.get(int(g.e_surf[e]), "asphalt"),
                    kind=KIND_LABELS_DE[int(costs.kind[e])],
                    length_m=length,
                    class_factor=float(cf[int(g.e_hwc[e])]),
                    road_penalty=float(pen[e]),
                    surface_factor=surf,
                    route_bonus=bonus,
                    hill_cost=total - float(costs.factor[e]) * length,
                    total_cost=total,
                    excess_cost=total - length,
                    urban=float(g.e_urban[e]) / 255.0,
                    center=float(g.e_center[e]) / 255.0,
                )
            )
    return rows


def merge_rows(rows: list[Row]) -> list[Row]:
    """Aufeinanderfolgende Stücke gleicher Straße zusammenfassen."""
    out: list[Row] = []
    for r in rows:
        if out and (out[-1].name, out[-1].highway, out[-1].surface, out[-1].kind) == (r.name, r.highway, r.surface, r.kind):
            o = out[-1]
            o.length_m += r.length_m
            o.total_cost += r.total_cost
            o.excess_cost += r.excess_cost
            o.hill_cost += r.hill_cost
            o.urban = (o.urban * (o.length_m - r.length_m) + r.urban * r.length_m) / max(o.length_m, 1e-9)
            o.center = (o.center * (o.length_m - r.length_m) + r.center * r.length_m) / max(o.length_m, 1e-9)
            o.road_penalty = max(o.road_penalty, r.road_penalty)
        else:
            out.append(Row(**{k: getattr(r, k) for k in Row.__dataclass_fields__}))
    return out


def format_rows(rows: list[Row], min_length: float = 0.0) -> str:
    lines = [f"{'Straße/Weg':30s} {'Typ':12s} {'Belag':10s} {'Art':10s} {'m':>6s} {'Straßen-Malus':>13s} {'Bebauung':>8s} {'Innenst.':>8s} {'Kosten/m':>8s} {'Aufpreis':>9s}"]
    for r in merge_rows(rows):
        if r.length_m < min_length:
            continue
        lines.append(
            f"{r.name[:30]:30s} {r.highway:12s} {r.surface:10s} {r.kind:10s} {r.length_m:6.0f} "
            f"{r.road_penalty:13.1f} {r.urban * 100:7.0f}% {r.center * 100:7.0f}% {r.factor:8.2f} {r.excess_cost:9.0f}"
        )
    tot_len = sum(r.length_m for r in rows)
    tot_cost = sum(r.total_cost for r in rows)
    lines.append(f"{'SUMME':30s} {'':12s} {'':10s} {'':10s} {tot_len:6.0f} {'':13s} {'':8s} {'':8s} {tot_cost / max(tot_len, 1):8.2f} {tot_cost - tot_len:9.0f}")
    return "\n".join(lines)


def compare(router: Router, points_free: list[tuple[float, float]], points_via: list[tuple[float, float]],
            profile: str = "trekking", options: Options | None = None) -> dict:
    """Vergleicht die vom Router gewählte Route mit einer, die durch feste Zwischenpunkte führt."""
    opts = options or Options()
    prof = get_profile(profile)
    free = router.route(points_free, prof, opts)
    via = router.route(points_via, prof, opts)
    return {
        "free": free,
        "via": via,
        "free_rows": explain(router, free),
        "via_rows": explain(router, via),
        "extra_cost": via.cost - free.cost,
        "extra_pct": (via.cost / free.cost - 1.0) * 100.0,
    }
