"""Shared pytest fixtures.

Each test gets a fresh in-memory SQLite database plus a set of factory helpers,
so tests never touch instance/fuelnow.sqlite3.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable when pytest is run from anywhere.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_DRIVER,
    ROLE_MECHANIC,
    ROLE_STATION,
    STATUS_APPROVED,
    STATUS_PENDING,
    FuelProduct,
    Mechanic,
    Station,
    User,
)

PASSWORD = "FuelNow!2026"

# Driver starts in Maitama, Abuja.
DRIVER_LAT, DRIVER_LNG = 9.0579, 7.4951


@pytest.fixture()
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_token(client, path="/login") -> str:
    """Fetch a page and pull the CSRF token out of the rendered form."""
    html = client.get(path).get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    return html[start : html.find('"', start)]


@pytest.fixture()
def csrf(app):
    """Return a function providing a valid CSRF token for a client."""
    return lambda client, path="/login": csrf_token(client, path)


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
def make_user(
    email="driver@example.com",
    role=ROLE_DRIVER,
    status=STATUS_APPROVED,
    full_name="Test User",
    lat=None,
    lng=None,
):
    user = User(
        email=email,
        full_name=full_name,
        role=role,
        status=status,
        email_verified=True,  # emails are verified unless a test says otherwise
    )
    user.set_password(PASSWORD)
    if lat is not None:
        user.latitude, user.longitude = lat, lng
    db.session.add(user)
    db.session.commit()
    return user


def make_station(
    name="Test Station",
    lat=9.0778,
    lng=7.4922,
    status=STATUS_APPROVED,
    is_open=True,
    owner_email="station@example.com",
    products=(("Petrol (PMS)", 900.0),),
):
    owner = User.query.filter_by(email=owner_email).first()
    if owner is None:
        owner = make_user(
            email=owner_email, role=ROLE_STATION, status=status, full_name="Operator"
        )
    station = Station(
        name=name,
        owner_id=owner.id,
        address="1 Test Road",
        city="Abuja",
        latitude=lat,
        longitude=lng,
        status=status,
        is_open=is_open,
    )
    db.session.add(station)
    db.session.flush()
    for fuel_type, price in products:
        db.session.add(
            FuelProduct(station_id=station.id, fuel_type=fuel_type, price_per_litre=price)
        )
    db.session.commit()
    return station


def make_mechanic(
    business_name="Test Mechanic",
    lat=9.0700,
    lng=7.4800,
    radius=15.0,
    available=True,
    status=STATUS_APPROVED,
    email="mechanic@example.com",
):
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = make_user(
            email=email, role=ROLE_MECHANIC, status=status, full_name="Mechanic Owner"
        )
        db.session.commit()
    mechanic = Mechanic(
        user_id=user.id,
        business_name=business_name,
        specialties="General repairs",
        city="Abuja",
        latitude=lat,
        longitude=lng,
        service_radius_km=radius,
        available=available,
        status=status,
    )
    db.session.add(mechanic)
    db.session.commit()
    return mechanic


def login(client, email, password=PASSWORD):
    token = csrf_token(client, "/login")
    return client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
        follow_redirects=True,
    )


@pytest.fixture()
def driver(app):
    return make_user(
        email="driver@example.com",
        role=ROLE_DRIVER,
        full_name="Amina Driver",
        lat=DRIVER_LAT,
        lng=DRIVER_LNG,
    )


@pytest.fixture()
def admin(app):
    return make_user(
        email="admin@example.com", role=ROLE_ADMIN, status=STATUS_APPROVED, full_name="Admin"
    )
