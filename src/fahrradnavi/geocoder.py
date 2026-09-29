"""Einfache Offline-Ortssuche über die beim Import gesammelten Namen (Orte, Sehenswürdigkeiten, Straßen).

Bewusst ohne externe Dienste: Der öffentliche Nominatim-Server erlaubt keinen Produktivbetrieb.
Wer bessere Adresssuche braucht, kann einen eigenen Nominatim/Photon betreiben und im Frontend anbinden.
"""

from __future__ import annotations

import math
import unicodedata

_TRANS = str.maketrans({"ß": "ss", "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "ae", "Ö": "oe", "Ü": "ue"})

KIND_LABELS = {
    "city": "Stadt", "town": "Stadt", "village": "Dorf", "suburb": "Stadtteil", "hamlet": "Weiler",
    "street": "Straße", "monastery": "Kloster", "castle": "Burg", "station": "Bahnhof", "halt": "Haltepunkt",
    "attraction": "Sehenswürdigkeit", "viewpoint": "Aussichtspunkt", "museum": "Museum",
    "biergarten": "Biergarten", "cafe": "Café", "restaurant": "Restaurant", "pub": "Gasthaus",
    "peak": "Gipfel", "place_of_worship": "Kirche", "beach": "Badestelle", "park": "Park",
}


def normalize(s: str) -> str:
    s = s.lower().translate(_TRANS)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


class Geocoder:
    def __init__(self, places: list):
        # places: [name, kind, rank, lat, lon]
        self.places = places
        self._norm = [normalize(p[0]) for p in places]

    def search(self, query: str, limit: int = 8, near: tuple[float, float] | None = None) -> list[dict]:
        toks = normalize(query).split()
        if not toks:
            return []
        scored: list[tuple[float, int]] = []
        for i, n in enumerate(self._norm):
            if not all(t in n for t in toks):
                continue
            words = n.replace("-", " ").split()
            score = 0.0
            if n == " ".join(toks):
                score += 100
            if n.startswith(toks[0]):
                score += 40
            elif any(w.startswith(toks[0]) for w in words):
                score += 20
            score += self.places[i][2] * 3  # Rang: Städte > Dörfer > Straßen
            score -= len(n) * 0.05
            if near is not None:
                d = math.hypot(self.places[i][3] - near[0], (self.places[i][4] - near[1]) * 0.68) * 111.0
                score -= min(d, 100) * 0.3
            scored.append((score, i))
        scored.sort(reverse=True)
        out = []
        seen = set()
        for _, i in scored:
            name, kind, _rank, lat, lon = self.places[i]
            key = (name, kind, round(lat, 2), round(lon, 2))
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "kind": KIND_LABELS.get(kind, kind), "lat": lat, "lon": lon})
            if len(out) >= limit:
                break
        return out
