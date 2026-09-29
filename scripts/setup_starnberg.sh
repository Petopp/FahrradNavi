#!/usr/bin/env bash
# Baut alles Nötige für den Landkreis Starnberg (+ Umland): OSM-Daten, Höhen, Routing-Graph.
# Benötigt Internetzugang zu download.geofabrik.de und elevation-tiles-prod.s3.amazonaws.com.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m fahrradnavi -v download oberbayern --out data          # ~250 MB
python -m fahrradnavi -v dem --region starnberg --out data/dem    # 2 SRTM-Kacheln, ~25 MB
python -m fahrradnavi -v build data/oberbayern-latest.osm.pbf --region starnberg --dem data/dem --out data/graph.npz

echo
echo "Fertig. Testen mit:"
echo '  python -m fahrradnavi route --from "Starnberg" --to "Kloster Andechs" --gpx andechs.gpx'
echo "Weboberfläche:"
echo "  python -m fahrradnavi serve   # http://127.0.0.1:8000"
