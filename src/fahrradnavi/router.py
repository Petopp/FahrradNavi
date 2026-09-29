"""A*-Router auf dem kontrahierten Graphen inkl. Snapping und Ergebnisaufbereitung."""

from __future__ import annotations

import heapq
import math
from collections import OrderedDict
from dataclasses import dataclass, field, replace

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from . import tags as T
from .costing import INF, KIND_CALM, KIND_OWN, KIND_ROAD, Costs, compute_costs
from .graph import E7, Graph
from .overlays import Overlays, SegmentIndex, edge_multipliers
from .profiles import Options, Profile, get_profile

KIND_LABELS = {KIND_OWN: "own", KIND_CALM: "calm", KIND_ROAD: "road"}
SURFACE_LABELS = {
    T.SURF_SMOOTH: "asphalt", T.SURF_PAVERS: "pflaster", T.SURF_COMPACTED: "verdichtet",
    T.SURF_GRAVEL: "schotter", T.SURF_COBBLES: "kopfstein", T.SURF_ROUGH: "unbefestigt",
}


SPIKE_MAX_M = 400.0  # Rundreisen: längere Hin-und-zurück-Spitzen zu einem Zwischenpunkt werden vermieden


def spike_total_m(coords: list[list[float]], near_m: float = 150.0, min_len: float = 400.0, step: float = 50.0) -> float:
    """Summe der Längen aller "Spitzen" einer Route: Stellen, an denen sie hin und dicht daneben wieder zurück fährt
    (gleicher Weg oder Parallelweg). Punkte werden alle ``step`` Meter entlang der Route verglichen."""
    if len(coords) < 3:
        return 0.0
    c = np.asarray([[p[0], p[1]] for p in coords], dtype=np.float64)
    kx = 111_320.0 * math.cos(math.radians(float(c[0, 1])))
    xy = np.column_stack([c[:, 0] * kx, c[:, 1] * 110_574.0])
    d = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    s_ = np.arange(0.0, d[-1], step)
    x, y = np.interp(s_, d, xy[:, 0]), np.interp(s_, d, xy[:, 1])
    n, total, i = len(s_), 0.0, 1
    while i < n - 1:
        k = 0
        while i - k - 1 >= 0 and i + k + 1 < n and math.hypot(x[i - k - 1] - x[i + k + 1], y[i - k - 1] - y[i + k + 1]) < near_m:
            k += 1
        if k * step >= min_len:
            total += k * step
            i += k
        i += 1
    return total


class NoRouteError(Exception):
    pass


def haversine_m(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (
        math.sin((lat2 - lat1) * p / 2) ** 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    )
    return 12_742_000 * math.asin(math.sqrt(a))


def polyline_length_m(lat: np.ndarray, lon: np.ndarray) -> float:
    if len(lat) < 2:
        return 0.0
    dy = np.diff(lat) * 110_574.0
    dx = np.diff(lon) * 111_320.0 * np.cos(np.radians((lat[:-1] + lat[1:]) / 2))
    return float(np.hypot(dx, dy).sum())


@dataclass
class Snap:
    edge: int
    vertex: int  # globaler Vertex-Index auf der Kante
    frac: float  # Position entlang der Kante 0..1
    lat: float
    lon: float
    distance_m: float


@dataclass
class Piece:
    """Ein (Teil-)Stück einer Kante in Fahrtrichtung."""

    edge: int
    reverse: bool
    lat: np.ndarray
    lon: np.ndarray
    ele: np.ndarray  # m, NaN = unbekannt
    share: float  # Anteil der Kante (für Teilstücke < 1)
    vidx: np.ndarray | None = None  # globale Vertex-Indizes in Fahrtrichtung


@dataclass
class Leg:
    pieces: list[Piece]
    cost: float


@dataclass
class RouteResult:
    legs: list[Leg]
    snaps: list[Snap]
    profile: Profile
    options: Options
    cost: float
    stats: dict = field(default_factory=dict)
    coords: list[list[float]] = field(default_factory=list)  # [lon, lat, ele]
    segments: list[dict] = field(default_factory=list)
    profile_points: list[list[float]] = field(default_factory=list)  # [km, ele]
    leg_ends: list[int] = field(default_factory=list)  # Index in coords, an dem jede Teilstrecke (Leg) endet


class Router:
    def __init__(self, graph: Graph, cache_size: int = 6):
        self.g = graph
        self.topo = graph.topology()
        self._cache: OrderedDict[tuple, Costs] = OrderedDict()
        self._cache_size = cache_size
        min_lon, min_lat, max_lon, max_lat = graph.bbox()
        self._lat0 = (min_lat + max_lat) / 2
        self._kx = 111_320.0 * math.cos(math.radians(self._lat0))
        self._ky = 110_574.0
        sv = graph.snap_v
        pts = np.column_stack([(graph.v_lon[sv] / E7) * self._kx, (graph.v_lat[sv] / E7) * self._ky])
        self._tree = cKDTree(pts)
        self._edge_main = self._main_component_mask()
        self._seg_index: SegmentIndex | None = None
        self._overlay_cache: OrderedDict[tuple, Costs] = OrderedDict()

    def _main_component_mask(self) -> np.ndarray:
        """Kanten in großen zusammenhängenden Netzen (kleine Inseln wie private Zufahrten sind schlechte Snap-Ziele)."""
        g = self.g
        n = g.n_nodes
        m = coo_matrix((np.ones(g.n_edges, dtype=np.int8), (g.e_u, g.e_v)), shape=(n, n))
        _, comp = connected_components(m, directed=False)
        size = np.bincount(comp)
        threshold = min(max(50, 0.001 * size.max()), size.max())
        return (size[comp] >= threshold)[g.e_u]

    # -- Kosten ---------------------------------------------------------
    def costs(self, profile: Profile, opts: Options) -> Costs:
        key = (profile.name, opts.key())
        c = self._cache.get(key)
        if c is not None:
            self._cache.move_to_end(key)
            return c
        c = self._prepare(compute_costs(self.g, profile, opts))
        self._cache[key] = c
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return c

    def _segment_index(self) -> SegmentIndex:
        if self._seg_index is None:
            self._seg_index = SegmentIndex(self.g)
        return self._seg_index

    def effective_costs(self, profile: Profile, opts: Options, overlays: Overlays | None = None) -> Costs:
        """Profilkosten plus Nutzer-Overlays (gemiedene Bereiche, Lieblingswege); Ergebnis wird kurz zwischengespeichert."""
        base = self.costs(profile, opts)
        if overlays is None or overlays.empty():
            return base
        key = (profile.name, opts.key(), overlays)
        hit = self._overlay_cache.get(key)
        if hit is not None:
            self._overlay_cache.move_to_end(key)
            return hit
        m = edge_multipliers(self._segment_index(), overlays)
        c = base.cost.copy()
        c[0::2] *= m
        c[1::2] *= m
        eff = self._prepare(Costs(
            cost=c, min_per_meter=base.min_per_meter * min(1.0, float(m.min())),
            factor=(base.factor * m).astype(np.float32), kind=base.kind,
        ))
        self._overlay_cache[key] = eff
        while len(self._overlay_cache) > 4:
            self._overlay_cache.popitem(last=False)
        return eff

    @staticmethod
    def _prepare(c: Costs) -> Costs:
        """Bögen zusätzlich als array('d') für schnellen Zugriff in der Suche."""
        from array import array

        arr = array("d")
        arr.frombytes(np.ascontiguousarray(c.cost, dtype=np.float64).tobytes())
        c._arr = arr  # type: ignore[attr-defined]
        return c

    # -- Snapping -------------------------------------------------------
    def snap(self, lat: float, lon: float, costs: Costs, max_distance_m: float = 2000.0) -> Snap:
        g = self.g
        k = min(48, len(g.snap_v))
        dist, idx = self._tree.query([lon * self._kx, lat * self._ky], k=k)
        dist = np.atleast_1d(dist)
        idx = np.atleast_1d(idx)
        if dist[0] > max_distance_m:
            raise NoRouteError(
                f"Punkt ({lat:.5f}, {lon:.5f}) liegt {dist[0] / 1000:.1f} km vom nächsten Weg entfernt "
                "bzw. außerhalb des Kartengebiets."
            )
        best = None
        # bevorzugt Kanten im Hauptnetz; nur wenn es in der Nähe keine gibt, auch Inseln
        main = self._edge_main[g.snap_e[idx]]
        if main.any():
            first = float(dist[main][0])
        else:
            first = float(dist[0])
            main = np.ones_like(main)
        for d, i, ok in zip(dist, idx, main):
            if d > first + 25.0:
                break
            if not ok:
                continue
            e = int(g.snap_e[i])
            if not (np.isfinite(costs.cost[2 * e]) or np.isfinite(costs.cost[2 * e + 1])):
                continue
            # bevorzuge in direkter Nähe die Kante mit dem geringsten Malus
            score = float(min(costs.factor[e], 50.0)) * 10.0 + d
            if best is None or score < best[0]:
                best = (score, float(d), int(i))
        if best is None:
            best = (0.0, float(dist[0]), int(idx[0]))
        _, d, i = best
        v = int(g.snap_v[i])
        return Snap(
            edge=int(g.snap_e[i]),
            vertex=v,
            frac=float(g.snap_frac[i]),
            lat=float(g.v_lat[v]) / E7,
            lon=float(g.v_lon[v]) / E7,
            distance_m=d,
        )

    # -- Suche ----------------------------------------------------------
    def _terminals(self, s: Snap, costs: Costs, source: bool) -> dict[int, tuple[float, int]]:
        g = self.g
        e = s.edge
        u, v = int(g.e_u[e]), int(g.e_v[e])
        cf, cb = costs.cost[2 * e], costs.cost[2 * e + 1]
        res: dict[int, tuple[float, int]] = {}

        def put(n: int, c: float, share: float, arc: int) -> None:
            if not math.isfinite(c):
                return  # Richtung gesperrt (Einbahn)
            c *= share
            if n not in res or c < res[n][0]:
                res[n] = (c, arc)

        if source:
            put(v, cf, 1.0 - s.frac, 2 * e)
            put(u, cb, s.frac, 2 * e + 1)
        else:
            put(u, cf, s.frac, 2 * e)
            put(v, cb, 1.0 - s.frac, 2 * e + 1)
        return res

    def _search(self, a: Snap, b: Snap, costs: Costs, max_expansions: int = 50_000_000):
        """Gibt (Kosten, Pfad-Bögen, Start-Bogen, End-Bogen) oder (Kosten, None, None, None) für Direktweg zurück."""
        topo = self.topo
        cost = costs._arr  # type: ignore[attr-defined]
        src = self._terminals(a, costs, True)
        tgt = self._terminals(b, costs, False)

        best = INF
        direct = None
        if a.edge == b.edge:
            e = a.edge
            if a.frac <= b.frac and math.isfinite(costs.cost[2 * e]):
                best = costs.cost[2 * e] * (b.frac - a.frac)
                direct = True
            elif a.frac > b.frac and math.isfinite(costs.cost[2 * e + 1]):
                best = costs.cost[2 * e + 1] * (a.frac - b.frac)
                direct = True

        hm = costs.min_per_meter * 0.95
        glat, glon = b.lat, b.lon
        kx, ky = self._kx, self._ky
        nlat, nlon = topo.node_lat, topo.node_lon
        indptr, order, dst = topo.indptr, topo.order, topo.dst

        def h(n: int) -> float:
            return hm * math.hypot((nlon[n] - glon) * kx, (nlat[n] - glat) * ky)

        dist: dict[int, float] = {}
        pred: dict[int, int] = {}
        heap: list[tuple[float, float, int]] = []
        for n, (c, _arc) in src.items():
            dist[n] = c
            pred[n] = -1
            heap.append((c + h(n), c, n))
        heapq.heapify(heap)

        best_node = -1
        expansions = 0
        pop = heapq.heappop
        push = heapq.heappush
        while heap:
            f, gc, u = pop(heap)
            if f >= best:
                break
            if gc > dist[u]:
                continue
            t = tgt.get(u)
            if t is not None:
                cand = gc + t[0]
                if cand < best:
                    best = cand
                    best_node = u
                    direct = False
            expansions += 1
            if expansions > max_expansions:
                raise NoRouteError("Suche abgebrochen (zu viele Knoten) – Route zu lang oder Graph unzusammenhängend.")
            for k in range(indptr[u], indptr[u + 1]):
                arc = order[k]
                w = cost[arc]
                if w == INF:
                    continue
                ng = gc + w
                v = dst[arc]
                if ng < dist.get(v, INF):
                    dist[v] = ng
                    pred[v] = arc
                    push(heap, (ng + h(v), ng, v))

        if best == INF:
            raise NoRouteError("Keine Verbindung zwischen Start und Ziel gefunden.")
        if direct:
            return best, None, None, None
        arcs: list[int] = []
        n = best_node
        while pred[n] != -1:
            arc = pred[n]
            arcs.append(arc)
            n = int(topo_src(self.g, arc))
        arcs.reverse()
        return best, arcs, src[n][1], tgt[best_node][1]

    # -- Geometrie ------------------------------------------------------
    def _piece(self, arc: int, v_from: int | None = None, v_to: int | None = None, share: float = 1.0) -> Piece:
        """Stück entlang eines Bogens; v_from/v_to = globale Vertex-Indizes (in Fahrtrichtung) für Teilstücke."""
        g = self.g
        e, rev = arc >> 1, bool(arc & 1)
        s, t = g.edge_vertices(e)
        a = v_from if v_from is not None else (t if rev else s)
        b = v_to if v_to is not None else (s if rev else t)
        idx = np.arange(a, b + 1) if a <= b else np.arange(a, b - 1, -1)
        lat = g.v_lat[idx] / E7
        lon = g.v_lon[idx] / E7
        ele = g.v_ele[idx].astype(np.float64)
        ele[ele == -32768] = np.nan
        return Piece(edge=e, reverse=rev, lat=lat, lon=lon, ele=ele / 10.0, share=share, vidx=idx)

    def _leg(self, a: Snap, b: Snap, costs: Costs) -> Leg:
        g = self.g
        total, arcs, a_arc, b_arc = self._search(a, b, costs)
        pieces: list[Piece] = []
        if arcs is None:  # gleiche Kante
            e = a.edge
            rev = a.frac > b.frac
            pieces.append(self._piece(2 * e + int(rev), a.vertex, b.vertex, share=abs(b.frac - a.frac)))
            return Leg(pieces, total)
        # Startstück: vom Snap-Punkt bis zum Knoten
        e = a_arc >> 1
        s, t = g.edge_vertices(e)
        if a_arc & 1:
            pieces.append(self._piece(a_arc, a.vertex, s, share=a.frac))
        else:
            pieces.append(self._piece(a_arc, a.vertex, t, share=1.0 - a.frac))
        for arc in arcs:
            pieces.append(self._piece(arc))
        e = b_arc >> 1
        s, t = g.edge_vertices(e)
        if b_arc & 1:
            pieces.append(self._piece(b_arc, t, b.vertex, share=1.0 - b.frac))
        else:
            pieces.append(self._piece(b_arc, s, b.vertex, share=b.frac))
        # Liegt ein Snap-Punkt genau auf einer Kreuzung, entstehen Stücke ohne Länge auf fremden Kanten
        pieces = [p for p in pieces if len(p.lat) > 1] or pieces[:1]
        return Leg(pieces, total)

    # -- Öffentliche API -------------------------------------------------
    def route(
        self,
        points: list[tuple[float, float]],
        profile: str | Profile = "trekking",
        options: Options | None = None,
        compare: bool = False,
        overlays: Overlays | None = None,
        _search_costs: Costs | None = None,
    ) -> RouteResult:
        """points: [(lat, lon), ...] – Start, optionale Zwischenziele, Ziel.

        ``_search_costs`` (intern): abweichende Kosten nur für die Wegfindung (Alternativen-Suche);
        Auswertung und ``cost`` des Ergebnisses beruhen weiterhin auf den echten Kosten.
        """
        if len(points) < 2:
            raise ValueError("Mindestens Start und Ziel angeben.")
        prof = get_profile(profile) if isinstance(profile, str) else profile
        opts = options or Options()
        costs = self.effective_costs(prof, opts, overlays)
        if _search_costs is not None:
            snaps = [self.snap(la, lo, costs) for la, lo in points]
            legs = [self._leg(snaps[i], snaps[i + 1], _search_costs) for i in range(len(snaps) - 1)]
            for leg in legs:  # echte Kosten
                leg.cost = sum(float(costs.cost[2 * p.edge + int(p.reverse)]) * p.share for p in leg.pieces)
            res = RouteResult(legs=legs, snaps=snaps, profile=prof, options=opts, cost=sum(l.cost for l in legs))
            self._summarize(res, costs, points)
            return res
        snaps = [self.snap(la, lo, costs) for la, lo in points]
        legs = [self._leg(snaps[i], snaps[i + 1], costs) for i in range(len(snaps) - 1)]
        res = RouteResult(legs=legs, snaps=snaps, profile=prof, options=opts, cost=sum(l.cost for l in legs))
        self._summarize(res, costs, points)
        if compare and opts.avoid_roads > 0:
            base_opts = replace(opts, avoid_roads=0.0)
            bcosts = self.costs(prof, base_opts)
            bsnaps = [self.snap(la, lo, bcosts) for la, lo in points]
            blegs = [self._leg(bsnaps[i], bsnaps[i + 1], bcosts) for i in range(len(bsnaps) - 1)]
            bres = RouteResult(legs=blegs, snaps=bsnaps, profile=prof, options=base_opts, cost=0.0)
            # Kinds für die Baseline mit den *strengen* Kosten bewerten (welche Straßen wären das gewesen?)
            self._summarize(bres, costs, points)
            res.stats["baseline"] = {
                "distance_m": bres.stats["distance_m"],
                "duration_s": bres.stats["duration_s"],
                "road_m": bres.stats["road_m"],
                "ascent_m": bres.stats["ascent_m"],
            }
            res.stats["detour_m"] = res.stats["distance_m"] - bres.stats["distance_m"]
            res.stats["baseline_coords"] = bres.coords
        return res

    def alternatives(
        self,
        points: list[tuple[float, float]],
        profile: str | Profile = "trekking",
        options: Options | None = None,
        n: int = 3,
        penalty: float = 3.0,
        max_extra: float = 0.8,
        max_overlap: float = 0.85,
        best: RouteResult | None = None,
        overlays: Overlays | None = None,
    ) -> list[RouteResult]:
        """Sucht bis zu ``n`` deutlich verschiedene Routen (Penalty-Verfahren).

        Nach jeder gefundenen Route werden deren Kanten für die Suche teurer gemacht (``penalty``); die
        Ergebnisse werden mit den echten Kosten bewertet und verworfen, wenn sie mehr als ``max_extra``
        (80 %) teurer sind als die beste Route oder sich zu stark überlappen. Die erste ist die beste.
        """
        prof = get_profile(profile) if isinstance(profile, str) else profile
        opts = options or Options()
        best = best or self.route(points, prof, opts, overlays=overlays)
        results = [best]
        costs = self.effective_costs(prof, opts, overlays)
        used = set(self._edges(best))
        seen = [set(used)]
        for _ in range(n + 3):  # ein paar Versuche mehr, weil Kandidaten verworfen werden
            if len(results) > n:
                break
            c = costs.cost.copy()
            idx = np.fromiter(used, dtype=np.int64)
            for d in (0, 1):
                c[2 * idx + d] *= penalty  # inf bleibt inf
            mod = self._prepare(Costs(cost=c, min_per_meter=costs.min_per_meter, factor=costs.factor, kind=costs.kind))
            try:
                cand = self.route(points, prof, opts, overlays=overlays, _search_costs=mod)
            except NoRouteError:
                break
            edges = set(self._edges(cand))
            used |= edges
            if cand.cost > best.cost * (1.0 + max_extra):
                continue
            if any(len(edges & o) / max(len(edges), 1) > max_overlap for o in seen):
                continue
            seen.append(edges)
            results.append(cand)
        return results[: n + 1]

    @staticmethod
    def _edges(res: RouteResult) -> list[int]:
        return [p.edge for leg in res.legs for p in leg.pieces if len(p.lat) > 1]

    # -- Rundreisen -------------------------------------------------------
    def _snap_soft(self, lat: float, lon: float, toward: tuple[float, float], costs: Costs) -> Snap | None:
        """Snap für Rundreise-Zwischenpunkte: liegt der Punkt im See oder außerhalb der Karte, wird er schrittweise
        Richtung ``toward`` (Start) verschoben. ``None``, wenn auch das nicht hilft."""
        for f in (0.0, 0.3, 0.55, 0.75):
            la, lo = lat + f * (toward[0] - lat), lon + f * (toward[1] - lon)
            try:
                return self.snap(la, lo, costs, max_distance_m=1500.0)
            except NoRouteError:
                continue
        return None

    def _remove_spurs(self, legs: list[Leg], costs: Costs, keep_before: set[int] | None = None) -> tuple[Leg, float]:
        """Entfernt Stichwege (Hin und auf demselben Weg wieder zurück) aus einer Rundreise.

        Die Route wird in einzelne Vertex-Schritte zerlegt; folgt auf einen Schritt A→B auf Kante e direkt B→A auf e,
        heben sich beide auf (Stapelverfahren, auch verschachtelt). Übrig bleibt eine Kante höchstens einmal je Richtung
        in Folge. ``keep_before``: Indizes von Teilstrecken, die an einer Nutzer-Station beginnen – ein Stichweg zu
        dieser Station bleibt erhalten (sie soll ja angefahren werden).
        Rückgabe: (eine Teilstrecke, Überlappungsanteil nach Länge)."""
        g = self.g
        stack: list[tuple[int, int, int]] = []  # (Kante, Vertex von, Vertex nach)
        barrier = (-1, -1, -1)
        for li, leg in enumerate(legs):
            if keep_before and li in keep_before:
                stack.append(barrier)
            for p in leg.pieces:
                v = p.vidx
                if v is None or len(v) < 2:
                    continue
                for a, b in zip(v[:-1].tolist(), v[1:].tolist()):
                    if stack and stack[-1][0] == p.edge and stack[-1][1] == b and stack[-1][2] == a:
                        stack.pop()
                    else:
                        stack.append((p.edge, a, b))
        pieces: list[Piece] = []
        run: list[int] = []
        run_edge, run_fwd = -1, True

        def flush() -> None:
            if len(run) < 2:
                return
            idx = np.array(run, dtype=np.int64)
            ele = g.v_ele[idx].astype(np.float64)
            ele[ele == -32768] = np.nan
            lat, lon = g.v_lat[idx] / E7, g.v_lon[idx] / E7
            share = min(1.0, polyline_length_m(lat, lon) / max(float(g.e_len[run_edge]), 0.01))
            pieces.append(Piece(edge=run_edge, reverse=not run_fwd, lat=lat, lon=lon, ele=ele / 10.0, share=share, vidx=idx))

        for e, a, b in stack:
            if e < 0:  # Sperre
                continue
            fwd = b > a
            if e == run_edge and fwd == run_fwd and run and run[-1] == a:
                run.append(b)
            else:
                flush()
                run, run_edge, run_fwd = [a, b], e, fwd
        flush()
        if not pieces:  # Start liegt direkt am einzigen Weg – nichts entfernen
            pieces = [p for leg in legs for p in leg.pieces]
        cost = sum(float(costs.cost[2 * p.edge + int(p.reverse)]) * p.share for p in pieces)
        # Überlappung: Kanten, die in mehreren getrennten Stücken befahren werden (z. B. Stiel einer "Lollipop"-Schleife)
        count: dict[int, float] = {}
        for p in pieces:
            count[p.edge] = count.get(p.edge, 0.0) + float(g.e_len[p.edge]) * p.share
        runs: dict[int, int] = {}
        for p in pieces:
            runs[p.edge] = runs.get(p.edge, 0) + 1
        rep = sum(l for e, l in count.items() if runs[e] > 1) / 2.0
        tot = sum(count.values())
        return Leg(pieces, cost), rep / max(tot, 1.0)

    def _progressive_legs(self, snaps: list[Snap], costs: Costs, penalty: float) -> list[Leg]:
        """Teilstrecken nacheinander; Kanten früherer Teilstrecken werden für spätere um ``penalty`` teurer."""
        legs: list[Leg] = []
        used: set[int] = set()
        for i in range(len(snaps) - 1):
            use = costs
            if used:
                c = costs.cost.copy()
                idx = np.fromiter(used, dtype=np.int64)
                c[2 * idx] *= penalty
                c[2 * idx + 1] *= penalty
                use = self._prepare(Costs(cost=c, min_per_meter=costs.min_per_meter, factor=costs.factor, kind=costs.kind))
            leg = self._leg(snaps[i], snaps[i + 1], use)
            legs.append(leg)
            used |= {p.edge for p in leg.pieces if len(p.lat) > 1}
        return legs

    def _spike_length(self, into: Leg, out: Leg, step: float = 50.0, near_m: float = 150.0) -> float:
        """Wie weit Hin- und Rückweg an einem Zwischenpunkt dicht beieinander verlaufen (Meter).

        Verglichen werden Punkte gleichen Abstands vor und nach dem Zwischenpunkt; liegen sie näher als ``near_m``,
        gilt das Stück als "Spitze" (hin und auf demselben oder einem Parallelweg zurück)."""
        def xy(leg: Leg) -> np.ndarray:
            lat = np.concatenate([p.lat for p in leg.pieces]) if leg.pieces else np.zeros(0)
            lon = np.concatenate([p.lon for p in leg.pieces]) if leg.pieces else np.zeros(0)
            return np.column_stack([lon * self._kx, lat * self._ky])

        def walk(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            if len(pts) < 2:
                return pts, np.zeros(len(pts))
            return pts, np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])

        a, da = walk(xy(into)[::-1])  # vom Zwischenpunkt rückwärts
        b, db = walk(xy(out))
        if len(a) < 2 or len(b) < 2:
            return 0.0
        spike = 0.0
        d = step
        while d <= min(da[-1], db[-1]):
            pa = np.array([np.interp(d, da, a[:, 0]), np.interp(d, da, a[:, 1])])
            pb = np.array([np.interp(d, db, b[:, 0]), np.interp(d, db, b[:, 1])])
            if np.hypot(*(pa - pb)) > near_m:
                break
            spike = d
            d += step
        return spike

    def _route_progressive(
        self, points: list[tuple[float, float]], prof: Profile, opts: Options, overlays: Overlays | None,
        penalty: float = 2.5, fixed: frozenset[int] = frozenset(),
    ) -> tuple[RouteResult, float]:
        """Rundreise durch ``points`` (Start … Start). Kanten früherer Teilstrecken sind für spätere teurer
        (vermeidet Hin-und-zurück), Stichwege zu erzeugten Zwischenpunkten werden anschließend entfernt.

        ``fixed``: Indizes in ``points``, die Nutzer-Stationen sind – sie werden genau angefahren, nie weggelassen.
        Rückgabe: (Ergebnis mit einer Teilstrecke, Überlappungsanteil 0..1 nach Länge)."""
        costs = self.effective_costs(prof, opts, overlays)
        start = self.snap(points[0][0], points[0][1], costs)
        wps: list[tuple[Snap, bool]] = []
        for k, (la, lo) in enumerate(points[1:-1], start=1):
            if k in fixed:
                wps.append((self.snap(la, lo, costs), True))
            else:
                sn = self._snap_soft(la, lo, points[0], costs)
                if sn is not None:
                    wps.append((sn, False))
        if not wps:
            raise NoRouteError("Keine Wege für die Zwischenpunkte der Rundreise gefunden.")
        legs = self._progressive_legs([start, *[w for w, _ in wps], start], costs, penalty)
        # erzeugte Zwischenpunkte, zu denen die Route als "Spitze" hin und (auf einem Parallelweg) zurück fährt, weglassen
        spiky = [i for i, (_, f) in enumerate(wps) if not f and self._spike_length(legs[i], legs[i + 1]) > SPIKE_MAX_M]
        if spiky and len(spiky) < len(wps):
            wps = [w for i, w in enumerate(wps) if i not in spiky]
            legs = self._progressive_legs([start, *[w for w, _ in wps], start], costs, penalty)
        keep = {i + 1 for i, (_, f) in enumerate(wps) if f}
        leg, overlap = self._remove_spurs(legs, costs, keep_before=keep)
        res = RouteResult(legs=[leg], snaps=[start, start], profile=prof, options=opts, cost=leg.cost)
        self._summarize(res, costs, [points[0], points[0]])
        return res, overlap

    def round_trips(
        self,
        start: tuple[float, float],
        target_m: float,
        profile: str | Profile = "trekking",
        options: Options | None = None,
        overlays: Overlays | None = None,
        n: int = 3,
        heading: float | None = None,
        tolerance: float = 0.12,
        exposure=None,
        stations: list[tuple[float, float]] | None = None,
    ) -> list[RouteResult]:
        """Rundreisen ab ``start`` mit etwa ``target_m`` Länge.

        Zwischenpunkte liegen auf einem Kreis, auf dem auch der Start liegt; der Radius wird iterativ so skaliert, dass die
        tatsächliche Länge zur Wunschlänge passt. Bewertet wird nach Längenabweichung, Überlappung (Hin-und-zurück) und
        Kosten pro Meter; zurückgegeben werden bis zu ``n`` deutlich verschiedene Schleifen, die beste zuerst.
        ``heading``: bevorzugte Richtung (0 = Nord, 90 = Ost) des Rundkurs-Mittelpunkts vom Start aus.
        """
        prof = get_profile(profile) if isinstance(profile, str) else profile
        opts = options or Options()
        if stations:
            return self._round_trips_with_stations(start, list(stations), target_m, prof, opts, overlays, n, tolerance, exposure)
        lat0, lon0 = start
        kx = 111_320.0 * math.cos(math.radians(lat0))
        ky = 110_574.0
        kappa = 1.35  # typisches Verhältnis Radweg-Länge / Luftlinien-Umfang

        if heading is not None:
            dirs = [(heading + off) % 360 for off in (0, -35, 35, -70, 70)]
        else:
            dirs = [d for d in range(0, 360, 60)]
        shapes: list[tuple[float, int, int]] = []  # (Richtung, Anzahl Zwischenpunkte, Umlaufsinn)
        for i, d in enumerate(dirs):
            shapes.append((d, 3 if i % 2 == 0 else 2, 1 if (i // 2) % 2 == 0 else -1))

        cands: list[tuple[RouteResult, float]] = []
        for phi, m, sense in shapes:
            per = (m + 1) * 2 * math.sin(math.pi / (m + 1))
            radius = target_m / (kappa * per)
            best_here: tuple[RouteResult, float] | None = None
            for _ in range(3):
                cx = radius * math.sin(math.radians(phi))
                cy = radius * math.cos(math.radians(phi))
                pts = [start]
                for k in range(1, m + 1):
                    th = math.radians(phi + 180.0 + sense * 360.0 * k / (m + 1))
                    pts.append((lat0 + (cy + radius * math.cos(th)) / ky, lon0 + (cx + radius * math.sin(th)) / kx))
                pts.append(start)
                try:
                    res, ovl = self._route_progressive(pts, prof, opts, overlays)
                except NoRouteError:
                    break
                length = res.stats["distance_m"]
                if best_here is None or abs(length - target_m) < abs(best_here[0].stats["distance_m"] - target_m):
                    best_here = (res, ovl)
                if abs(length - target_m) / target_m <= tolerance:
                    break
                radius *= max(0.5, min(2.0, (target_m / max(length, 1.0)) ** 0.9))
            if best_here is not None:
                res, ovl = best_here
                res.stats["roundtrip"] = {"target_m": round(target_m), "heading": round(phi), "overlap": round(ovl, 3)}
                cands.append(best_here)
        if not cands:
            raise NoRouteError("Für diese Länge und Richtung liegt keine Rundreise im Kartengebiet.")
        return self._pick_round_trips(cands, target_m, n, exposure)


    def _pick_round_trips(
        self, cands: list[tuple[RouteResult, float]], target_m: float, n: int, exposure, max_shared: float = 0.6,
    ) -> list[RouteResult]:
        """Bewertet Rundreise-Kandidaten (Länge, Überlappung, Spitzen, Kosten, Straßenanteil) und wählt bis zu ``n``
        deutlich verschiedene aus."""
        # Varianten, die weit von der Wunschlänge abweichen, nur behalten, wenn es keine besseren gibt
        dev_of = lambda c: abs(c[0].stats["distance_m"] - target_m) / target_m  # noqa: E731
        good = [c for c in cands if dev_of(c) <= 0.3]
        if good:
            cands = good
        else:
            cands = sorted(cands, key=dev_of)[:1]
            cands[0][0].stats["roundtrip"]["note"] = (
                "Die Wunschlänge lässt sich hier nicht erreichen (Kartengebiet, Gewässer oder Wegenetz begrenzen die Schleife)."
            )

        # Bewertung: Länge (wichtig), Überlappung, Kosten pro Meter relativ zur besten Kandidatin
        per_m = [c.cost / max(c.stats["distance_m"], 1.0) for c, _ in cands]
        q0 = min(per_m)
        scored = []
        for (c, ovl), q in zip(cands, per_m):
            dev = abs(c.stats["distance_m"] - target_m) / target_m
            exp_ = exposure(c) / max(c.stats["distance_m"], 1.0) if exposure else 0.0
            spike = spike_total_m(c.coords) / max(c.stats["distance_m"], 1.0)
            c.stats["roundtrip"]["spike_m"] = round(spike * c.stats["distance_m"])
            scored.append((3.0 * dev + 2.0 * ovl + 4.0 * spike + (q / q0 - 1.0) + exp_, c))
        scored.sort(key=lambda t: t[0])
        chosen: list[RouteResult] = []
        # erst Varianten ohne lange Spitzen (> 1 km), nur wenn nötig auch die übrigen
        for allow_spikes in (False, True):
            for _, c in scored:
                if len(chosen) >= n:
                    break
                if any(c is o for o in chosen) or (not allow_spikes and c.stats["roundtrip"].get("spike_m", 0) > 1000):
                    continue
                e = set(self._edges(c))
                if all(len(e & set(self._edges(o))) / max(len(e), 1) < max_shared for o in chosen):
                    chosen.append(c)
        return chosen

    def _round_trips_with_stations(
        self, start, stations, target_m, prof, opts, overlays, n, tolerance, exposure,
    ) -> list[RouteResult]:
        """Rundreise Start → Stationen (in der angegebenen Reihenfolge) → Start mit Wunschlänge.

        Ist die Schleife über die Stationen kürzer als gewünscht, wird auf einem Abschnitt ein Umweg-Punkt seitlich der
        Verbindungslinie eingefügt (verschiedene Abschnitte/Seiten ergeben die Varianten) und so skaliert, dass die Länge
        passt. Ist sie schon länger, wird sie unverändert mit Hinweis zurückgegeben."""
        fixed_pts = [start, *stations, start]
        fixed_idx = frozenset(range(1, len(fixed_pts) - 1))
        base, ovl = self._route_progressive(fixed_pts, prof, opts, overlays, fixed=fixed_idx)
        base_len = base.stats["distance_m"]
        base.stats["roundtrip"] = {"target_m": round(target_m), "heading": None, "overlap": round(ovl, 3),
                                   "stations": len(stations), "min_m": round(base_len)}
        cands: list[tuple[RouteResult, float]] = [(base, ovl)]
        extra = target_m - base_len
        if extra <= tolerance * target_m:
            if base_len > target_m * (1 + tolerance):
                base.stats["roundtrip"]["note"] = (
                    f"Über deine Stationen ist die Rundreise mindestens {base_len / 1000:.1f} km lang."
                )
            return [base]

        kappa = 1.35
        lat0 = start[0]
        kx = 111_320.0 * math.cos(math.radians(lat0))
        ky = 110_574.0
        sections = []
        for i in range(len(fixed_pts) - 1):
            (la1, lo1), (la2, lo2) = fixed_pts[i], fixed_pts[i + 1]
            dx, dy = (lo2 - lo1) * kx, (la2 - la1) * ky
            sections.append((math.hypot(dx, dy), i, dx, dy))
        sections.sort(reverse=True)
        for d, i, dx, dy in sections[:3]:
            (la1, lo1), (la2, lo2) = fixed_pts[i], fixed_pts[i + 1]
            mx, my = (lo1 + lo2) / 2, (la1 + la2) / 2
            if d > 1.0:
                nx_, ny_ = -dy / d, dx / d
            else:  # Abschnitt ohne Länge (Station am Start): Richtung vom Start weg
                nx_, ny_ = 0.0, 1.0
            for side in (1, -1):
                want = extra / kappa  # zusätzliche Luftlinie
                h = math.sqrt(max(((want + d) / 2) ** 2 - (d / 2) ** 2, 1.0))
                best_here: tuple[RouteResult, float] | None = None
                for _ in range(3):
                    wp = (my + side * ny_ * h / ky, mx + side * nx_ * h / kx)
                    pts = fixed_pts[: i + 1] + [wp] + fixed_pts[i + 1:]
                    fixed = frozenset(k if k <= i else k + 1 for k in fixed_idx)
                    try:
                        res, o = self._route_progressive(pts, prof, opts, overlays, fixed=fixed)
                    except NoRouteError:
                        break
                    length = res.stats["distance_m"]
                    if best_here is None or abs(length - target_m) < abs(best_here[0].stats["distance_m"] - target_m):
                        best_here = (res, o)
                    if abs(length - target_m) / target_m <= tolerance:
                        break
                    got = max(length - base_len, 50.0)
                    h *= max(0.4, min(2.5, (extra / got) ** 0.9))
                if best_here is not None:
                    res, o = best_here
                    res.stats["roundtrip"] = {"target_m": round(target_m), "heading": None, "overlap": round(o, 3),
                                              "stations": len(stations), "min_m": round(base_len)}
                    cands.append(best_here)
        # Varianten teilen die Wege zwischen den Stationen – daher mehr Gemeinsamkeit zulassen
        return self._pick_round_trips(cands, target_m, n, exposure, max_shared=0.9)

    # -- Auswertung -----------------------------------------------------
    def _summarize(self, res: RouteResult, costs: Costs, points) -> None:
        g = self.g
        prof = res.profile
        coords: list[list[float]] = []
        segments: list[dict] = []
        dur = 0.0
        asc = desc = 0.0
        kind_len = {KIND_OWN: 0.0, KIND_CALM: 0.0, KIND_ROAD: 0.0}
        unpaved = 0.0
        urban = 0.0
        center = 0.0
        signals = 0
        prof_pts: list[list[float]] = []
        surf_f = np.array(prof.surface_factor)

        cur: dict | None = None
        leg_ends: list[int] = []
        for li, leg in enumerate(res.legs):
            if li:
                leg_ends.append(len(coords) - 1)
            for p in leg.pieces:
                e = p.edge
                length = polyline_length_m(p.lat, p.lon)
                kind = int(costs.kind[e])
                surf = int(g.e_surf[e])
                hwc = int(g.e_hwc[e])
                name = g.names[int(g.e_name[e])]
                fl = int(g.e_flags[e])
                kind_len[kind] += length
                urban += length * float(g.e_urban[e]) / 255.0
                center += length * float(g.e_center[e]) / 255.0
                if surf >= T.SURF_GRAVEL:
                    unpaved += length
                pushing = bool(fl & T.FLAG_DISMOUNT) or hwc == T.HW_FOOTWAY
                speed = 5.0 if pushing else prof.speed_kmh / max(1.0, float(surf_f[min(surf, 6)])) ** 0.6
                dur += length / (speed / 3.6)
                if p.reverse:
                    up, down = float(g.e_down[e]) * p.share, float(g.e_up[e]) * p.share
                else:
                    up, down = float(g.e_up[e]) * p.share, float(g.e_down[e]) * p.share
                asc += up
                desc += down
                dur += up * prof.climb_seconds_per_m
                signals += int(g.e_sig[e]) if p.share >= 1.0 else 0

                key = (kind, name, surf, hwc, li)
                pts = [[float(lo), float(la)] for la, lo in zip(p.lat, p.lon)]
                if cur is not None and cur["key"] == key:
                    cur["coords"].extend(pts[1:] if len(pts) > 1 else [])
                    cur["length_m"] += length
                else:
                    cur = {"key": key, "coords": pts, "length_m": length, "kind": KIND_LABELS[kind],
                           "name": name, "surface": SURFACE_LABELS.get(surf, "asphalt"),
                           "highway": T.HW_NAMES.get(hwc, "")}
                    segments.append(cur)

                for la, lo, el in zip(p.lat, p.lon, p.ele):
                    if coords and coords[-1][0] == float(lo) and coords[-1][1] == float(la):
                        continue
                    coords.append([float(lo), float(la), float(el) if np.isfinite(el) else None])  # type: ignore[list-item]

        leg_ends.append(len(coords) - 1)
        # Anfahrt: gewünschter Start-/Zielpunkt liegt meist neben dem Weg -> gerade Verbindung in die Geometrie
        if coords and points:
            (la0, lo0), (la1, lo1) = points[0], points[-1]
            if res.snaps[0].distance_m > 3.0:
                coords.insert(0, [float(lo0), float(la0), coords[0][2]])
                leg_ends = [x + 1 for x in leg_ends]
            if res.snaps[-1].distance_m > 3.0:
                coords.append([float(lo1), float(la1), coords[-1][2]])

        # Elevationsprofil aus den Koordinaten
        km = 0.0
        prev = None
        for lo, la, el in coords:
            if prev is not None:
                km += haversine_m(prev[1], prev[0], la, lo) / 1000.0
            prev = (lo, la)
            if el is not None:
                prof_pts.append([round(km, 3), round(el, 1)])
        if len(prof_pts) > 400:
            step = len(prof_pts) / 400.0
            prof_pts = [prof_pts[int(i * step)] for i in range(400)] + [prof_pts[-1]]

        for s in segments:
            s.pop("key", None)
            s["length_m"] = round(s["length_m"], 1)

        res.coords = coords
        res.leg_ends = leg_ends
        res.segments = segments
        res.profile_points = prof_pts
        res.stats = {
            "distance_m": round(dist_sum(kind_len), 1),
            "duration_s": round(dur),
            "ascent_m": round(asc),
            "descent_m": round(desc),
            "road_m": round(kind_len[KIND_ROAD], 1),
            "calm_m": round(kind_len[KIND_CALM], 1),
            "own_m": round(kind_len[KIND_OWN], 1),
            "unpaved_m": round(unpaved, 1),
            "urban_m": round(urban, 1),
            "center_m": round(center, 1),
            "signals": signals,
            "has_elevation": g.has_elevation,
            "snap_distance_m": [round(s.distance_m, 1) for s in res.snaps],
        }


def dist_sum(kind_len: dict) -> float:
    return sum(kind_len.values())


def topo_src(g: Graph, arc: int) -> int:
    e = arc >> 1
    return int(g.e_v[e]) if arc & 1 else int(g.e_u[e])
