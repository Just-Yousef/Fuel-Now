"""Unit tests for the reusable geo module (geo.py)."""
import pytest

from geo import (
    LatLng,
    clamp_radius,
    distance_km,
    haversine_km,
    is_within_service_area,
    nearby_mechanics,
    nearby_stations,
    parse_coords,
    sort_by_distance,
)
from tests.conftest import make_mechanic, make_station

ABUJA = LatLng(9.0579, 7.4951)
WUSE = LatLng(9.0772, 7.4723)


def test_haversine_zero_distance():
    assert haversine_km(9.0, 7.4, 9.0, 7.4) == pytest.approx(0.0, abs=1e-9)


def test_haversine_known_distance():
    # Abuja -> Lagos is roughly 530 km as the crow flies.
    dist = haversine_km(9.0579, 7.4951, 6.4474, 3.4736)
    assert 480 < dist < 560


def test_haversine_symmetry_and_scale():
    a, b = (9.0579, 7.4951), (9.0772, 7.4723)
    assert haversine_km(*a, *b) == pytest.approx(haversine_km(*b, *a))
    # ~0.02 degrees of latitude is ~2.2 km
    assert haversine_km(9.0, 7.4, 9.02, 7.4) == pytest.approx(2.22, abs=0.05)


def test_distance_km_between_latlng_points():
    assert distance_km(ABUJA, WUSE) == pytest.approx(haversine_km(9.0579, 7.4951, 9.0772, 7.4723))


def test_parse_coords_valid_and_invalid():
    assert parse_coords("9.05", "7.49") == LatLng(9.05, 7.49)
    assert parse_coords(9, 7) == LatLng(9.0, 7.0)
    assert parse_coords("", "") is None
    assert parse_coords(None, None) is None
    assert parse_coords("abc", "7.4") is None
    assert parse_coords("91", "7.4") is None        # latitude out of range
    assert parse_coords("9.05", "181") is None      # longitude out of range


def test_clamp_radius(app):
    with app.app_context():
        assert clamp_radius(None) == app.config["DEFAULT_SEARCH_RADIUS_KM"]
        assert clamp_radius("bad") == app.config["DEFAULT_SEARCH_RADIUS_KM"]
        assert clamp_radius(-5) == app.config["DEFAULT_SEARCH_RADIUS_KM"]
        assert clamp_radius(10) == 10.0
        # Never exceed the configured maximum.
        assert clamp_radius(10_000) == app.config["MAX_SEARCH_RADIUS_KM"]


def test_sort_by_distance_orders_nearest_first(app):
    with app.app_context():
        far = make_station(name="Far", lat=9.30, lng=7.60, owner_email="f@example.com")
        near = make_station(name="Near", lat=9.06, lng=7.50, owner_email="n@example.com")
        mid = make_station(name="Mid", lat=9.10, lng=7.52, owner_email="m@example.com")

        results = sort_by_distance([far, mid, near], ABUJA)
        assert [r.entity.name for r in results] == ["Near", "Mid", "Far"]
        assert results[0].distance_km < results[1].distance_km < results[2].distance_km


def test_nearby_stations_filters_status_radius_and_stock(app):
    with app.app_context():
        make_station(name="Open Approved", lat=9.06, lng=7.50, owner_email="a@example.com")
        make_station(
            name="Closed", lat=9.06, lng=7.50, is_open=False, owner_email="b@example.com"
        )
        make_station(
            name="Pending",
            lat=9.06,
            lng=7.50,
            status="pending",
            owner_email="c@example.com",
        )
        make_station(name="Too far", lat=6.44, lng=3.47, owner_email="d@example.com")

        names = [r.entity.name for r in nearby_stations(ABUJA, radius_km=25)]
        assert names == ["Open Approved"]
        # Status/open filters still apply: only the approved, open seller is
        # returned even at a wide radius.
        names_all = [r.entity.name for r in nearby_stations(ABUJA, radius_km=1000)]
        assert names_all == ["Open Approved"]
        # The radius is clamped to MAX_SEARCH_RADIUS_KM (200 km) by default,
        # so a 500 km station stays hidden unless the cap is raised.
        capped = [r.entity.name for r in nearby_stations(ABUJA, radius_km=10_000)]
        assert capped == ["Open Approved"]


def test_nearby_stations_fuel_type_filter(app):
    with app.app_context():
        make_station(
            name="Petrol only",
            lat=9.06,
            lng=7.50,
            owner_email="p@example.com",
            products=(("Petrol (PMS)", 900.0),),
        )
        make_station(
            name="Diesel only",
            lat=9.07,
            lng=7.50,
            owner_email="d@example.com",
            products=(("Diesel (AGO)", 1100.0),),
        )
        diesel = nearby_stations(ABUJA, radius_km=25, fuel_type="Diesel (AGO)")
        assert [r.entity.name for r in diesel] == ["Diesel only"]


def test_nearby_mechanics_respects_availability_and_service_area(app):
    with app.app_context():
        make_mechanic(business_name="Near Available", lat=9.06, lng=7.49, email="m1@example.com")
        make_mechanic(
            business_name="Busy",
            lat=9.06,
            lng=7.49,
            available=False,
            email="m2@example.com",
        )
        make_mechanic(
            business_name="Pending",
            lat=9.06,
            lng=7.49,
            status="pending",
            email="m3@example.com",
        )
        # Approved and available but 500 km away with a 15 km service area.
        make_mechanic(
            business_name="Lagos Mechanic", lat=6.44, lng=3.47, email="m4@example.com"
        )

        names = [r.entity.business_name for r in nearby_mechanics(ABUJA, radius_km=25)]
        assert names == ["Near Available"]


def test_mechanic_visible_when_inside_own_service_radius(app):
    with app.app_context():
        # 30 km away with a 40 km radius: not inside the 10 km search radius,
        # but the mechanic's own service area reaches the driver.
        make_mechanic(
            business_name="Wide Reach",
            lat=9.30,
            lng=7.60,
            radius=40.0,
            email="wide@example.com",
        )
        names = [r.entity.business_name for r in nearby_mechanics(ABUJA, radius_km=10)]
        assert names == ["Wide Reach"]


def test_is_within_service_area(app):
    with app.app_context():
        mechanic = make_mechanic(lat=9.06, lng=7.49, radius=5.0, email="zone@example.com")
        inside, dist = is_within_service_area(mechanic, ABUJA)
        assert inside is True and dist < 5
        outside, dist2 = is_within_service_area(mechanic, LatLng(9.30, 7.60))
        assert outside is False and dist2 > 5
