"""Routing-Profile: welche Wege ein bestimmter Radtyp bevorzugt.

Alle Kosten sind "Meter-Äquivalente": 1 m ebene, gute Strecke kostet 1.
Eine Autostraße mit Faktor 100 kostet also so viel wie 100 m Radweg – der Router
nimmt lieber 5 km Umweg in Kauf, als 100 m auf einer stark befahrenen Straße zu fahren.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import tags as T


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    # Grundfaktor je Wegklasse (ohne Auto-Malus), Index = HW_*-Code
    class_factor: tuple[float, ...]
    # Oberflächenfaktor, Index = SURF_*-Code (Index 0 unbenutzt)
    surface_factor: tuple[float, ...]
    # Zusatzfaktor je Smoothness-Stufe 0..3
    smooth_factor: tuple[float, ...]
    # Steigungen: Meter-Äquivalente je Höhenmeter bergauf / je Meter Strecke ab 8 % bzw. 12 %
    climb_cost: float
    steep8_cost: float
    steep12_cost: float
    # kleiner Malus für steile Abfahrten (Sicherheit/Bremsen)
    steep_down_cost: float
    # Zeitmodell
    speed_kmh: float
    climb_seconds_per_m: float


# Auto-Malus je Klasse bei voller Meidungs-Stärke (avoid_roads = 1.0).
# Wird mit der Stärke potenziert: Stärke 0 -> Faktor 1 (Straßen egal).
CAR_PENALTY: dict[int, float] = {
    T.HW_TRUNK: 400.0,
    T.HW_PRIMARY: 150.0,
    T.HW_SECONDARY: 100.0,
    T.HW_TERTIARY: 40.0,
    T.HW_UNCLASSIFIED: 10.0,
    T.HW_RESIDENTIAL: 8.0,
    T.HW_LIVING_STREET: 1.5,
    T.HW_SERVICE: 2.0,
}

# Exponent, mit dem der Auto-Malus je nach Radinfrastruktur abgeschwächt wird
INFRA_EXPONENT = {
    T.INFRA_NONE: 1.0,
    T.INFRA_SHARED: 0.8,
    T.INFRA_LANE: 0.5,
    T.INFRA_TRACK: 0.0,  # eigener, baulich getrennter Radweg: Straße spielt keine Rolle
    T.INFRA_BIKE_ROAD: 0.0,
}


def _class_factor(**overrides: float) -> tuple[float, ...]:
    base = {
        T.HW_TRUNK: 1.0,
        T.HW_PRIMARY: 1.0,
        T.HW_SECONDARY: 1.0,
        T.HW_TERTIARY: 1.0,
        T.HW_UNCLASSIFIED: 1.0,
        T.HW_RESIDENTIAL: 1.0,
        T.HW_LIVING_STREET: 1.05,
        T.HW_SERVICE: 1.15,
        T.HW_TRACK: 1.05,
        T.HW_PATH: 1.15,
        T.HW_CYCLEWAY: 0.9,
        T.HW_FOOTWAY: 2.5,
    }
    base.update({getattr(T, "HW_" + k.upper()): v for k, v in overrides.items()})
    out = [1.0] * T.N_HW
    for k, v in base.items():
        out[k] = v
    return tuple(out)


PROFILES: dict[str, Profile] = {
    "trekking": Profile(
        name="trekking",
        label="Trekkingrad",
        class_factor=_class_factor(),
        surface_factor=(1.0, 1.0, 1.15, 1.35, 1.9, 2.4, 3.5),
        smooth_factor=(1.0, 1.1, 1.5, 2.5),
        climb_cost=8.0,
        steep8_cost=0.6,
        steep12_cost=1.5,
        steep_down_cost=0.1,
        speed_kmh=18.0,
        climb_seconds_per_m=3.0,
    ),
    "road": Profile(
        name="road",
        label="Rennrad",
        class_factor=_class_factor(path=1.4, track=1.3, footway=4.0),
        surface_factor=(1.0, 1.0, 1.5, 2.6, 5.5, 3.8, 10.0),
        smooth_factor=(1.0, 1.4, 3.0, 8.0),
        climb_cost=5.0,
        steep8_cost=0.4,
        steep12_cost=1.0,
        steep_down_cost=0.25,
        speed_kmh=25.0,
        climb_seconds_per_m=2.5,
    ),
    "gravel": Profile(
        name="gravel",
        label="Gravel/Crossrad",
        class_factor=_class_factor(track=0.95, path=1.0, cycleway=0.9),
        surface_factor=(1.0, 1.05, 1.25, 1.0, 1.15, 1.8, 2.8),
        smooth_factor=(1.0, 1.0, 1.25, 2.0),
        climb_cost=7.0,
        steep8_cost=0.5,
        steep12_cost=1.2,
        steep_down_cost=0.15,
        speed_kmh=19.0,
        climb_seconds_per_m=3.0,
    ),
    "ebike": Profile(
        name="ebike",
        label="E-Bike",
        class_factor=_class_factor(),
        surface_factor=(1.0, 1.0, 1.15, 1.3, 1.7, 2.2, 3.0),
        smooth_factor=(1.0, 1.1, 1.4, 2.2),
        climb_cost=2.0,
        steep8_cost=0.15,
        steep12_cost=0.5,
        steep_down_cost=0.1,
        speed_kmh=22.0,
        climb_seconds_per_m=1.0,
    ),
}

DEFAULT_PROFILE = "trekking"


@dataclass(frozen=True)
class Options:
    """Vom Nutzer einstellbare Parameter.

    avoid_roads:  Stärke des Straßen-Meidens. 0 = egal, 1 = konsequent (Standard),
                  >1 = noch schärfer.
    calm:         Zusätzliche Skalierung nur für Wohnstraßen, Spielstraßen und Zufahrten
                  (Exponent des Malus; 0 = egal, 1 = Standard, 2-3 = möglichst nie durch Wohngebiete).
    hills:        Skalierung der Steigungs-Abneigung. 0 = Steigungen egal, 1 = Profil-Standard.
    surface:      Skalierung der Oberflächen-Abneigung (0 = egal, 1 = Standard, 2 = sehr pingelig).
    signal_cost:  Zeitverlust je Ampel in Meter-Äquivalenten.
    prefer_routes: Bonus für ausgeschilderte Radrouten (Faktor < 1 = bevorzugt).
    """

    avoid_roads: float = 1.0
    calm: float = 1.0
    hills: float = 1.0
    surface: float = 1.0
    signal_cost: float = 30.0
    route_bonus: float = 0.85

    def key(self) -> tuple:
        return (self.avoid_roads, self.calm, self.hills, self.surface, self.signal_cost, self.route_bonus)


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"Unbekanntes Profil {name!r}; verfügbar: {', '.join(PROFILES)}") from None
