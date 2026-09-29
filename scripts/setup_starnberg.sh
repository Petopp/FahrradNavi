#!/usr/bin/env bash
# Baut alles Nötige für den Landkreis Starnberg (+ Umland): OSM-Daten, Höhen, Routing-Graph.
# Benötigt Internetzugang zu download.geofabrik.de und elevation-tiles-prod.s3.amazonaws.com.
set -euo pipefail
cd "$(dirname "$0")/.."

# Variante 1 (Standard): Geofabrik-Extrakt Oberbayern (~250 MB), wird auf Starnberg zugeschnitten.
# Variante 2 (./scripts/setup_starnberg.sh overpass): nur das Gebiet per Overpass-API in Kacheln laden (~15 Min, ~16 MB).
if [ "${1:-}" = "overpass" ]; then
  python -m fahrradnavi -v download starnberg --overpass --out data \
    ${OVERPASS_URL:+--overpass-url "$OVERPASS_URL"}
  OSM=data/starnberg.osm.pbf
else
  python -m fahrradnavi -v download oberbayern --out data
  OSM=data/oberbayern-latest.osm.pbf
fi
python -m fahrradnavi -v dem --region starnberg --out data/dem    # 2 SRTM-Kacheln, ~21 MB
python -m fahrradnavi -v build "$OSM" --region starnberg --dem data/dem --out data/graph.npz

echo
echo "Fertig. Testen mit:"
echo '  python -m fahrradnavi route --from "Starnberg" --to "Kloster Andechs" --gpx andechs.gpx'
echo "Weboberfläche:"
echo "  python -m fahrradnavi serve   # http://127.0.0.1:8000"
