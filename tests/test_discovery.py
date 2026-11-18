"""Core flow: location-based discovery (nearby stations and mechanics)."""
from extensions import db
from models import ServiceRequest
from tests.conftest import (
    DRIVER_LAT,
    DRIVER_LNG,
    login,
    make_mechanic,
    make_station,
)

NEARBY = f"lat={DRIVER_LAT}&lng={DRIVER_LNG}"


def test_nearby_stations_sorted_by_distance(app, client, driver):
    with app.app_context():
        make_station(name="Far Station", lat=9.30, lng=7.60, owner_email="f@example.com")
        make_station(name="Near Station", lat=9.06, lng=7.50, owner_email="n@example.com")
        make_station(name="Mid Station", lat=9.10, lng=7.52, owner_email="m@example.com")

    login(client, driver.email)
    body = client.get(f"/driver/stations?{NEARBY}&radius=50").get_data(as_text=True)

    near, mid, far = (
        body.index("Near Station"),
        body.index("Mid Station"),
        body.index("Far Station"),
    )
    assert near < mid < far


def test_stations_outside_radius_are_excluded(app, client, driver):
    with app.app_context():
        make_station(name="Abuja Station", lat=9.06, lng=7.50, owner_email="a@example.com")
        make_station(name="Lagos Station", lat=6.44, lng=3.47, owner_email="l@example.com")

    login(client, driver.email)
    body = client.get(f"/driver/stations?{NEARBY}&radius=25").get_data(as_text=True)
    assert "Abuja Station" in body
    assert "Lagos Station" not in body

    # The radius is clamped to MAX_SEARCH_RADIUS_KM (200 km), so a station
    # ~530 km away in Lagos never shows up on this page.
    body_wide = client.get(f"/driver/stations?{NEARBY}&radius=5000").get_data(as_text=True)
    assert "Lagos Station" not in body_wide
    assert "Abuja Station" in body_wide


def test_mechanics_filtered_by_area_availability_and_approval(app, client, driver):
    with app.app_context():
        make_mechanic(business_name="Available Mech", lat=9.06, lng=7.49, email="a@example.com")
        make_mechanic(
            business_name="Busy Mech", lat=9.06, lng=7.49, available=False, email="b@example.com"
        )
        make_mechanic(
            business_name="Unapproved Mech", lat=9.06, lng=7.49, status="pending", email="c@example.com"
        )
        make_mechanic(
            business_name="Distant Mech", lat=6.44, lng=3.47, radius=10, email="d@example.com"
        )

    login(client, driver.email)
    body = client.get(f"/driver/mechanics?{NEARBY}&radius=25").get_data(as_text=True)

    assert "Available Mech" in body
    assert "Busy Mech" not in body
    assert "Unapproved Mech" not in body
    assert "Distant Mech" not in body


def test_mechanics_sorted_nearest_first(app, client, driver):
    with app.app_context():
        make_mechanic(business_name="Mechanic Far", lat=9.25, lng=7.55, radius=40, email="f@example.com")
        make_mechanic(business_name="Mechanic Near", lat=9.06, lng=7.49, email="n@example.com")

    login(client, driver.email)
    body = client.get(f"/driver/mechanics?{NEARBY}&radius=50").get_data(as_text=True)
    assert body.index("Mechanic Near") < body.index("Mechanic Far")


def test_dashboard_requires_location_to_list_results(app, client, driver):
    with app.app_context():
        # A user with no stored coordinates and no query params.
        driver.latitude = None
        driver.longitude = None
        db.session.commit()
        make_station(name="Invisible Until Located", owner_email="s@example.com")

    login(client, driver.email)
    body = client.get("/driver/stations").get_data(as_text=True)
    assert "We need your location" in body
    assert "Invisible Until Located" not in body

    # Supplying coordinates from the browser makes it appear.
    body = client.get(f"/driver/stations?{NEARBY}").get_data(as_text=True)
    assert "Invisible Until Located" in body


def test_save_location_endpoint_persists_coordinates(app, client, driver):
    login(client, driver.email)
    response = client.post(
        "/driver/location", json={"latitude": 9.1234, "longitude": 7.5678}
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert "lat=9.1234" in payload["redirect"]

    with app.app_context():
        from models import User

        user = User.query.filter_by(email=driver.email).one()
        assert round(user.latitude, 4) == 9.1234
        assert round(user.longitude, 4) == 7.5678


def test_save_location_rejects_invalid_coordinates(app, client, driver):
    login(client, driver.email)
    response = client.post("/driver/location", json={"latitude": 999, "longitude": 0})
    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_mechanic_request_flow_in_area(app, client, driver):
    with app.app_context():
        mechanic = make_mechanic(business_name="In Area Mech", lat=9.06, lng=7.49)
        mechanic_id = mechanic.id

    login(client, driver.email)
    response = client.post(
        "/driver/requests",
        data={
            "mechanic_id": mechanic_id,
            "issue": "Flat tyre",
            "description": "Front left tyre is flat.",
            "vehicle": "Honda Accord",
            "lat": str(DRIVER_LAT),
            "lng": str(DRIVER_LNG),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        service_request = ServiceRequest.query.one()
        assert service_request.status == "pending"
        assert service_request.issue == "Flat tyre"
        assert service_request.distance_km is not None
        assert service_request.reference.startswith("SR-")
        requested_id = service_request.id

    # The mechanic sees it, accepts it, and progresses to completed.
    client.post("/logout")
    login(client, "mechanic@example.com")
    assert "Flat tyre" in client.get("/mechanic/requests").get_data(as_text=True)
    for status in ("accepted", "en_route", "in_progress", "completed"):
        client.post(
            f"/mechanic/requests/{requested_id}/status",
            data={"status": status},
            follow_redirects=True,
        )
        with app.app_context():
            assert db.session.get(ServiceRequest, requested_id).status == status


def test_mechanic_request_outside_service_area_is_rejected(app, client, driver):
    with app.app_context():
        # Mechanic based in Lagos with a 15 km service area.
        mechanic = make_mechanic(business_name="Lagos Mech", lat=6.44, lng=3.47, radius=15)
        mechanic_id = mechanic.id

    login(client, driver.email)
    response = client.post(
        "/driver/requests",
        data={
            "mechanic_id": mechanic_id,
            "issue": "Brakes",
            "lat": str(DRIVER_LAT),
            "lng": str(DRIVER_LNG),
        },
        follow_redirects=True,
    )
    assert b"only covers" in response.data
    with app.app_context():
        assert ServiceRequest.query.count() == 0


def test_fuel_type_filter(app, client, driver):
    with app.app_context():
        make_station(
            name="Diesel Depot",
            lat=9.06,
            lng=7.50,
            owner_email="d@example.com",
            products=(("Diesel (AGO)", 1100.0),),
        )
        make_station(
            name="Petrol Point",
            lat=9.07,
            lng=7.51,
            owner_email="p@example.com",
            products=(("Petrol (PMS)", 900.0),),
        )

    login(client, driver.email)
    body = client.get(f"/driver/stations?{NEARBY}&fuel_type=Diesel%20(AGO)").get_data(as_text=True)
    assert "Diesel Depot" in body
    assert "Petrol Point" not in body
