"""Browser-Rauchtest der Weboberfläche (Playwright + Chromium) auf synthetischen Testnetzen.

Baut zwei kleine Netze (aus tests/fixture.py), startet den Server im Hintergrund ohne Login und spielt die Bedienung durch:
Route, Kreis/Fläche zum Meiden, Ziehen an der Route (Zwischenziel), "Stelle meiden", GPX-Lieblingsweg, Rundkurs,
Speicherung im Browser, Rundreise.  Aufruf:  python scripts/ui_smoke.py  (benötigt: pip install playwright)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import uvicorn  # noqa: E402
from fixture import OsmBuilder, to_ll  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from fahrradnavi.api import create_app  # noqa: E402
from fahrradnavi.auth import AuthConfig  # noqa: E402
from fahrradnavi.graph import build_graph  # noqa: E402
from fahrradnavi.importer import read_osm  # noqa: E402

CYCLE = {"highway": "cycleway", "surface": "asphalt"}
CHROMIUM = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("  OK   " if ok else "  FAIL ") + name + (f"  ({detail})" if detail else ""))


def make_graph(builder_fn, tmp: str, name: str):
    b = OsmBuilder()
    builder_fn(b)
    path = os.path.join(tmp, name + ".osm")
    b.write(path)
    return build_graph(read_osm(path))


def kurz_lang(b: OsmBuilder) -> None:
    b.way([(0, 0), (3000, 0)], {**CYCLE, "name": "Kurz"}, step=100)
    b.way([(0, 0), (0, 400), (3000, 400), (3000, 0)], {**CYCLE, "name": "Lang"}, step=100)
    b.node(2200, 430, {"place": "hamlet", "name": "Weiler"})  # für die Ortssuche


def grid(b: OsmBuilder) -> None:
    for i in range(21):
        b.way([(0, i * 500), (10000, i * 500)], CYCLE, step=500)
        b.way([(i * 500, 0), (i * 500, 10000)], CYCLE, step=500)


def serve(graph, port: int) -> uvicorn.Server:
    srv = uvicorn.Server(uvicorn.Config(create_app(graph=graph, auth=AuthConfig.disabled()), port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    return srv


GPX_LANG = """<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><metadata><name>Mein Lieblingsweg</name></metadata><trk><trkseg>
%s</trkseg></trk></gpx>"""


async def main() -> int:
    tmp = tempfile.mkdtemp()
    g1, g2 = make_graph(kurz_lang, tmp, "kl"), make_graph(grid, tmp, "grid")
    s1, s2 = serve(g1, 8791), serve(g2, 8792)
    gpx_pts = "".join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"/>' for la, lo in (to_ll(x, y) for x, y in
                      [(0, 0), (0, 200), (0, 400), (1000, 400), (2000, 400), (3000, 400), (3000, 200), (3000, 0)]))
    gpx_file = os.path.join(tmp, "lieblingsweg.gpx")
    open(gpx_file, "w").write(GPX_LANG % gpx_pts)

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROMIUM)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
        page = await ctx.new_page()
        errs: list[str] = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

        async def click_ll(xy):
            la, lo = to_ll(*xy)
            pos = await page.evaluate("([la,lo]) => { const p = map.latLngToContainerPoint([la,lo]); const r = document.getElementById('map').getBoundingClientRect(); return [p.x + r.left, p.y + r.top]; }", [la, lo])
            await page.mouse.click(pos[0], pos[1])
            await page.wait_for_timeout(200)
            return pos

        async def dist():
            await page.wait_for_selector("#stats", state="visible", timeout=15000)
            await page.wait_for_timeout(700)
            txt = await page.inner_text("#sDist")
            return float(txt.replace(" km", "").replace(",", "."))

        async def settle():
            await page.wait_for_timeout(1200)

        print("Route + Bereiche (Kurz/Lang-Netz)")
        await page.goto("http://127.0.0.1:8791/")
        await page.wait_for_selector("#profile option", state="attached")
        await page.evaluate("localStorage.clear()")
        await page.reload(); await page.wait_for_selector("#profile option", state="attached")
        await click_ll((0, 0)); await click_ll((3000, 0))
        d = await dist(); check("Route A→B ist die kurze (3,0 km)", abs(d - 3.0) < 0.1, f"{d} km")

        # Kreis zeichnen: Mittelpunkt (1500,0), Radius ~250 m
        await page.click("#toolCircle")
        check("Zeichenmodus zeigt Hinweisleiste", await page.is_visible("#modebar"))
        await click_ll((1500, 0)); await click_ll((1750, 0))
        await settle(); d = await dist()
        check("Kreis meiden → Route weicht auf den Umweg aus (3,8 km)", abs(d - 3.8) < 0.15, f"{d} km")
        check("Bereich steht in der Liste", await page.locator("#areaList .ov-item").count() == 1)
        check("Hinweisleiste wieder aus", not await page.is_visible("#modebar"))

        # deaktivieren
        await page.locator("#areaList .ov-item input[type=checkbox]").first.uncheck()
        await settle(); d = await dist()
        check("Bereich deaktiviert → wieder kurze Route", abs(d - 3.0) < 0.1, f"{d} km")
        await page.locator("#areaList .ov-item button", has_text="×").first.click()
        check("Bereich gelöscht", await page.locator("#areaList .ov-item").count() == 0)

        # Fläche
        await page.click("#toolPolygon")
        for xy in ((1200, -100), (1800, -100), (1800, 100), (1200, 100)):
            await click_ll(xy)
        await page.click("#modeDone")
        await settle(); d = await dist()
        check("Fläche meiden → Umweg (3,8 km)", abs(d - 3.8) < 0.15, f"{d} km")

        # Persistenz
        await page.reload(); await page.wait_for_selector("#profile option", state="attached")
        check("Bereich bleibt nach Neuladen erhalten", await page.locator("#areaList .ov-item").count() == 1)
        await page.locator("#areaList .ov-item button", has_text="×").first.click()

        # Ziehen an der Route: Kurz-Route, Ghost-Marker ziehen nach (1500, 400) -> Zwischenziel auf dem Umweg
        await page.click("#reset")
        await click_ll((0, 0)); await click_ll((3000, 0)); await dist()
        pos = await click_ll((1500, 0))  # Klick auf die Route -> Popup
        check("Klick auf Route öffnet Popup mit Aktionen", await page.locator(".popbtn").count() == 2)
        await page.locator(".popbtn", has_text="Zwischenziel").click()
        await settle()
        n_pts = await page.locator("#points .pt").count()
        check("Zwischenziel per Popup eingefügt (3 Punkte)", n_pts == 3, f"{n_pts}")

        await page.click("#reset")
        await click_ll((0, 0)); await click_ll((3000, 0)); await dist()
        la, lo = to_ll(1000, 0)
        pos = await page.evaluate("([la,lo]) => { const p = map.latLngToContainerPoint([la,lo]); const r = document.getElementById('map').getBoundingClientRect(); return [p.x + r.left, p.y + r.top]; }", [la, lo])
        await page.mouse.move(pos[0] - 20, pos[1] - 25); await page.mouse.move(pos[0], pos[1], steps=5)
        await page.wait_for_selector(".ghost", timeout=4000)
        check("Über der Route erscheint der Zieh-Marker", True)
        ghost = await page.locator(".ghost").bounding_box()
        target = to_ll(1500, 400)
        tpos = await page.evaluate("([la,lo]) => { const p = map.latLngToContainerPoint([la,lo]); const r = document.getElementById('map').getBoundingClientRect(); return [p.x + r.left, p.y + r.top]; }", list(target))
        await page.mouse.move(ghost["x"] + 8, ghost["y"] + 8); await page.mouse.down()
        await page.mouse.move(tpos[0], tpos[1], steps=12); await page.mouse.up()
        await settle(); d = await dist()
        n_pts = await page.locator("#points .pt").count()
        check("Ziehen fügt ein Zwischenziel ein (3 Punkte) und ändert die Route", n_pts == 3 and d > 3.5, f"{n_pts} Punkte, {d} km")

        # Leeres Zwischenziel-Feld: "+ Zwischenziel" fügt ein leeres Feld vor dem Ziel ein
        await page.click("#reset")
        await click_ll((0, 0)); await click_ll((3000, 0)); await dist()
        await page.click("#addVia")
        vals = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        focused = await page.evaluate("document.activeElement === document.querySelectorAll('#points input')[1]")
        check("„+ Zwischenziel“ fügt ein leeres Feld zwischen Start und Ziel ein (und setzt den Cursor hinein)",
              len(vals) == 3 and vals[1] == "" and vals[0] != "" and vals[2] != "" and focused, str(vals))
        await click_ll((1500, 330)); await settle(); d = await dist()  # neben der Alternativ-Linie (Klick darauf würde sie auswählen)
        vals = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        check("Klick auf die Karte füllt das leere Zwischenziel (Ziel bleibt Ziel)", len(vals) == 3 and vals[1] != "" and abs(d - 3.8) < 0.15, f"{d} km {vals}")
        await page.click("#addVia")
        await page.locator("#points input").nth(2).fill("Weiler")
        await page.wait_for_selector(".suggest div", timeout=5000)
        await page.locator(".suggest div").first.click(); await settle()
        vals = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        check("Suche füllt das leere Zwischenziel", len(vals) == 4 and vals[2] == "Weiler", str(vals))
        await page.click("#addVia")
        n_before = await page.locator("#points .pt").count()
        await page.locator("#points .pt").nth(n_before - 2).locator("button.x").click()
        check("Leeres Zwischenziel lässt sich wieder entfernen", await page.locator("#points .pt").count() == n_before - 1)

        # Umsortieren per Ziehen: Ziel (Zeile 3) nach oben ziehen -> wird Start
        await page.click("#reset")
        await click_ll((0, 0)); await click_ll((1500, 330)); await click_ll((3000, 0)); await dist()
        before = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        g = await page.locator("#points .grip").nth(2).bounding_box()
        top = await page.locator("#points .grip").nth(0).bounding_box()
        await page.mouse.move(g["x"] + g["width"] / 2, g["y"] + g["height"] / 2); await page.mouse.down()
        await page.mouse.move(top["x"] + top["width"] / 2, top["y"] + 2, steps=10)
        await page.wait_for_timeout(150)
        shifted = await page.locator("#points .pt").nth(0).evaluate("e => getComputedStyle(e).transform")
        await page.mouse.up(); await settle(); await dist()
        after = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        check("Ziehen am Griff sortiert um (Ziel wird Start)", after == [before[2], before[0], before[1]], f"{before} -> {after}")
        check("Beim Ziehen rücken die anderen Zeilen sichtbar nach", shifted not in ("none", ""), shifted)
        start_marker = await page.evaluate("""() => { const m = markers[0].getLatLng(); return [m.lat, m.lng]; }""")
        check("Marker A folgt der neuen Reihenfolge", abs(start_marker[1] - to_ll(3000, 0)[1]) < 1e-4, str(start_marker))
        # Pfeiltasten
        await page.locator("#points .grip").nth(0).focus(); await page.keyboard.press("ArrowDown"); await settle()
        after2 = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        focus_ok = await page.evaluate("document.activeElement === document.querySelectorAll('#points .grip')[1]")
        check("Pfeiltaste ↓ verschiebt den Punkt eine Position nach unten (Fokus wandert mit)", after2 == [after[1], after[0], after[2]] and focus_ok, str(after2))

        # Stelle meiden per Popup
        await page.click("#reset")
        await click_ll((0, 0)); await click_ll((3000, 0)); await dist()
        await click_ll((1500, 0))
        await page.locator(".popbtn", has_text="Stelle meiden").click()
        await settle(); d = await dist()
        check("„Stelle meiden“ → Umweg (3,8 km)", abs(d - 3.8) < 0.15, f"{d} km")
        await page.locator("#areaList .ov-item button", has_text="×").first.click()

        # GPX-Lieblingsweg
        await settle(); d = await dist()
        check("Ohne Overlays wieder kurz", abs(d - 3.0) < 0.1, f"{d} km")
        await page.set_input_files("#gpxFile", gpx_file)
        await settle(); d = await dist()
        check("GPX-Lieblingsweg importiert und bevorzugt (3,8 km)", abs(d - 3.8) < 0.15 and await page.locator("#favList .ov-item").count() == 1, f"{d} km")
        check("Name aus der GPX-Datei", "Mein Lieblingsweg" in await page.inner_text("#favList"))
        await page.reload(); await page.wait_for_selector("#profile option", state="attached")
        check("Lieblingsweg bleibt nach Neuladen erhalten", await page.locator("#favList .ov-item").count() == 1)
        await page.locator("#favList .ov-item button", has_text="×").first.click()

        # Route merken
        await page.click("#reset")
        await click_ll((0, 0)); await click_ll((3000, 0)); await dist()
        await page.click("#toolSaveFav")
        check("Route als Lieblingsweg gemerkt", await page.locator("#favList .ov-item").count() == 1)
        await page.locator("#favList .ov-item button", has_text="×").first.click()

        # Rundkurs (loop)
        await page.click("#reset")
        await click_ll((0, 0)); await click_ll((3000, 0)); await click_ll((3000, 400)); await dist()
        await page.check("#loop"); await settle(); d = await dist()
        check("„Zurück zum Start“ verlängert die Route (Rundkurs)", d > 6.5, f"{d} km")

        # GPX-Export
        async with page.expect_download() as dl:
            await page.click("#gpx")
        txt = open(await (await dl.value).path()).read()
        check("GPX-Export enthält Track", "<trkpt" in txt)

        print("Rundreise (Gitter-Netz)")
        await page.goto("http://127.0.0.1:8792/")
        await page.wait_for_selector("#profile option", state="attached")
        await page.click("#tabTrip")
        check("Rundreise-Bereich sichtbar", await page.is_visible("#tripBox"))
        await click_ll((5000, 5000))
        await page.fill("#tripKm", "12"); await page.evaluate("document.getElementById('tripKm').dispatchEvent(new Event('input'))")
        await page.click("#tripGo")
        d = await dist()
        check("Rundreise ≈ 12 km", 9 < d < 15, f"{d} km")
        cards = await page.locator("#alts .alt").all_inner_texts()
        check("Mehrere Rundreise-Varianten", len(cards) >= 2 and cards[0].startswith("Rundreise 1"), f"{len(cards)}")
        check("Hinweis zur Überlappung", "Rundreise" in await page.inner_text("#detour"))
        loop_coords = await page.evaluate("routes[selected].coordinates.map(c => [c[0].toFixed(7), c[1].toFixed(7)].join())")
        uturns = sum(1 for i in range(1, len(loop_coords) - 1) if loop_coords[i - 1] == loop_coords[i + 1])
        check("Rundreise ohne Hin-und-zurück-Stichwege", uturns == 0, f"{uturns} Kehrtwenden")
        pt = await page.evaluate("routes[selected].coordinates[Math.floor(routes[selected].coordinates.length / 3)]")
        pos = await page.evaluate("([la,lo]) => { const p = map.latLngToContainerPoint([la,lo]); const r = document.getElementById('map').getBoundingClientRect(); return [p.x + r.left, p.y + r.top]; }", [pt[1], pt[0]])
        await page.mouse.click(pos[0], pos[1]); await page.wait_for_timeout(400)
        btns = await page.locator(".popbtn").all_inner_texts()
        check("Menü auf der Rundreise bietet nur „Stelle meiden“", len(btns) == 1 and "meiden" in btns[0], str(btns))
        await page.keyboard.press("Escape"); await page.evaluate("map.closePopup()")
        await page.select_option("#tripHeading", "0"); await settle(); await dist()

        # Stationen: Klick auf die Karte fügt eine Station hinzu, Richtung wird gesperrt, Schleife fährt sie an
        await click_ll((8000, 5000)); await settle()
        vals = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        check("Klick in der Rundreise fügt eine Station hinzu (Start bleibt)", len(vals) == 2 and vals[0] != "" and vals[1] != "", str(vals))
        check("Mit Stationen ist die Richtungswahl gesperrt", await page.is_disabled("#tripHeading"))
        await page.click("#tripGo"); d = await dist()
        near = await page.evaluate("""([la,lo]) => Math.min(...routes[selected].coordinates.map(c => Math.hypot((c[0]-lo)*74600, (c[1]-la)*110574)))""", list(to_ll(8000, 5000)))
        check("Rundreise fährt die Station an und hat ≈ Wunschlänge", near < 30 and 9 < d < 15, f"{near:.0f} m, {d} km")
        check("Hinweis nennt die Stationen", "Station" in await page.inner_text("#detour"))
        await page.click("#addStation")
        vals = await page.locator("#points input").evaluate_all("e => e.map(x => x.value)")
        focused = await page.evaluate("document.activeElement === [...document.querySelectorAll('#points input')].pop()")
        check("„+ Station“ fügt ein leeres Feld ein", len(vals) == 3 and vals[2] == "" and focused, str(vals))
        # zwei Stationen, Reihenfolge per Ziehen tauschen -> Rundreise fährt sie in neuer Reihenfolge
        await page.locator("#points .pt").nth(2).locator("button.x").click()
        await page.evaluate("([la, lo]) => map.setView([la, lo], map.getZoom(), { animate: false })", list(to_ll(5250, 8250)))
        await click_ll((5250, 8250)); await settle()  # Zellmitte, nicht auf der angezeigten Schleife
        await page.click("#tripGo"); await dist()
        def order_js(a, b):
            return f"""() => {{ const c = routes[selected].coordinates;
              const idx = (la, lo) => {{ let best = 0, bd = 1e18; c.forEach((p, i) => {{ const d = Math.hypot((p[0]-lo)*74600, (p[1]-la)*110574); if (d < bd) {{ bd = d; best = i; }} }}); return best; }};
              return idx({a[0]}, {a[1]}) < idx({b[0]}, {b[1]}); }}"""
        st1, st2 = to_ll(8000, 5000), to_ll(5250, 8250)
        first_ok = await page.evaluate(order_js(st1, st2))
        g = await page.locator("#points .grip").nth(2).bounding_box()
        t = await page.locator("#points .grip").nth(1).bounding_box()
        await page.mouse.move(g["x"] + 5, g["y"] + 5); await page.mouse.down()
        await page.mouse.move(t["x"] + 5, t["y"] + 2, steps=8); await page.mouse.up()
        await settle(); await dist()
        swapped = await page.evaluate(order_js(st2, st1))
        check("Stationen umsortiert → Rundreise fährt sie in der neuen Reihenfolge an", first_ok and swapped, f"{first_ok} {swapped}")
        await page.locator("#points .pt").nth(2).locator("button.x").click()
        await page.locator("#points .pt").nth(1).locator("button.x").click()
        check("Stationen wieder entfernt, Richtung wieder wählbar", await page.locator("#points .pt").count() == 1 and not await page.is_disabled("#tripHeading"))

        # Deutliche Anzeige während der Berechnung (Antwort künstlich verzögert)
        async def slow(route_):
            await asyncio.sleep(1.5)
            await route_.continue_()
        await page.route("**/api/route", slow)
        await page.click("#tripGo")
        await page.wait_for_timeout(900)
        busy_visible = await page.is_visible("#busy")
        btn = await page.inner_text("#tripGo")
        check("Während der Berechnung: Wartanzeige sichtbar und Knopf gesperrt", busy_visible and await page.is_disabled("#tripGo") and "Berechne" in btn, btn)
        await page.wait_for_timeout(2500)
        check("Nach der Berechnung: Wartanzeige weg", not await page.is_visible("#busy") and not await page.is_disabled("#tripGo"))
        await page.unroute("**/api/route")
        check("Richtung Nord neu berechnet", True)
        async with page.expect_download() as dl:
            await page.click("#gpx")
        check("GPX der Rundreise", "Rundreise" in open(await (await dl.value).path()).read())
        await page.click("#tabRoute")
        check("Zurück zum Reiter Route räumt Ergebnis auf", not await page.is_visible("#stats"))

        # Handy-Ansicht: Karte füllt ihren Bereich (Regression: alte Regel begrenzte sie auf 45 % davon)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(400)
        mh = await page.evaluate("document.getElementById('map').getBoundingClientRect().height")
        wh = await page.evaluate("document.getElementById('mapwrap').getBoundingClientRect().height")
        check("Handy-Ansicht: Karte füllt ihren Bereich", abs(mh - wh) < 2 and wh > 300, f"{mh:.0f}/{wh:.0f} px")
        check("Keine JavaScript-Fehler", not errs, "; ".join(errs))
        await browser.close()
    s1.should_exit = s2.should_exit = True
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} Prüfungen bestanden")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
