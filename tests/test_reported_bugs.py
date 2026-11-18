"""Regression tests for issues found during the project review."""
import pytest

from extensions import db
from models import Mechanic, ServiceRequest, User
from tests.conftest import login, make_mechanic, make_user


def test_invalid_mechanic_request_input_returns_validation_error(app, client, driver):
    with app.app_context():
        mechanic = make_mechanic()
        mechanic_id = mechanic.id

    login(client, driver.email)
    response = client.post(
        "/driver/requests",
        data={"mechanic_id": mechanic_id, "issue": "not a supported issue"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"issue" in response.data.lower()
    with app.app_context():
        assert ServiceRequest.query.count() == 0


def test_admin_status_change_rejects_external_next_url(app, client):
    admin = make_user(email="redirect-admin@example.com", role="admin")
    target = make_user(
        email="redirect-target@example.com", role="driver", status="pending"
    )
    login(client, admin.email)

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "approved", "next": "https://example.org"},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/admin/users"
    with app.app_context():
        assert db.session.get(User, target.id).status == "approved"


def test_invalid_service_radius_does_not_save_other_availability_changes(
    app, client
):
    with app.app_context():
        mechanic = make_mechanic(email="radius-mechanic@example.com", radius=20)
        mechanic_id = mechanic.id

    login(client, "radius-mechanic@example.com")
    response = client.post(
        "/mechanic/availability",
        data={"service_radius_km": "invalid", "city": "Changed City"},
    )

    assert response.status_code == 302
    with app.app_context():
        mechanic = db.session.get(Mechanic, mechanic_id)
        assert mechanic.service_radius_km == 20
        assert mechanic.available is True
        assert mechanic.city == "Abuja"


def test_invalid_hourly_rate_does_not_save_profile_changes(app, client):
    with app.app_context():
        mechanic = make_mechanic(email="rate-mechanic@example.com")
        mechanic_id = mechanic.id

    login(client, "rate-mechanic@example.com")
    response = client.post(
        "/mechanic/profile",
        data={
            "business_name": "Changed Name",
            "specialties": "Brakes",
            "city": "Changed City",
            "full_name": "Changed User",
            "phone": "",
            "hourly_rate": "not-a-number",
        },
    )

    assert response.status_code == 302
    with app.app_context():
        mechanic = db.session.get(Mechanic, mechanic_id)
        assert mechanic.business_name == "Test Mechanic"
        assert mechanic.city == "Abuja"
        assert mechanic.hourly_rate is None
        assert mechanic.user.full_name == "Mechanic Owner"


def test_registration_rejects_invalid_mechanic_service_radius(app, client):
    response = client.post(
        "/register",
        data={
            "role": "mechanic",
            "full_name": "New Mechanic",
            "email": "new-mechanic@example.com",
            "password": "FuelNow!2026",
            "confirm_password": "FuelNow!2026",
            "business_name": "New Repairs",
            "city": "Abuja",
            "latitude": "9.05",
            "longitude": "7.49",
            "service_radius_km": "invalid",
        },
    )

    assert response.status_code == 400
    with app.app_context():
        assert User.query.filter_by(email="new-mechanic@example.com").first() is None


def test_configured_smtp_sends_verification_and_password_reset(app, client, monkeypatch):
    from email import message_from_string

    messages = []

    class FakeSMTP:
        def __init__(self, server, port, timeout):
            assert server == "smtp.example.org"
            assert port == 2525
            assert timeout == 10

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            raise AssertionError("TLS was disabled for this test")

        def login(self, _username, _password):
            pass

        def send_message(self, message):
            messages.append(message_from_string(message.as_string()))

    import blueprints.auth as auth

    monkeypatch.setattr(auth.smtplib, "SMTP", FakeSMTP)
    app.config.update(
        MAIL_SERVER="smtp.example.org",
        MAIL_PORT=2525,
        MAIL_USE_TLS=False,
        MAIL_USERNAME=None,
        MAIL_PASSWORD=None,
        MAIL_FROM="no-reply@example.org",
        EMAIL_VERIFICATION_REQUIRED=True,
        SHOW_VERIFICATION_LINK=False,
    )

    registration = client.post(
        "/register",
        data={
            "role": "driver",
            "full_name": "New Driver",
            "email": "mail-driver@example.com",
            "password": "FuelNow!2026",
            "confirm_password": "FuelNow!2026",
        },
        follow_redirects=True,
    )
    assert registration.status_code == 200
    assert len(messages) == 1
    assert messages[0]["Subject"] == "Verify your Veyro account"
    assert "verify?token=" in messages[0].get_payload()

    reset = client.post(
        "/forgot-password",
        data={"email": "mail-driver@example.com"},
        follow_redirects=True,
    )
    assert reset.status_code == 200
    assert len(messages) == 2
    assert messages[1]["Subject"] == "Reset your Veyro password"
    assert "reset-password?token=" in messages[1].get_payload()


def test_production_requires_smtp_configuration(monkeypatch):
    from flask import Flask

    from app import _check_production_safety
    from config import ProductionConfig

    monkeypatch.setenv("FLASK_CONFIG", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.example.org/fuelnow")
    monkeypatch.setattr(ProductionConfig, "MAIL_SERVER", None)

    app = Flask(__name__)
    app.config.from_object(ProductionConfig)
    app.config["_config_object"] = ProductionConfig()
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["COOKIE_LESS_SESSIONS"] = False

    with pytest.raises(RuntimeError, match="MAIL_SERVER"):
        _check_production_safety(app)
