"""Kostenmodell: aus Kanten-Merkmalen werden profilabhängige Bogenkosten (vektorisiert)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import tags as T
from .graph import Graph
from .profiles import CAR_PENALTY, CENTER_PENALTY, INFRA_EXPONENT, URBAN_PENALTY, Options, Profile

INF = float("inf")

# Ab dieser effektiven Straßen-Strafe gilt eine Kante im Ergebnis als "Straße" (rot dargestellt).
ROAD_KIND_THRESHOLD = 6.0

KIND_OWN = 0  # eigener Weg / Radweg / baulich getrennt
KIND_CALM = 1  # ruhige Straße
KIND_ROAD = 2  # Autostraße ohne eigenen Radweg
KIND_LABELS_DE = {KIND_OWN: "eigener Weg", KIND_CALM: "ruhig", KIND_ROAD: "Autostraße"}


@dataclass
class Costs:
    cost: np.ndarray  # float64, Länge 2E; INF = nicht befahrbar
    min_per_meter: float  # untere Schranke Kosten/Meter (für A*-Heuristik)
    factor: np.ndarray  # float32 je Kante: effektiver Streckenfaktor (ohne Steigung)
    kind: np.ndarray  # uint8 je Kante: KIND_*


def road_penalty(g: Graph, opts: Options) -> np.ndarray:
    """Effektiver Auto-Malus je Kante (1 = kein Malus)."""
    table = np.ones(T.N_HW, dtype=np.float64)
    for hw, p in CAR_PENALTY.items():
        table[hw] = p
    lp = np.log(table[g.e_hwc])
    infra_exp = np.array([INFRA_EXPONENT[i] for i in range(5)])
    lp = lp * infra_exp[np.minimum(g.e_infra, 4)]
    ms = g.e_maxspeed.astype(np.int32)
    lp = np.where((ms > 0) & (ms <= 30), lp * 0.6, lp)  # Tempo 30: deutlich ruhiger
    lp = np.where(ms >= 80, lp * 1.15, lp)  # Landstraße mit Tempo 80/100
    calm_class = np.isin(g.e_hwc, (T.HW_RESIDENTIAL, T.HW_LIVING_STREET, T.HW_SERVICE))
    pen = np.exp(lp * opts.avoid_roads * np.where(calm_class, opts.calm, 1.0))
    # "Radweg auf der anderen Seite benutzen"-Schild: Straße noch weniger geeignet
    side = (g.e_flags & T.FLAG_SIDEPATH) != 0
    pen = np.where(side & (pen > 1.0), pen * (3.0 ** opts.avoid_roads), pen)
    return pen


def compute_costs(g: Graph, profile: Profile, opts: Options) -> Costs:
    length = g.e_len.astype(np.float64)
    pen = road_penalty(g, opts)

    cf = np.array(profile.class_factor, dtype=np.float64)[g.e_hwc]
    surf = np.array(profile.surface_factor, dtype=np.float64)[np.minimum(g.e_surf, T.N_SURF - 1)]
    smooth = np.array(profile.smooth_factor, dtype=np.float64)[np.minimum(g.e_smooth, 3)]
    # Oberflächen-Abneigung skalieren (Potenz)
    surf = surf ** opts.surface
    smooth = smooth ** opts.surface
    factor = cf * pen * surf * smooth

    if g.has_urban and opts.urban > 0:
        # Anteil der Kante innerhalb von Siedlungsflächen: 0 -> Faktor 1, 1 -> voller Bebauungs-Malus
        factor = factor * (1.0 + (URBAN_PENALTY**opts.urban - 1.0) * (g.e_urban / 255.0))

    if g.has_center and opts.center > 0:
        # Innenstadt-Wert der Kante 0..1 -> Faktor zwischen 1 und dem vollen Innenstadt-Malus
        factor = factor * (1.0 + (CENTER_PENALTY**opts.center - 1.0) * (g.e_center / 255.0))

    route = (g.e_flags & T.FLAG_ROUTE) != 0
    factor = np.where(route, factor * opts.route_bonus, factor)
    # Schieben (Fußweg / "dismount"): mind. Faktor 3
    push = (g.e_flags & T.FLAG_DISMOUNT) != 0
    factor = np.where(push, np.maximum(factor, 6.0), factor)

    sig_cost = opts.signal_cost * g.e_sig.astype(np.float64)
    base = length * factor + sig_cost

    h = opts.hills
    if g.has_elevation and h > 0:
        fwd_hill = h * (
            profile.climb_cost * g.e_up
            + profile.steep8_cost * g.e_s8u
            + profile.steep12_cost * g.e_s12u
            + profile.steep_down_cost * (g.e_s8d + g.e_s12d)
        )
        bwd_hill = h * (
            profile.climb_cost * g.e_down
            + profile.steep8_cost * g.e_s8d
            + profile.steep12_cost * g.e_s12d
            + profile.steep_down_cost * (g.e_s8u + g.e_s12u)
        )
    else:
        fwd_hill = bwd_hill = np.zeros_like(base)

    # Ampel am Zielknoten
    sigp = opts.signal_cost * g.node_sig.astype(np.float64)
    cost = np.empty(2 * len(base), dtype=np.float64)
    cost[0::2] = base + fwd_hill + sigp[g.e_v]
    cost[1::2] = base + bwd_hill + sigp[g.e_u]
    cost[0::2][~g.e_fwd] = INF
    cost[1::2][~g.e_bwd] = INF

    # Art der Kante für die Darstellung
    kind = np.full(len(base), KIND_OWN, dtype=np.uint8)
    car = np.isin(g.e_hwc, T.CAR_CLASSES + (T.HW_LIVING_STREET, T.HW_SERVICE))
    # Straße "ohne Malus" (Strafe fast 1) gilt als eigener Weg, mit kleinem Malus als ruhig
    strict = road_penalty(g, Options(avoid_roads=1.0))
    kind[car & (strict > 1.0001)] = KIND_CALM
    kind[car & (strict >= ROAD_KIND_THRESHOLD)] = KIND_ROAD
    # Wohn-/Spielstraßen und Zufahrten bleiben "ruhig", auch wenn ihr Malus hoch eingestellt wird
    kind[(g.e_hwc == T.HW_LIVING_STREET) | (g.e_hwc == T.HW_SERVICE)] = KIND_CALM
    kind[(g.e_hwc == T.HW_RESIDENTIAL) & (strict > 1.0001) & (g.e_infra < T.INFRA_TRACK)] = KIND_CALM

    # Untere Schranke für Kosten/Meter (Faktor); Ampeln/Steigung addieren nur.
    min_pm = float(min(np.min(factor), 1.0)) if len(factor) else 1.0
    return Costs(cost=cost, min_per_meter=max(min_pm, 0.05), factor=factor.astype(np.float32), kind=kind)
