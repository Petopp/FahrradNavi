# Projektstand

Stand: `main` nach PR #2 (Commit a192082), 30.09.2026.
Repository: <https://github.com/Petopp/FahrradNavi>

## Lokalen Ordner aktualisieren

```bash
cd /Volumes/Daten/Projekte/AICode/FahrradNavi
git status                 # lokale Änderungen vorher sichern
git fetch origin
git checkout main
git pull origin main
```

Ist der Ordner kein Git-Clone: neu klonen (`git clone https://github.com/Petopp/FahrradNavi.git`)
und den alten Ordner umbenennen statt überschreiben. Die Daten unter `data/` sind **nicht** in Git
(siehe unten) und müssen ggf. vorher gesichert werden.

## Funktionsumfang

* Fahrradrouting auf OpenStreetMap-Daten, das Autostraßen stark meidet (auch mit mehreren km Umweg);
  eigene Radwege und Straßen mit getrenntem Radweg sind in Ordnung.
* Alternativen, sortiert nach möglichst wenig Straße statt nach Kürze.
* Regler: Wohnstraßen meiden, Bebauung meiden, Innenstadt meiden.
* Bereiche zum Meiden (Kreis/Polygon) und Lieblingswege (GPX).
* Route per Markierung ändern: Ziehen fügt einen Zwischenpunkt ein, „Stelle meiden“.
* Leeres Zwischenpunkt-Feld; Punkte und Stationen per Ziehen umsortieren.
* „Zurück zum Start“ sowie Rundreisen mit Länge, Richtung und eigenen Stationen.
* Deutliche Warteanzeige während der Berechnung.
* Zurücksetzen in beiden Reitern; Routen werden gelöscht, sobald Punkte/Stationen fehlen.
* GPX-Export (z. B. für Komoot), im Browser erzeugt.
* Web-Oberfläche (FastAPI, Leaflet lokal mitgeliefert, kein CDN).
* Docker/Compose/Caddy (HTTPS); Passwortschutz standardmäßig, Login-Sperre, optionale IP-Freigabe,
  API-Token. Konfiguration über `FAHRRADNAVI_*` (siehe `.env.example`, Details im README).

## Qualität

* `pytest -q`: 138 Tests grün.
* `scripts/ui_smoke.py`: 58 Browser-Checks (Playwright/Chromium) grün.
* CI: `.github/workflows/tests.yml`.

## Nicht in Git (lokal erzeugen)

| Datei | Erzeugung |
|---|---|
| `data/graph.npz` (Graph-Format **v3**) | `fahrradnavi setup` bzw. `build`; für Starnberg `scripts/setup_starnberg.sh` |
| `data/dem/*.hgt.gz` | SRTM via AWS Terrain Tiles (`fahrradnavi dem`) |
| `data/lu/`, `data/poi2/` | Landnutzung/POIs für „Bebauung“/„Innenstadt“ |

Das Graphformat hat sich mit Bebauung/Innenstadt geändert (v3: zusätzlich `e_urban`, `e_center`).
Ein älterer Graph muss neu gebaut werden.

Datenquellen: Overpass-Mirror (maps.mail.ru) für das Testgebiet, da Geofabrik aus der
Entwicklungsumgebung nicht erreichbar war; für den Serverbetrieb sind Geofabrik-Extrakte vorgesehen.

## Testgebiet

Landkreis Starnberg (Bbox 11.0–11.65 / 47.7–48.2), Beispielroute Starnberg Mitte
(Leutstettener Str.) → Kloster Andechs.

## Arbeitsweise

* Änderungen per Pull-Request nach `main`.
* Claude darf eigene PRs selbst nach `main` mergen, wenn sie konfliktfrei und grün sind; bei großen
  oder riskanten Änderungen (Auth, Datenformat, Docker) wird vorher gefragt.

## Bekannte Grenzen / mögliche nächste Schritte

* Keine Turn-by-Turn-Ansagen (Routenbeschreibung nach Straßennamen wäre möglich).
* Rundreisen: vereinzelt noch kurze Stichstrecken (>400 m) in ~11 von 45 Testschleifen.
* SRTM-Höhen (30 m) sind grob; OSM-Tags bestimmen die Qualität.
* Skalierung auf ganz Bayern: siehe README („Skalierung auf ganz Bayern“).
* Lizenz für den Code ist noch festzulegen.
