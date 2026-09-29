"""Kompakter Routing-Graph (numpy) mit Aufbau aus Rohdaten, Speichern und Laden.

Aufbau
------
* Knoten = Kreuzungen/Wegenden ("Junctions"). Wege zwischen Junctions werden zu Kanten
  zusammengefasst (Kontraktion); die Zwischenpunkte bleiben als Geometrie erhalten.
* Jede ungerichtete Kante ``e`` hat zwei Bögen: ``2e`` (u→v) und ``2e+1`` (v→u).
* Alle kostenrelevanten Eigenschaften werden als Merkmale gespeichert; die Kosten werden erst
  zur Abfragezeit je Profil berechnet (siehe costing.py). So lassen sich Profile und
  Schieberegler ändern, ohne den Graphen neu zu bauen.
"""

from __future__ import annotations

import json
import logging
import math
from array import array
from dataclasses import dataclass

import numpy as np

from . import tags as T
from .dem import Dem
from .importer import E7, RawData
from .urban import CenterField, UrbanRaster

log = logging.getLogger(__name__)

FORMAT_VERSION = 3
NO_ELE = -32768  # int16-Marker, Höhe in Dezimetern

ARRAYS = (
    # Knoten
    "node_lat", "node_lon", "node_sig",
    # Kanten
    "e_u", "e_v", "e_len", "e_vs", "e_ve", "e_hwc", "e_fwd", "e_bwd", "e_infra", "e_surf", "e_smooth",
    "e_maxspeed", "e_flags", "e_name", "e_sig", "e_urban", "e_center", "e_up", "e_down", "e_s8u", "e_s8d", "e_s12u", "e_s12d",
    # Vertices (Geometrie)
    "v_lat", "v_lon", "v_ele",
    # Snap-Punkte
    "snap_v", "snap_e", "snap_frac",
)


@dataclass
class Topology:
    """Aus dem Graphen abgeleitete Adjazenz (CSR über Bögen), als array.array für schnellen Python-Zugriff."""

    indptr: array
    order: array  # Bogen-IDs sortiert nach Quellknoten
    dst: array  # Zielknoten je Bogen-ID
    node_lat: array
    node_lon: array


class Graph:
    def __init__(self, arrays: dict[str, np.ndarray], names: list[str], places: list, meta: dict):
        for k in ARRAYS:
            setattr(self, k, arrays[k])
        self.names = names
        self.places = places
        self.meta = meta
        self._topo: Topology | None = None

    # -- Größen ---------------------------------------------------------
    @property
    def n_nodes(self) -> int:
        return len(self.node_lat)

    @property
    def n_edges(self) -> int:
        return len(self.e_u)

    @property
    def has_elevation(self) -> bool:
        return bool(self.meta.get("has_elevation"))

    @property
    def has_urban(self) -> bool:
        return bool(self.meta.get("has_urban"))

    @property
    def has_center(self) -> bool:
        return bool(self.meta.get("has_center"))

    # -- Topologie ------------------------------------------------------
    def topology(self) -> Topology:
        if self._topo is None:
            E = self.n_edges
            src = np.empty(2 * E, dtype=np.int32)
            dst = np.empty(2 * E, dtype=np.int32)
            src[0::2], dst[0::2] = self.e_u, self.e_v
            src[1::2], dst[1::2] = self.e_v, self.e_u
            order = np.argsort(src, kind="stable").astype(np.int32)
            counts = np.bincount(src, minlength=self.n_nodes)
            indptr = np.zeros(self.n_nodes + 1, dtype=np.int32)
            np.cumsum(counts, out=indptr[1:])
            self._topo = Topology(
                indptr=_to_array("i", indptr),
                order=_to_array("i", order),
                dst=_to_array("i", dst),
                node_lat=_to_array("d", self.node_lat / E7),
                node_lon=_to_array("d", self.node_lon / E7),
            )
        return self._topo

    # -- Geometrie ------------------------------------------------------
    def edge_vertices(self, e: int) -> tuple[int, int]:
        return int(self.e_vs[e]), int(self.e_ve[e])

    def edge_polyline(self, e: int, reverse: bool = False) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(lat, lon, ele) der Kantengeometrie; ele in Metern (NaN = unbekannt)."""
        s, t = self.edge_vertices(e)
        sl = slice(s, t + 1)
        lat = self.v_lat[sl] / E7
        lon = self.v_lon[sl] / E7
        ele = self.v_ele[sl].astype(np.float64)
        ele[ele == NO_ELE] = np.nan
        ele /= 10.0
        if reverse:
            return lat[::-1], lon[::-1], ele[::-1]
        return lat, lon, ele

    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lon, min_lat, max_lon, max_lat)"""
        return tuple(self.meta["bbox"])  # type: ignore[return-value]

    # -- Persistenz -----------------------------------------------------
    def save(self, path: str, compress: bool = True) -> None:
        payload = {k: getattr(self, k) for k in ARRAYS}
        payload["names_blob"] = np.frombuffer("\n".join(self.names).encode("utf-8"), dtype=np.uint8)
        payload["places_blob"] = np.frombuffer(json.dumps(self.places, ensure_ascii=False).encode("utf-8"), dtype=np.uint8)
        payload["meta_blob"] = np.frombuffer(json.dumps(self.meta).encode("utf-8"), dtype=np.uint8)
        (np.savez_compressed if compress else np.savez)(path, **payload)

    @classmethod
    def load(cls, path: str) -> "Graph":
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(bytes(z["meta_blob"]).decode("utf-8"))
            if meta.get("format") != FORMAT_VERSION:
                raise ValueError(
                    f"{path}: Graph-Format {meta.get('format')} passt nicht zu dieser Version ({FORMAT_VERSION}); "
                    "bitte mit 'fahrradnavi build' neu bauen."
                )
            arrays = {k: z[k] for k in ARRAYS}
            names = bytes(z["names_blob"]).decode("utf-8").split("\n")
            places = json.loads(bytes(z["places_blob"]).decode("utf-8"))
        return cls(arrays, names, places, meta)


def _to_array(code: str, a: np.ndarray) -> array:
    out = array(code)
    out.frombytes(np.ascontiguousarray(a, dtype={"i": np.int32, "d": np.float64}[code]).tobytes())
    return out


# ---------------------------------------------------------------------------
# Aufbau
# ---------------------------------------------------------------------------


def _segment_lengths(vlat: np.ndarray, vlon: np.ndarray) -> np.ndarray:
    """Länge (m) von Vertex i zu i+1 (Länge N-1). Über Way-Grenzen hinweg bedeutungslos, wird dort nicht genutzt."""
    dlat = np.diff(vlat.astype(np.float64)) / E7
    dlon = np.diff(vlon.astype(np.float64)) / E7
    mlat = np.radians((vlat[:-1].astype(np.float64) + vlat[1:]) / (2 * E7))
    dy = dlat * 110_574.0
    dx = dlon * 111_320.0 * np.cos(mlat)
    return np.hypot(dx, dy)


def build_graph(raw: RawData, dem: Dem | None = None, bbox: tuple | None = None) -> Graph:
    W = len(raw.way_hwc)
    if W == 0:
        raise ValueError("Keine nutzbaren Wege in den Daten gefunden.")
    refs, vlat, vlon = raw.refs, raw.vlat, raw.vlon
    N = len(refs)
    lens = np.diff(raw.way_off)
    way_of_vertex = np.repeat(np.arange(W, dtype=np.int32), lens)

    # --- Junctions bestimmen -------------------------------------------------
    _, inv, counts = np.unique(refs, return_inverse=True, return_counts=True)
    is_boundary = counts[inv] >= 2
    is_boundary[raw.way_off[:-1]] = True
    is_boundary[raw.way_off[1:] - 1] = True
    bp = np.flatnonzero(is_boundary)
    same_way = way_of_vertex[bp[1:]] == way_of_vertex[bp[:-1]]
    vs = bp[:-1][same_way]
    ve = bp[1:][same_way]
    E = len(vs)

    node_refs, first_idx, node_inv = np.unique(refs[bp], return_index=True, return_inverse=True)
    e_u = node_inv[:-1][same_way].astype(np.int32)
    e_v = node_inv[1:][same_way].astype(np.int32)
    node_lat = vlat[bp[first_idx]].astype(np.int32)
    node_lon = vlon[bp[first_idx]].astype(np.int32)
    node_sig = np.isin(node_refs, raw.signal_ids)

    # --- Längen ---------------------------------------------------------------
    seg = _segment_lengths(vlat, vlon)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    e_len = (cum[ve] - cum[vs]).astype(np.float32)

    # Ampeln entlang der Kante (ohne Endpunkte; Endpunkte zählt node_sig)
    vsig = np.isin(refs, raw.signal_ids).astype(np.int32)
    csig = np.cumsum(vsig)
    inner = csig[np.maximum(ve - 1, 0)] - csig[vs]
    e_sig = np.clip(np.where(ve - vs >= 2, inner, 0), 0, 255).astype(np.uint8)

    # --- Höhen ----------------------------------------------------------------
    ele = np.full(N, np.nan, dtype=np.float32)
    have_ele = False
    if dem is not None:
        ele = dem.sample(vlat / E7, vlon / E7)
        have_ele = bool(np.isfinite(ele).any())
        if not have_ele:
            log.warning("Keine Höhendaten für das Gebiet gefunden – Steigungen werden ignoriert.")

    # Inzidenzen: (Kante, Vertex) für alle Vertices jeder Kante
    ccount = ve - vs + 1
    total = int(ccount.sum())
    inc_edge = np.repeat(np.arange(E, dtype=np.int64), ccount)
    starts = np.cumsum(ccount) - ccount
    inc_vertex = np.repeat(vs, ccount) + (np.arange(total, dtype=np.int64) - np.repeat(starts, ccount))
    inc_dist = cum[inc_vertex] - cum[vs][inc_edge]
    last_of_edge = np.r_[inc_edge[1:] != inc_edge[:-1], True]

    e_hwc = raw.way_hwc[way_of_vertex[vs]]
    e_flags = raw.way_flags[way_of_vertex[vs]]

    e_up = np.zeros(E, np.float32)
    e_down = np.zeros(E, np.float32)
    e_s8u = np.zeros(E, np.float32)
    e_s8d = np.zeros(E, np.float32)
    e_s12u = np.zeros(E, np.float32)
    e_s12d = np.zeros(E, np.float32)
    if have_ele:
        inc_ele = ele[inc_vertex].copy()
        bt = (e_flags & (T.FLAG_BRIDGE | T.FLAG_TUNNEL)) != 0
        m = bt[inc_edge] & np.isfinite(ele[vs][inc_edge]) & np.isfinite(ele[ve][inc_edge])
        if m.any():
            ln = np.maximum(e_len[inc_edge[m]], 0.01)
            t = np.clip(inc_dist[m] / ln, 0.0, 1.0)
            e0 = ele[vs][inc_edge[m]]
            e1 = ele[ve][inc_edge[m]]
            inc_ele[m] = e0 + t * (e1 - e0)
            ele[inc_vertex[m]] = inc_ele[m]

        bucket = np.floor(inc_dist / 30.0).astype(np.int64)
        key = inc_edge * (1 << 20) + bucket
        keep = np.r_[True, key[1:] != key[:-1]] | last_of_edge
        k_e, k_d, k_z = inc_edge[keep], inc_dist[keep], inc_ele[keep].astype(np.float64)
        same = k_e[1:] == k_e[:-1]
        dz = k_z[1:] - k_z[:-1]
        dd = k_d[1:] - k_d[:-1]
        ok = same & (dd > 0) & np.isfinite(dz)
        grade = np.where(ok, dz / np.where(dd > 0, dd, 1.0), 0.0)
        ke = k_e[:-1]

        def acc(mask: np.ndarray, w: np.ndarray) -> np.ndarray:
            return np.bincount(ke[mask & ok], weights=w[mask & ok], minlength=E).astype(np.float32)

        up = dz > 0
        dn = dz < 0
        e_up = acc(up, dz)
        e_down = acc(dn, -dz)
        e_s8u = acc(up & (grade >= 0.08), dd)
        e_s8d = acc(dn & (grade <= -0.08), dd)
        e_s12u = acc(up & (grade >= 0.12), dd)
        e_s12d = acc(dn & (grade <= -0.12), dd)

    # --- Ortsbezogene Werte je Kante (0..255): Anteil in Bebauung, Innenstadt-Wert -------------
    def edge_mean(sample, step_m: float) -> np.ndarray:
        """Längengewichteter Mittelwert eines Rasterwerts (0..1) je Kante; Segmente werden fein abgetastet."""
        same_e = inc_edge[1:] == inc_edge[:-1]
        seg_len = np.where(same_e, inc_dist[1:] - inc_dist[:-1], 0.0)
        idx = np.flatnonzero(same_e & (seg_len > 0))
        n = np.maximum(1, np.ceil(seg_len[idx] / step_m)).astype(np.int64)
        rep = np.repeat(idx, n)
        k = np.arange(int(n.sum()), dtype=np.int64) - np.repeat(np.cumsum(n) - n, n)
        t = (k + 0.5) / np.repeat(n, n)
        va, vb = inc_vertex[rep], inc_vertex[rep + 1]
        lat = vlat[va] + t * (vlat[vb].astype(np.float64) - vlat[va])
        lon = vlon[va] + t * (vlon[vb].astype(np.float64) - vlon[va])
        vals = np.asarray(sample(lat, lon), dtype=np.float64)
        tot = np.bincount(inc_edge[rep], weights=vals * (seg_len[rep] / np.repeat(n, n)), minlength=E)
        return np.clip(np.round(tot / np.maximum(e_len.astype(np.float64), 0.01) * 255.0), 0, 255).astype(np.uint8)

    area_bbox = bbox or (float(vlon.min()) / E7, float(vlat.min()) / E7, float(vlon.max()) / E7, float(vlat.max()) / E7)
    e_urban = np.zeros(E, dtype=np.uint8)
    has_urban = bool(raw.urban_rings)
    if has_urban:
        raster = UrbanRaster(raw.urban_rings, area_bbox)
        e_urban = edge_mean(lambda la, lo: raster.contains(la, lo), raster.cell)
    e_center = np.zeros(E, dtype=np.uint8)
    has_center = len(raw.pois) > 0
    if has_center:
        retail = [r for r in raw.urban_rings if len(r) > 2 and r[2] in ("retail", "commercial")]
        field_ = CenterField(raw.pois, area_bbox, retail)
        e_center = edge_mean(field_.sample, field_.cell)

    v_ele = np.full(N, NO_ELE, dtype=np.int16)
    fin = np.isfinite(ele)
    v_ele[fin] = np.clip(np.round(ele[fin] * 10.0), -30000, 32000).astype(np.int16)

    # --- Snap-Punkte (Vertices, ~20 m Abstand) ---------------------------------
    b20 = np.floor(inc_dist / 20.0).astype(np.int64)
    key20 = inc_edge * (1 << 20) + b20
    keep20 = np.r_[True, key20[1:] != key20[:-1]] | last_of_edge
    snap_e = inc_edge[keep20].astype(np.int32)
    snap_v = inc_vertex[keep20].astype(np.int32)
    snap_frac = (inc_dist[keep20] / np.maximum(e_len[snap_e].astype(np.float64), 0.01)).clip(0, 1).astype(np.float32)

    wv = way_of_vertex[vs]
    if bbox is None:
        bbox = (
            float(vlon.min()) / E7,
            float(vlat.min()) / E7,
            float(vlon.max()) / E7,
            float(vlat.max()) / E7,
        )
    arrays = dict(
        node_lat=node_lat, node_lon=node_lon, node_sig=node_sig,
        e_u=e_u, e_v=e_v, e_len=e_len, e_vs=vs.astype(np.int32), e_ve=ve.astype(np.int32),
        e_hwc=e_hwc, e_fwd=raw.way_fwd[wv], e_bwd=raw.way_bwd[wv], e_infra=raw.way_infra[wv],
        e_surf=raw.way_surface[wv], e_smooth=raw.way_smooth[wv], e_maxspeed=raw.way_maxspeed[wv],
        e_flags=e_flags, e_name=raw.way_name[wv], e_sig=e_sig, e_urban=e_urban, e_center=e_center,
        e_up=e_up, e_down=e_down, e_s8u=e_s8u, e_s8d=e_s8d, e_s12u=e_s12u, e_s12d=e_s12d,
        v_lat=vlat, v_lon=vlon, v_ele=v_ele,
        snap_v=snap_v, snap_e=snap_e, snap_frac=snap_frac,
    )
    places = [list(p) for p in raw.places]
    meta = {
        "format": FORMAT_VERSION,
        "bbox": list(bbox),
        "has_elevation": have_ele,
        "has_urban": has_urban,
        "has_center": has_center,
        "n_nodes": int(len(node_lat)),
        "n_edges": int(E),
    }
    log.info("Graph: %d Knoten, %d Kanten, %d Vertices, Höhen: %s, Bebauung: %s, Innenstadt: %s", len(node_lat), E, N, have_ele, has_urban, has_center)
    return Graph(arrays, raw.names, places, meta)
