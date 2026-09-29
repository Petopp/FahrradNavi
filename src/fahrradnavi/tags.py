"""Interpretation von OSM-Tags für das Fahrrad-Routing.

Reine Python-Funktionen ohne Abhängigkeit von osmium, damit sie gut testbar sind.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Straßenklassen (im Graphen als uint8 gespeichert) -----------------------
HW_TRUNK = 1
HW_PRIMARY = 2
HW_SECONDARY = 3
HW_TERTIARY = 4
HW_UNCLASSIFIED = 5
HW_RESIDENTIAL = 6
HW_LIVING_STREET = 7
HW_SERVICE = 8
HW_TRACK = 9
HW_PATH = 10
HW_CYCLEWAY = 11
HW_FOOTWAY = 12  # Fußweg/Fußgängerzone mit Radfreigabe ("Radfahrer frei")
N_HW = 13

HW_NAMES = {
    HW_TRUNK: "trunk",
    HW_PRIMARY: "primary",
    HW_SECONDARY: "secondary",
    HW_TERTIARY: "tertiary",
    HW_UNCLASSIFIED: "unclassified",
    HW_RESIDENTIAL: "residential",
    HW_LIVING_STREET: "living_street",
    HW_SERVICE: "service",
    HW_TRACK: "track",
    HW_PATH: "path",
    HW_CYCLEWAY: "cycleway",
    HW_FOOTWAY: "footway",
}

_HIGHWAY_MAP = {
    "trunk": HW_TRUNK,
    "trunk_link": HW_TRUNK,
    "primary": HW_PRIMARY,
    "primary_link": HW_PRIMARY,
    "secondary": HW_SECONDARY,
    "secondary_link": HW_SECONDARY,
    "tertiary": HW_TERTIARY,
    "tertiary_link": HW_TERTIARY,
    "unclassified": HW_UNCLASSIFIED,
    "road": HW_UNCLASSIFIED,
    "residential": HW_RESIDENTIAL,
    "living_street": HW_LIVING_STREET,
    "service": HW_SERVICE,
    "track": HW_TRACK,
    "path": HW_PATH,
    "cycleway": HW_CYCLEWAY,
    "bridleway": HW_PATH,
    "footway": HW_FOOTWAY,
    "pedestrian": HW_FOOTWAY,
    "corridor": HW_FOOTWAY,
}

# --- Radinfrastruktur an Autostraßen ----------------------------------------
INFRA_NONE = 0
INFRA_SHARED = 1  # Schutzstreifen-artig / Bus-Mitbenutzung / "shared_lane"
INFRA_LANE = 2  # Radfahrstreifen (aufgemalt)
INFRA_TRACK = 3  # baulich getrennter Radweg (straßenbegleitend)
INFRA_BIKE_ROAD = 4  # Fahrradstraße

# --- Oberflächenklassen ------------------------------------------------------
SURF_SMOOTH = 1  # Asphalt, Beton
SURF_PAVERS = 2  # Pflaster (eben), Platten, Ziegel
SURF_COMPACTED = 3  # verdichtet, Splitt/Feinkies
SURF_GRAVEL = 4  # Schotter, Erde
SURF_COBBLES = 5  # Kopfsteinpflaster
SURF_ROUGH = 6  # Gras, Sand, Matsch
N_SURF = 7

_SURFACE_MAP = {
    "asphalt": SURF_SMOOTH,
    "concrete": SURF_SMOOTH,
    "paved": SURF_SMOOTH,
    "concrete:lanes": SURF_SMOOTH,
    "metal": SURF_SMOOTH,
    "wood": SURF_PAVERS,
    "paving_stones": SURF_PAVERS,
    "paving_stones:lanes": SURF_PAVERS,
    "concrete:plates": SURF_PAVERS,
    "bricks": SURF_PAVERS,
    "brick": SURF_PAVERS,
    "cobblestone:flattened": SURF_PAVERS,
    "compacted": SURF_COMPACTED,
    "fine_gravel": SURF_COMPACTED,
    "gravel": SURF_GRAVEL,
    "pebblestone": SURF_GRAVEL,
    "unpaved": SURF_GRAVEL,
    "ground": SURF_GRAVEL,
    "dirt": SURF_GRAVEL,
    "earth": SURF_GRAVEL,
    "rock": SURF_GRAVEL,
    "sett": SURF_COBBLES,
    "cobblestone": SURF_COBBLES,
    "unhewn_cobblestone": SURF_COBBLES,
    "grass": SURF_ROUGH,
    "grass_paver": SURF_ROUGH,
    "sand": SURF_ROUGH,
    "mud": SURF_ROUGH,
    "snow": SURF_ROUGH,
    "woodchips": SURF_ROUGH,
}

_TRACKTYPE_MAP = {
    "grade1": SURF_SMOOTH,
    "grade2": SURF_COMPACTED,
    "grade3": SURF_GRAVEL,
    "grade4": SURF_GRAVEL,
    "grade5": SURF_ROUGH,
}

_SMOOTHNESS_MAP = {
    "excellent": 0,
    "good": 0,
    "intermediate": 1,
    "bad": 2,
    "very_bad": 3,
    "horrible": 3,
    "very_horrible": 3,
    "impassable": 3,
}

# Flags (Bitmaske pro Way)
FLAG_ROUTE = 1  # Teil einer ausgeschilderten Radroute (Relation route=bicycle)
FLAG_BRIDGE = 2
FLAG_TUNNEL = 4
FLAG_SIDEPATH = 8  # bicycle=use_sidepath: Radfahrer sollen den Radweg nebenan nutzen
FLAG_DISMOUNT = 16  # bicycle=dismount: schieben

_YES = {"yes", "true", "1", "designated", "permissive", "official"}
_NO = {"no", "false", "0", "private", "dismount"}


@dataclass(frozen=True)
class WayInfo:
    hwc: int
    fwd: bool
    bwd: bool
    infra: int
    surface: int
    smooth: int
    maxspeed: int
    flags: int
    name: str


def parse_maxspeed(value: str | None) -> int:
    """km/h oder 0 wenn unbekannt."""
    if not value:
        return 0
    v = value.strip().lower()
    if v in ("walk", "de:living_street"):
        return 7
    if v == "de:urban":
        return 50
    if v == "de:rural":
        return 100
    if v == "de:zone30" or v == "zone:30":
        return 30
    digits = ""
    for ch in v.split(";")[0].split(" ")[0]:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if digits:
        n = int(digits)
        if "mph" in v:
            n = int(n * 1.609)
        return min(n, 255)
    if ":" in v:  # z.B. "DE:30"
        tail = v.rsplit(":", 1)[-1]
        if tail.isdigit():
            return min(int(tail), 255)
    return 0


def _infra(tags: dict[str, str]) -> int:
    best = INFRA_NONE
    for key in ("cycleway", "cycleway:both", "cycleway:left", "cycleway:right"):
        val = tags.get(key)
        if not val:
            continue
        if val in ("track", "opposite_track"):
            best = max(best, INFRA_TRACK)
        elif val in ("lane", "opposite_lane", "exclusive"):
            best = max(best, INFRA_LANE)
        elif val in ("shared_lane", "share_busway", "opposite_share_busway", "shared", "opposite"):
            best = max(best, INFRA_SHARED)
    if tags.get("bicycle_road") == "yes" or tags.get("cyclestreet") == "yes":
        best = max(best, INFRA_BIKE_ROAD)
    elif tags.get("bicycle") == "designated":
        best = max(best, INFRA_LANE)
    return best


def _contraflow(tags: dict[str, str]) -> bool:
    if tags.get("oneway:bicycle") == "no":
        return True
    for key in ("cycleway", "cycleway:both", "cycleway:left", "cycleway:right"):
        val = tags.get(key, "")
        if val.startswith("opposite"):
            return True
    return False


def _oneway(tags: dict[str, str], hwc: int) -> tuple[bool, bool]:
    """(vorwärts erlaubt, rückwärts erlaubt) für Radfahrer."""
    ob = tags.get("oneway:bicycle")
    ow = ob if ob is not None else tags.get("oneway")
    if ow is None and tags.get("junction") in ("roundabout", "circular"):
        ow = "yes"
    if ow in ("yes", "true", "1"):
        return (True, _contraflow(tags))
    if ow in ("-1", "reverse"):
        return (_contraflow(tags), True)
    return (True, True)


def _surface(tags: dict[str, str], hwc: int) -> int:
    s = tags.get("surface")
    if s:
        first = s.split(";")[0].strip()
        if first in _SURFACE_MAP:
            return _SURFACE_MAP[first]
    tt = tags.get("tracktype")
    if tt in _TRACKTYPE_MAP:
        return _TRACKTYPE_MAP[tt]
    if hwc in (HW_TRACK, HW_PATH):
        return SURF_COMPACTED
    return SURF_SMOOTH


def classify_way(tags: dict[str, str]) -> WayInfo | None:
    """Wertet die Tags eines OSM-Ways aus.

    Gibt ``None`` zurück, wenn der Weg für Radfahrer nicht nutzbar ist.
    """
    hw = tags.get("highway")
    if hw is None:
        return None
    hwc = _HIGHWAY_MAP.get(hw)
    if hwc is None:
        return None  # motorway, steps, construction, proposed, ...

    if tags.get("area") == "yes" and hwc != HW_FOOTWAY:
        return None
    if "route" in tags and tags.get("route") == "ferry":
        return None
    if tags.get("indoor") == "yes":
        return None

    bicycle = tags.get("bicycle")
    access = tags.get("access")
    vehicle = tags.get("vehicle")

    if bicycle in ("no", "private", "false", "0"):
        return None
    bike_yes = bicycle in _YES
    if access in ("no", "private") and not bike_yes:
        return None
    if vehicle in ("no", "private") and not bike_yes:
        return None

    flags = 0
    if bicycle == "dismount":
        flags |= FLAG_DISMOUNT
    if bicycle == "use_sidepath":
        flags |= FLAG_SIDEPATH
        bike_yes = False

    if hw == "bridleway" and not bike_yes:
        return None
    if hwc == HW_FOOTWAY and not bike_yes and not (flags & FLAG_DISMOUNT):
        return None
    if hwc == HW_SERVICE and tags.get("service") in ("driveway", "parking_aisle", "drive-through"):
        return None
    if hw in ("construction", "proposed", "razed", "abandoned"):
        return None

    bridge = tags.get("bridge")
    if bridge and bridge != "no":
        flags |= FLAG_BRIDGE
    tunnel = tags.get("tunnel")
    if (tunnel and tunnel != "no") or tags.get("covered") == "building_passage":
        flags |= FLAG_TUNNEL

    fwd, bwd = _oneway(tags, hwc)
    infra = _infra(tags) if hwc <= HW_RESIDENTIAL else INFRA_NONE
    name = tags.get("name") or tags.get("ref") or ""
    return WayInfo(
        hwc=hwc,
        fwd=fwd,
        bwd=bwd,
        infra=infra,
        surface=_surface(tags, hwc),
        smooth=_SMOOTHNESS_MAP.get(tags.get("smoothness", ""), 0),
        maxspeed=parse_maxspeed(tags.get("maxspeed")),
        flags=flags,
        name=name,
    )


# Autostraßen im Sinne von "Straßen meiden" (Radwege/Wege zählen nicht)
CAR_CLASSES = (HW_TRUNK, HW_PRIMARY, HW_SECONDARY, HW_TERTIARY, HW_UNCLASSIFIED, HW_RESIDENTIAL)
