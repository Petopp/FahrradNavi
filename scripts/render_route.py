"""Rendert eine Route als PNG auf OSM-Kacheln (für Doku/Tests ohne Browser).

Beispiel: python scripts/render_route.py "Starnberg" "Kloster Andechs" -o data/route.png
Hinweis: lädt wenige Kacheln von tile.openstreetmap.org (Nutzungsrichtlinie beachten, nicht für Massenabruf).
"""
from __future__ import annotations

import argparse
import io
import math
import urllib.request

from PIL import Image, ImageDraw, ImageFont

from fahrradnavi.geocoder import Geocoder
from fahrradnavi.graph import Graph
from fahrradnavi.profiles import Options
from fahrradnavi.router import Router

COLORS = {"own": (31, 157, 85), "calm": (224, 161, 0), "road": (214, 60, 60)}
UA = "FahrradNavi-dev/0.1 (route rendering script)"


def tile_xy(lat, lon, z):
    n = 2**z
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--graph", default="data/graph.npz")
    ap.add_argument("--profile", default="trekking")
    ap.add_argument("--avoid", type=float, default=1.0)
    ap.add_argument("-o", "--out", default="data/route.png")
    ap.add_argument("--zoom", type=int, default=13)
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

    pts = [res(a.start), res(a.end)]
    route = r.route(pts, a.profile, Options(avoid_roads=a.avoid), compare=True)
    st = route.stats

    lats = [c[1] for c in route.coords]
    lons = [c[0] for c in route.coords]
    z = a.zoom
    x0, y1 = tile_xy(min(lats), min(lons), z)
    x1, y0 = tile_xy(max(lats), max(lons), z)
    tx0, tx1, ty0, ty1 = int(x0) - 1, int(x1) + 1, int(y0) - 1, int(y1) + 1
    W, H = (tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256
    img = Image.new("RGB", (W, H), (221, 221, 221))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            req = urllib.request.Request(f"https://tile.openstreetmap.org/{z}/{tx}/{ty}.png", headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    img.paste(Image.open(io.BytesIO(resp.read())).convert("RGB"), ((tx - tx0) * 256, (ty - ty0) * 256))
            except Exception as e:  # Kachel fehlt -> grau lassen
                print("Kachel fehlt:", tx, ty, e)

    def px(lon, lat):
        x, y = tile_xy(lat, lon, z)
        return (x - tx0) * 256, (y - ty0) * 256

    # Auf Route zuschneiden (mit Rand)
    S = 3  # Supersampling für glatte Linien
    big = img.resize((W * S, H * S), Image.LANCZOS).convert("RGBA")
    ov = Image.new("RGBA", big.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    base = route.stats.get("baseline_coords") or []
    bp = [tuple(v * S for v in px(c[0], c[1])) for c in base]
    for i in range(0, len(bp) - 1, 2):  # gestrichelt
        d.line([bp[i], bp[i + 1]], fill=(90, 95, 110, 230), width=4 * S)
    for seg in route.segments:
        p = [tuple(v * S for v in px(lo, la)) for lo, la in seg["coords"]]
        if len(p) > 1:
            d.line(p, fill=(255, 255, 255, 235), width=9 * S, joint="curve")
    for seg in route.segments:
        p = [tuple(v * S for v in px(lo, la)) for lo, la in seg["coords"]]
        if len(p) > 1:
            d.line(p, fill=COLORS[seg["kind"]] + (255,), width=5 * S, joint="curve")
    for (lat, lon), label, col in ((pts[0], "A", (31, 157, 85)), (pts[1], "B", (214, 60, 60))):
        x, y = px(lon, lat)
        x, y = x * S, y * S
        d.ellipse([x - 13 * S, y - 13 * S, x + 13 * S, y + 13 * S], fill=col + (255,), outline=(255, 255, 255, 255), width=2 * S)
        d.text((x - 4 * S, y - 7 * S), label, fill=(255, 255, 255, 255), font=ImageFont.load_default(size=13 * S))
    big = Image.alpha_composite(big, ov).convert("RGB").resize((W, H), Image.LANCZOS)

    xs = [px(c[0], c[1])[0] for c in route.coords]
    ys = [px(c[0], c[1])[1] for c in route.coords]
    pad = 70
    box = (max(0, int(min(xs)) - pad), max(0, int(min(ys)) - pad), min(W, int(max(xs)) + pad), min(H, int(max(ys)) + pad + 60))
    out = big.crop(box)
    # Legende unten
    dr = ImageDraw.Draw(out)
    f = ImageFont.load_default(size=15)
    txt = (f"{a.start} -> {a.end}  |  {st['distance_m']/1000:.1f} km, {st['duration_s']//60} min, +{st['ascent_m']} m  |  "
           f"Autostrasse {st['road_m']/1000:.1f} km (kuerzeste Route: {st['baseline']['road_m']/1000:.1f} km auf {st['baseline']['distance_m']/1000:.1f} km)")
    dr.rectangle([0, out.height - 54, out.width, out.height], fill=(255, 255, 255))
    dr.text((10, out.height - 50), txt, fill=(30, 35, 32), font=f)
    x = 10
    for label, key in (("eigener Weg/Radweg", "own"), ("ruhige Strasse", "calm"), ("Autostrasse", "road")):
        dr.rectangle([x, out.height - 24, x + 22, out.height - 14], fill=COLORS[key])
        dr.text((x + 28, out.height - 28), label, fill=(60, 65, 62), font=f)
        x += 28 + int(dr.textlength(label, font=f)) + 22
    dr.line([(x, out.height - 19), (x + 26, out.height - 19)], fill=(90, 95, 110), width=3)
    dr.text((x + 32, out.height - 28), "kuerzeste Route", fill=(60, 65, 62), font=f)
    dr.text((out.width - 250, out.height - 28), "© OpenStreetMap-Mitwirkende", fill=(90, 95, 92), font=f)
    out.save(a.out)
    print("gespeichert:", a.out, out.size)


if __name__ == "__main__":
    main()
