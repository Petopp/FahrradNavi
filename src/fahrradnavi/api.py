"""FastAPI-Anwendung: JSON-API + statische Weboberfläche."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .geocoder import Geocoder
from .gpx import route_to_gpx
from .graph import Graph
from .profiles import DEFAULT_PROFILE, PROFILES, Options
from .router import NoRouteError, Router, RouteResult

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"

DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende'


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteRequest(BaseModel):
    points: list[Point] = Field(min_length=2, max_length=12)
    profile: str = DEFAULT_PROFILE
    avoid_roads: float = Field(1.0, ge=0.0, le=2.0, description="Stärke des Straßen-Meidens (0 = egal, 1 = konsequent)")
    hills: float = Field(1.0, ge=0.0, le=3.0)
    surface: float = Field(1.0, ge=0.0, le=3.0)
    compare: bool = True

    def options(self) -> Options:
        return Options(avoid_roads=self.avoid_roads, hills=self.hills, surface=self.surface)


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
        "snapped": [{"lat": s.lat, "lon": s.lon} for s in res.snaps],
    }
    if baseline_coords is not None:
        out["baseline"] = {"type": "LineString", "coordinates": [[c[0], c[1]] for c in baseline_coords]}
    return out


def create_app(graph: Graph | None = None, graph_path: str | None = None) -> FastAPI:
    app = FastAPI(title="FahrradNavi", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")

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
    tiles = os.environ.get("FAHRRADNAVI_TILE_URL", DEFAULT_TILES)
    attribution = os.environ.get("FAHRRADNAVI_TILE_ATTRIBUTION", DEFAULT_ATTRIBUTION)

    @app.get("/api/config")
    def config() -> dict:
        min_lon, min_lat, max_lon, max_lat = graph.bbox()
        return {
            "version": __version__,
            "bbox": [min_lon, min_lat, max_lon, max_lat],
            "has_elevation": graph.has_elevation,
            "profiles": [{"id": p.name, "label": p.label} for p in PROFILES.values()],
            "default_profile": DEFAULT_PROFILE,
            "tiles": {"url": tiles, "attribution": attribution},
        }

    @app.get("/api/geocode")
    def geocode(q: str = Query(min_length=2, max_length=100), lat: float | None = None, lon: float | None = None) -> list[dict]:
        near = (lat, lon) if lat is not None and lon is not None else None
        return geocoder.search(q, near=near)

    def _compute(req: RouteRequest) -> RouteResult:
        if req.profile not in PROFILES:
            raise HTTPException(400, f"Unbekanntes Profil {req.profile!r}")
        try:
            return router.route([(p.lat, p.lon) for p in req.points], req.profile, req.options(), compare=req.compare)
        except NoRouteError as e:
            raise HTTPException(422, str(e)) from e

    @app.post("/api/route")
    def route(req: RouteRequest) -> dict:
        return _route_payload(_compute(req))

    @app.post("/api/gpx")
    def gpx(req: RouteRequest) -> Response:
        req.compare = False
        res = _compute(req)
        p0, p1 = res.snaps[0], res.snaps[-1]
        wpts = [(p0.lat, p0.lon, "Start"), (p1.lat, p1.lon, "Ziel")]
        name = f"FahrradNavi {PROFILES[req.profile].label} {res.stats['distance_m'] / 1000:.1f} km"
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
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app
