"""Reusable geo / location module.

Single source of truth for every distance calculation in the app. Nothing else
in the codebase should do trigonometry -- blueprints call `distance_km`,
`haversine_km`, `nearby_stations` and `nearby_mechanics`.

Distances use the haversine great-circle formula, which is accurate to well
under 1% at city scale and needs no external service or API key.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Iterable, Optional, Sequence, TypeVar

from flask import current_app
from sqlalchemy import select

from extensions import db
from models import (
    STATUS_APPROVED,
    FuelProduct,
    Mechanic,
    Station,
)

EARTH_RADIUS_KM = 6371.0088

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Core maths
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two WGS84 points."""
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def distance_km(a: "LatLng", b: "LatLng") -> float:
    """Distance between two `LatLng` points, in kilometres."""
    return haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)


@dataclass(frozen=True)
class LatLng:
    latitude: float
    longitude: float

    @classmethod
    def from_obj(cls, obj) -> Optional["LatLng"]:
        lat = getattr(obj, "latitude", None)
        lon = getattr(obj, "longitude", None)
        if lat is None or lon is None:
            return None
        return cls(float(lat), float(lon))


def parse_coords(lat_raw, lon_raw) -> Optional[LatLng]:
    """Parse and validate (lat, lon) form/JSON input. Returns None if invalid."""
    if lat_raw in (None, "") or lon_raw in (None, ""):
        return None
    try:
        lat, lon = float(lat_raw), float(lon_raw)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return LatLng(lat, lon)


def clamp_radius(radius_raw, default: float | None = None, maximum: float | None = None) -> float:
    """Normalise a radius argument coming from a form/query string."""
    default = default if default is not None else current_app.config["DEFAULT_SEARCH_RADIUS_KM"]
    maximum = maximum if maximum is not None else current_app.config["MAX_SEARCH_RADIUS_KM"]
    try:
        radius = float(radius_raw)
    except (TypeError, ValueError):
        return float(default)
    if radius <= 0:
        return float(default)
    return min(radius, float(maximum))


def resolve_driver_location(request_args, user=None) -> tuple[Optional[LatLng], bool]:
    """Work out the driver's position.

    Priority: explicit query params (sent by the browser's geolocation API)
    -> last known position stored on the user record.

    Returns (point_or_None, came_from_browser).
    """
    browser_point = parse_coords(request_args.get("lat"), request_args.get("lng"))
    if browser_point:
        return browser_point, True
    if user is not None:
        return LatLng.from_obj(user), False
    return None, False


# --------------------------------------------------------------------------- #
# "Nearby" queries -- filtering + distance sorting
# --------------------------------------------------------------------------- #
@dataclass
class NearbyResult:
    """An entity paired with its distance from the driver."""

    entity: object
    distance_km: float

    @property
    def distance_label(self) -> str:
        if self.distance_km < 1:
            return f"{self.distance_km * 1000:.0f} m"
        return f"{self.distance_km:.1f} km"


def sort_by_distance(
    items: Iterable[T], origin: LatLng, limit: Optional[int] = None
) -> list[NearbyResult]:
    """Attach a distance to each item and sort nearest-first."""
    results: list[NearbyResult] = []
    for item in items:
        point = LatLng.from_obj(item)
        if point is None:
            continue
        results.append(NearbyResult(item, distance_km(origin, point)))
    results.sort(key=lambda r: r.distance_km)
    return results[:limit] if limit else results


def nearby_stations(
    origin: LatLng,
    radius_km: float | None = None,
    fuel_type: str | None = None,
    limit: int | None = None,
) -> list[NearbyResult]:
    """Approved, open stations within `radius_km`, nearest first.

    Optionally restricted to stations that currently offer `fuel_type`.
    """
    radius = clamp_radius(radius_km)
    stmt = select(Station).where(
        Station.status == STATUS_APPROVED, Station.is_open.is_(True)
    )
    if fuel_type:
        stmt = stmt.join(FuelProduct).where(
            FuelProduct.fuel_type == fuel_type, FuelProduct.in_stock.is_(True)
        )
    stations = db.session.execute(stmt).scalars().unique().all()
    results = sort_by_distance(stations, origin)
    return [r for r in results if r.distance_km <= radius][:limit] if limit else [
        r for r in results if r.distance_km <= radius
    ]


def nearby_mechanics(
    origin: LatLng,
    radius_km: float | None = None,
    available_only: bool = True,
    limit: int | None = None,
) -> list[NearbyResult]:
    """Approved mechanics whose service area covers the driver, nearest first.

    A mechanic matches when the driver is inside the larger of the request
    radius and the mechanic's own `service_radius_km`.
    """
    radius = clamp_radius(radius_km)
    stmt = select(Mechanic).where(Mechanic.status == STATUS_APPROVED)
    if available_only:
        stmt = stmt.where(Mechanic.available.is_(True))
    mechanics = db.session.execute(stmt).scalars().all()

    results: list[NearbyResult] = []
    for mechanic in mechanics:
        point = LatLng.from_obj(mechanic)
        if point is None:
            continue
        dist = distance_km(origin, point)
        service_radius = mechanic.service_radius_km or 0.0
        # Within the driver's own search radius OR inside the mechanic's area.
        if dist <= max(radius, service_radius):
            results.append(NearbyResult(mechanic, dist))
    results.sort(key=lambda r: r.distance_km)
    return results[:limit] if limit else results


def is_within_service_area(mechanic: Mechanic, point: LatLng) -> tuple[bool, float]:
    """(in_area, distance) check used when a driver requests a mechanic."""
    base = LatLng.from_obj(mechanic)
    if base is None:
        return False, float("inf")
    dist = distance_km(base, point)
    return dist <= (mechanic.service_radius_km or 0.0), dist


def fuel_types_offered() -> Sequence[str]:
    """Distinct fuel types available anywhere on the platform (for filters)."""
    rows = db.session.execute(
        select(FuelProduct.fuel_type).distinct().order_by(FuelProduct.fuel_type)
    ).scalars().all()
    return rows
