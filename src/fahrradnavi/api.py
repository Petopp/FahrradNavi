"""FastAPI-Anwendung: JSON-API + statische Weboberfläche."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import __version__, auth as authmod
from .geocoder import Geocoder
from .gpx import route_to_gpx
from .graph import Graph
from . import overlays as ov
from .profiles import DEFAULT_PROFILE, PROFILES, Options
from .router import NoRouteError, Router, RouteResult

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"

MAX_BODY_BYTES = 3_000_000
DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende'


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class AreaIn(BaseModel):
    """Gemiedener Bereich: Kreis (lat/lon/radius_m) oder Polygon (points)."""

    kind: str = Field(pattern="^(circle|polygon)$")
    strength: float = Field(1.0, ge=0.0, le=1.0)
    lat: float = Field(0.0, ge=-90, le=90)
    lon: float = Field(0.0, ge=-180, le=180)
    radius_m: float = Field(0.0, ge=0.0, le=ov.MAX_RADIUS_M)
    points: list[Point] = Field(default_factory=list, max_length=ov.MAX_POLYGON_POINTS)


class FavoriteIn(BaseModel):
    """Lieblingsweg: Linie, z. B. aus einer GPX-Datei."""

    coords: list[Point] = Field(min_length=2, max_length=ov.MAX_FAVORITE_POINTS)
    strength: float = Field(1.0, ge=0.0, le=1.0)


class RoundtripIn(BaseModel):
    distance_km: float = Field(ge=2.0, le=300.0, description="Gewünschte Länge der Rundreise")
    heading: float | None = Field(None, ge=0.0, lt=360.0, description="Bevorzugte Richtung (0 = Nord, 90 = Ost); leer = beliebig")


class RouteRequest(BaseModel):
    points: list[Point] = Field(min_length=1, max_length=14)
    profile: str = DEFAULT_PROFILE
    avoid_roads: float = Field(1.0, ge=0.0, le=2.0, description="Stärke des Straßen-Meidens (0 = egal, 1 = konsequent)")
    calm: float = Field(1.0, ge=0.0, le=3.0, description="Wohnstraßen/Zufahrten zusätzlich meiden (0 = egal)")
    urban: float = Field(0.0, ge=0.0, le=3.0, description="Bebauung meiden (0 = aus)")
    center: float = Field(0.0, ge=0.0, le=3.0, description="Innenstadt meiden (0 = aus)")
    hills: float = Field(1.0, ge=0.0, le=3.0)
    surface: float = Field(1.0, ge=0.0, le=3.0)
    compare: bool = True
    alternatives: int = Field(0, ge=0, le=4, description="Anzahl zusätzlicher Alternativrouten (0 = nur die beste)")
    pick: int = Field(0, ge=0, le=5, description="Nur GPX: Index der gewünschten Route aus der Alternativenliste")
    avoid_areas: list[AreaIn] = Field(default_factory=list, max_length=ov.MAX_AREAS)
    favorites: list[FavoriteIn] = Field(default_factory=list, max_length=ov.MAX_FAVORITES)
    loop: bool = Field(False, description="Zurück zum Start: der erste Punkt wird als Ziel angehängt")
    roundtrip: RoundtripIn | None = Field(
        None, description="Rundreise ab dem ersten Punkt mit gewünschter Länge; weitere Punkte sind Stationen, die angefahren werden")

    @model_validator(mode="after")
    def _check(self) -> "RouteRequest":
        if self.roundtrip is None and len(self.points) < 2:
            raise ValueError("Mindestens Start und Ziel angeben (oder eine Rundreise mit Länge).")
        self.overlays()  # Grenzen prüfen (Radius, Polygonpunkte, Punktezahl)
        return self

    def overlays(self) -> ov.Overlays:
        areas = []
        for a in self.avoid_areas:
            areas.append(ov.AvoidArea(a.kind, a.strength, a.lat, a.lon, a.radius_m, tuple((p.lat, p.lon) for p in a.points)))
        favs = [ov.Favorite(tuple((p.lat, p.lon) for p in f.coords), f.strength) for f in self.favorites]
        return ov.Overlays(tuple(areas), tuple(favs))

    def options(self) -> Options:
        return Options(avoid_roads=self.avoid_roads, calm=self.calm, urban=self.urban, center=self.center, hills=self.hills, surface=self.surface)


# Straßenanteil als Rangkriterium: Autostraße wiegt 4x so schwer wie Wohnstraße. Länge zählt nicht mit –
# "Straßen meiden hat Vorrang vor Kürze".
ROAD_WEIGHT = 4.0


URBAN_WEIGHT = 0.5  # nur wenn der Regler "Bebauung meiden" aktiv ist
CENTER_WEIGHT = 1.0  # nur wenn der Regler "Innenstadt meiden" aktiv ist


def exposure_m(res: RouteResult) -> float:
    e = ROAD_WEIGHT * res.stats["road_m"] + res.stats["calm_m"]
    if res.options.urban > 0:
        e += URBAN_WEIGHT * res.stats["urban_m"]
    if res.options.center > 0:
        e += CENTER_WEIGHT * res.stats["center_m"]
    return e


def label_routes(results: list[RouteResult]) -> list[str]:
    if results and results[0].stats.get("roundtrip"):  # Rundreisen: Reihenfolge = Güte, Länge im Namen
        return [f"Rundreise {i + 1}" for i in range(len(results))]
    best = min(range(len(results)), key=lambda i: (exposure_m(results[i]), results[i].stats["distance_m"]))
    short = min(range(len(results)), key=lambda i: results[i].stats["distance_m"])
    labels = []
    for i in range(len(results)):
        if i == best and i == short:
            labels.append("Straßenärmste & kürzeste")
        elif i == best:
            labels.append("Straßenärmste")
        elif i == short:
            labels.append("Kürzeste")
        else:
            labels.append(f"Alternative {i + 1}")
    return labels


def _route_payload(res: RouteResult) -> dict:
    stats = dict(res.stats)
    baseline_coords = stats.pop("baseline_coords", None)
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": s["coords"]},
            "properties": {k: v for k, v in s.items() if k != "coords"},
        }
        for s in res.segments
    ]
    out = {
        "stats": stats,
        "coordinates": res.coords,
        "segments": {"type": "FeatureCollection", "features": features},
        "elevation": res.profile_points,
        "leg_ends": res.leg_ends,
        "snapped": [{"lat": s.lat, "lon": s.lon} for s in res.snaps],
    }
    if baseline_coords is not None:
        out["baseline"] = {"type": "LineString", "coordinates": [[c[0], c[1]] for c in baseline_coords]}
    return out


def create_app(
    graph: Graph | None = None,
    graph_path: str | None = None,
    auth: authmod.AuthConfig | None = None,
) -> FastAPI:
    """``auth=None`` liest den Zugriffsschutz aus den Umgebungsvariablen (Standard: Passwort erforderlich)."""
    auth = auth if auth is not None else authmod.AuthConfig.from_env()
    docs = os.environ.get("FAHRRADNAVI_DOCS", "").lower() in ("1", "true", "yes", "on")  # Swagger-UI lädt Skripte von einem CDN
    app = FastAPI(
        title="FahrradNavi", version=__version__,
        docs_url="/api/docs" if docs else None, redoc_url=None, openapi_url="/api/openapi.json" if docs else None,
    )

    if graph is None:
        graph_path = graph_path or os.environ.get("FAHRRADNAVI_GRAPH", "data/graph.npz")
        if not os.path.exists(graph_path):
            raise RuntimeError(
                f"Graph-Datei {graph_path} fehlt. Zuerst bauen: fahrradnavi build --help (siehe README)."
            )
        log.info("Lade Graph %s ...", graph_path)
        graph = Graph.load(graph_path)
    router = Router(graph)
    geocoder = Geocoder(graph.places)
    tiles = os.environ.get("FAHRRADNAVI_TILE_URL") or DEFAULT_TILES
    attribution = os.environ.get("FAHRRADNAVI_TILE_ATTRIBUTION") or DEFAULT_ATTRIBUTION
    authmod.install(app, auth, tiles)

    @app.middleware("http")
    async def limit_body(request, call_next):
        # Schutz vor riesigen Eingaben (Lieblingswege aus GPX sind die größten Anfragen)
        if request.method == "POST" and int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Anfrage zu groß"}, status_code=413)
        return await call_next(request)

    @app.get("/api/config")
    def config() -> dict:
        min_lon, min_lat, max_lon, max_lat = graph.bbox()
        return {
            "version": __version__,
            "bbox": [min_lon, min_lat, max_lon, max_lat],
            "has_elevation": graph.has_elevation,
            "has_urban": graph.has_urban,
            "has_center": graph.has_center,
            "auth": auth.enabled,
            "profiles": [{"id": p.name, "label": p.label} for p in PROFILES.values()],
            "default_profile": DEFAULT_PROFILE,
            "tiles": {"url": tiles, "attribution": attribution},
        }

    @app.get("/api/geocode")
    def geocode(q: str = Query(min_length=2, max_length=100), lat: float | None = None, lon: float | None = None) -> list[dict]:
        near = (lat, lon) if lat is not None and lon is not None else None
        return geocoder.search(q, near=near)

    def _compute_all(req: RouteRequest) -> list[RouteResult]:
        """Beste Route (Index 0) plus ggf. Alternativen."""
        if req.profile not in PROFILES:
            raise HTTPException(400, f"Unbekanntes Profil {req.profile!r}")
        pts = [(p.lat, p.lon) for p in req.points]
        if req.loop and len(pts) >= 2:
            pts = pts + [pts[0]]
        overlays = req.overlays()
        try:
            if req.roundtrip is not None:
                return router.round_trips(
                    pts[0], req.roundtrip.distance_km * 1000.0, req.profile, req.options(), overlays,
                    n=max(1, req.alternatives), heading=req.roundtrip.heading, exposure=exposure_m,
                    stations=pts[1:] or None,  # weitere Punkte = Stationen, die die Rundreise anfährt
                )
            best = router.route(pts, req.profile, req.options(), compare=req.compare, overlays=overlays)
            if req.alternatives <= 0:
                return [best]
            return router.alternatives(pts, req.profile, req.options(), n=req.alternatives, best=best, overlays=overlays)
        except NoRouteError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/route")
    def route(req: RouteRequest) -> dict:
        results = _compute_all(req)
        payloads = [_route_payload(r) for r in results]
        base = results[0].stats.get("baseline")
        for r, pl in zip(results, payloads):
            if base:
                pl["stats"]["baseline"] = base
                pl["stats"]["detour_m"] = r.stats["distance_m"] - base["distance_m"]
                pl["baseline"] = payloads[0].get("baseline")
        if len(results) == 1:
            return payloads[0]
        labels = label_routes(results)
        rec = 0 if results[0].stats.get("roundtrip") else min(
            range(len(results)), key=lambda i: (exposure_m(results[i]), results[i].stats["distance_m"]))
        for i, pl in enumerate(payloads):
            pl["label"] = labels[i]
            pl["exposure_m"] = round(exposure_m(results[i]), 1)
        return {**payloads[rec], "routes": payloads, "recommended": rec}

    @app.post("/api/gpx")
    def gpx(req: RouteRequest) -> Response:
        req.compare = False
        results = _compute_all(req)
        res = results[min(req.pick, len(results) - 1)]
        p0, p1 = res.snaps[0], res.snaps[-1]
        wpts = [(p0.lat, p0.lon, "Start"), (p1.lat, p1.lon, "Ziel")]
        kind = "Rundreise" if req.roundtrip is not None or req.loop else PROFILES[req.profile].label
        name = f"FahrradNavi {kind} {res.stats['distance_m'] / 1000:.1f} km"
        body = route_to_gpx(res.coords, name=name, waypoints=wpts)
        return Response(
            body,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": 'attachment; filename="fahrradnavi-route.gpx"'},
        )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "nodes": graph.n_nodes, "edges": graph.n_edges}

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> HTMLResponse:
            # app.js mit Änderungszeit versionieren, damit Browser nach einem Update nicht die alte Datei aus dem Cache nehmen
            v = str(int((WEB_DIR / "app.js").stat().st_mtime))
            return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__V__", v))

    return app
