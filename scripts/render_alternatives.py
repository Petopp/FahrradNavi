"""Zeichnet Alternativrouten (und optional eine Wunschroute durch Zwischenpunkte) auf OSM-Kacheln.

    python scripts/render_alternatives.py "48.0012,11.3455" "Kloster Andechs" --calm 2 --via "48.0066,11.3477" ...
"""
from __future__ import annotations

import argparse
import math
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from PIL import Image, ImageDraw, ImageFont
from render_route import basemap, tile_xy

from fahrradnavi.geocoder import Geocoder
from fahrradnavi.graph import Graph
from fahrradnavi.profiles import Options
from fahrradnavi.router import Router

PALETTE = [(31, 120, 200), (156, 60, 180), (230, 120, 20), (20, 150, 140), (200, 40, 110)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--graph", default="data/graph.npz")
    ap.add_argument("--profile", default="trekking")
    ap.add_argument("--avoid", type=float, default=1.0)
    ap.add_argument("--calm", type=float, default=1.0)
    ap.add_argument("--alts", type=int, default=3)
    ap.add_argument("--via", action="append", default=[], help="Wunschroute durch diese Punkte (schwarz gestrichelt)")
    ap.add_argument("--zoom", type=int, default=14)
    ap.add_argument("-o", "--out", default="data/alternativen.png")
    a = ap.parse_args()

    g = Graph.load(a.graph)
    r = Router(g)
    gc = Geocoder(g.places)

    def res(s):
        try:
            la, lo = map(float, s.split(","))
            return la, lo
        except ValueError:
            h = gc.search(s, limit=1)[0]
            return h["lat"], h["lon"]

    A, B = res(a.start), res(a.end)
    opts = Options(avoid_roads=a.avoid, calm=a.calm)
    routes = r.alternatives([A, B], a.profile, opts, n=a.alts)
    wish = r.route([A] + [res(v) for v in a.via] + [B], a.profile, opts) if a.via else None
    allr = routes + ([wish] if wish else [])
    lats = [c[1] for x in allr for c in x.coords]
    lons = [c[0] for x in allr for c in x.coords]
    z = a.zoom
    img, tx0, ty0, W, H = basemap(lats, lons, z)

    def px(lon, lat):
        x, y = tile_xy(lat, lon, z)
        return (x - tx0) * 256, (y - ty0) * 256

    S = 3
    big = img.resize((W * S, H * S), Image.LANCZOS).convert("RGBA")
    ov = Image.new("RGBA", big.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    f = ImageFont.load_default(size=14 * S)
    for i, x in enumerate(routes):
        p = [tuple(v * S for v in px(c[0], c[1])) for c in x.coords]
        d.line(p, fill=(255, 255, 255, 220), width=9 * S, joint="curve")
        d.line(p, fill=PALETTE[i % len(PALETTE)] + (255,), width=5 * S, joint="curve")
    if wish:
        p = [tuple(v * S for v in px(c[0], c[1])) for c in wish.coords]
        for k in range(0, len(p) - 1, 3):
            d.line(p[k:k + 2], fill=(20, 20, 20, 255), width=4 * S)
    for (lat, lon), lab, col in ((A, "A", (31, 157, 85)), (B, "B", (214, 60, 60))):
        x, y = px(lon, lat)
        x, y = x * S, y * S
        d.ellipse([x - 13 * S, y - 13 * S, x + 13 * S, y + 13 * S], fill=col + (255,), outline=(255, 255, 255, 255), width=2 * S)
        d.text((x - 4 * S, y - 8 * S), lab, fill=(255, 255, 255, 255), font=f)
    big = Image.alpha_composite(big, ov).convert("RGB").resize((W, H), Image.LANCZOS)
    xs = [px(lo, la)[0] for la, lo in zip(lats, lons)]
    ys = [px(lo, la)[1] for la, lo in zip(lats, lons)]
    n_leg = len(routes) + (1 if wish else 0)
    leg_h = 26 * n_leg + 12
    box = (max(0, int(min(xs)) - 60), max(0, int(min(ys)) - 60), min(W, int(max(xs)) + 60), min(H, int(max(ys)) + 60))
    out = big.crop(box)
    canvas = Image.new("RGB", (out.width, out.height + leg_h), (255, 255, 255))
    canvas.paste(out, (0, 0))
    dr = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=15)
    y = out.height + 8
    for i, x in enumerate(routes):
        s = x.stats
        exp = 4 * s["road_m"] + s["calm_m"]
        dr.rectangle([10, y + 4, 40, y + 12], fill=PALETTE[i % len(PALETTE)])
        dr.text((50, y), f"Route {i + 1}: {s['distance_m']/1000:.1f} km | Autostrasse {s['road_m']/1000:.2f} km | Wohnstrasse {s['calm_m']/1000:.2f} km | +{s['ascent_m']} m", fill=(30, 35, 32), font=font)
        y += 26
    if wish:
        s = wish.stats
        dr.line([(10, y + 8), (40, y + 8)], fill=(20, 20, 20), width=3)
        dr.text((50, y), f"Wunschroute: {s['distance_m']/1000:.1f} km | Autostrasse {s['road_m']/1000:.2f} km | Wohnstrasse {s['calm_m']/1000:.2f} km | +{s['ascent_m']} m", fill=(30, 35, 32), font=font)
    dr.text((canvas.width - 250, canvas.height - 22), "© OpenStreetMap-Mitwirkende", fill=(90, 95, 92), font=font)
    canvas.save(a.out)
    print("gespeichert:", a.out, canvas.size)
    for i, x in enumerate(routes):
        print(f"  Route {i + 1}: {x.stats['distance_m']/1000:.1f} km, Autostrasse {x.stats['road_m']/1000:.2f}, Wohnstrasse {x.stats['calm_m']/1000:.2f}")


if __name__ == "__main__":
    main()
