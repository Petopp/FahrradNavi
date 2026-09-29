"""GPX-Export (Track mit Höhen). Von Komoot, Garmin, Wahoo, OsmAnd, Locus, BRouter u. a. importierbar."""

from __future__ import annotations

from datetime import datetime, timezone
from xml.sax.saxutils import escape


def route_to_gpx(coords: list[list[float]], name: str = "FahrradNavi Route", waypoints: list[tuple[float, float, str]] | None = None) -> str:
    """coords: [[lon, lat, ele|None], ...]"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="FahrradNavi" xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd">',
        f"<metadata><name>{escape(name)}</name><time>{now}</time></metadata>",
    ]
    for lat, lon, label in waypoints or []:
        out.append(f'<wpt lat="{lat:.6f}" lon="{lon:.6f}"><name>{escape(label)}</name></wpt>')
    out.append(f"<trk><name>{escape(name)}</name><trkseg>")
    for c in coords:
        lon, lat = c[0], c[1]
        ele = c[2] if len(c) > 2 else None
        if ele is None:
            out.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"/>')
        else:
            out.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{ele:.1f}</ele></trkpt>')
    out.append("</trkseg></trk></gpx>")
    return "\n".join(out)
