"""Core flow: authentication and role-restricted access."""
import pytest

from extensions import db
from models import ROLE_ADMIN, ROLE_DRIVER, ROLE_MECHANIC, ROLE_STATION, User
from tests.conftest import PASSWORD, login, make_mechanic, make_station, make_user

# Pages that each role must be able to reach.
ROLE_HOME = {
    ROLE_DRIVER: "/driver/",
    ROLE_STATION: "/station/",
    ROLE_MECHANIC: "/mechanic/",
    ROLE_ADMIN: "/admin/",
}

# A representative page per role that *other* roles must not reach.
ROLE_PROTECTED = {
    ROLE_DRIVER: "/driver/orders",
    ROLE_STATION: "/station/orders",
    ROLE_MECHANIC: "/mechanic/requests",
    ROLE_ADMIN: "/admin/users",
}

ROLE_EMAILS = {
    ROLE_DRIVER: "driver@example.com",
    ROLE_STATION: "station@example.com",
    ROLE_MECHANIC: "mechanic@example.com",
    ROLE_ADMIN: "admin@example.com",
}


@pytest.fixture()
def all_roles(app):
    """Create one approved account (plus profile record) per role."""
    make_user(email="driver@example.com", role=ROLE_DRIVER, full_name="Driver One")
    make_station(owner_email="station@example.com")
    make_mechanic(email="mechanic@example.com")
    make_user(email="admin@example.com", role=ROLE_ADMIN, full_name="Admin One")
    return ROLE_EMAILS


def test_anonymous_users_are_redirected_to_login(client):
    for path in ROLE_PROTECTED.values():
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_each_role_reaches_its_own_page(app, client, all_roles):
    for role, email in all_roles.items():
        client.post("/logout")
        assert login(client, email).status_code == 200
        response = client.get(ROLE_HOME[role])
        assert response.status_code == 200, f"{role} could not open {ROLE_HOME[role]}"


def test_each_role_is_forbidden_from_other_pages(app, client, all_roles):
    for role, email in all_roles.items():
        for other_role, path in ROLE_PROTECTED.items():
            if other_role == role:
                continue
            client.post("/logout")
            login(client, email)
            response = client.get(path)
            assert response.status_code == 403, (
                f"{role} should not reach {path} (got {response.status_code})"
            )
            assert b"Access denied" in response.data


def test_login_redirects_to_role_home(app, client, all_roles):
    expected = {
        "driver@example.com": "/driver/",
        "station@example.com": "/station/",
        "mechanic@example.com": "/mechanic/",
        "admin@example.com": "/admin/",
    }
    for email, path in expected.items():
        client.post("/logout")
        response = client.post(
            "/login", data={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith(path)


def test_wrong_password_is_rejected(client, all_roles):
    response = client.post(
        "/login", data={"email": "driver@example.com", "password": "nope"}
    )
    assert response.status_code == 401
    assert b"Invalid email or password" in response.data


def test_deactivated_user_cannot_log_in(app, client):
    user = make_user(email="gone@example.com", role=ROLE_DRIVER)
    user.status = "deactivated"
    user.is_active_flag = False
    db.session.commit()

    response = client.post("/login", data={"email": "gone@example.com", "password": PASSWORD})
    assert response.status_code == 403
    assert b"deactivated" in response.data


def test_pending_station_operator_sees_pending_page(app, client):
    make_station(status="pending", owner_email="waiting@example.com")
    response = login(client, "waiting@example.com")
    assert response.status_code == 200
    # Redirected away from the dashboard to the pending screen.
    assert b"awaiting approval" in response.data
    assert client.get("/station/orders").status_code == 302


def test_driver_signup_creates_working_account(app, client):
    response = client.post(
        "/register",
        data={
            "role": "driver",
            "full_name": "New Driver",
            "email": "newdriver@example.com",
            "phone": "+2348000000001",
            "password": "FuelNow!2026",
            "confirm_password": "FuelNow!2026",
            "latitude": "9.05",
            "longitude": "7.49",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        user = User.query.filter_by(email="newdriver@example.com").one()
        assert user.role == ROLE_DRIVER
        assert user.status == "approved"
        assert user.check_password("FuelNow!2026")
    assert client.get("/driver/").status_code == 200


def test_station_signup_creates_pending_station(app, client):
    response = client.post(
        "/register",
        data={
            "role": "station",
            "full_name": "Operator Name",
            "email": "newstation@example.com",
            "password": "FuelNow!2026",
            "confirm_password": "FuelNow!2026",
            "station_name": "New Fuel Hub",
            "city": "Abuja",
            "address": "12 Somewhere Road",
            "latitude": "9.08",
            "longitude": "7.49",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        user = User.query.filter_by(email="newstation@example.com").one()
        assert user.role == ROLE_STATION
        assert user.status == "pending"
        assert user.station is not None
        assert user.station.name == "New Fuel Hub"
        assert user.station.status == "pending"


def test_signup_validation_errors(app, client):
    # Mismatched passwords
    response = client.post(
        "/register",
        data={
            "role": "driver",
            "full_name": "Bad",
            "email": "bad@example.com",
            "password": "FuelNow!2026",
            "confirm_password": "Different123",
        },
    )
    assert response.status_code == 400
    assert b"Passwords do not match" in response.data

    # Duplicate email
    make_user(email="taken@example.com", role=ROLE_DRIVER)
    response = client.post(
        "/register",
        data={
            "role": "driver",
            "full_name": "Dup",
            "email": "taken@example.com",
            "password": "FuelNow!2026",
            "confirm_password": "FuelNow!2026",
        },
    )
    assert b"already exists" in response.data

    # Station without coordinates
    response = client.post(
        "/register",
        data={
            "role": "station",
            "full_name": "No Coords",
            "email": "nocoords@example.com",
            "password": "FuelNow!2026",
            "confirm_password": "FuelNow!2026",
            "station_name": "Ghost Station",
            "city": "Abuja",
        },
    )
    assert b"valid latitude and longitude" in response.data


def test_admin_role_cannot_be_self_registered(app, client):
    response = client.post(
        "/register",
        data={
            "role": "admin",
            "full_name": "Sneaky",
            "email": "sneaky@example.com",
            "password": "FuelNow!2026",
            "confirm_password": "FuelNow!2026",
        },
    )
    assert response.status_code == 400
    with app.app_context():
        assert User.query.filter_by(email="sneaky@example.com").first() is None


def test_admin_can_approve_and_deactivate(app, client):
    make_user(email="admin@example.com", role=ROLE_ADMIN, full_name="Admin")
    with app.app_context():
        station = make_station(status="pending", owner_email="tobeapproved@example.com")
        station_id = station.id
        driver = make_user(email="victim@example.com", role=ROLE_DRIVER)
        driver_id = driver.id

    login(client, "admin@example.com")

    client.post(
        f"/admin/stations/{station_id}/status", data={"status": "approved"}, follow_redirects=True
    )
    with app.app_context():
        from models import Station

        station = db.session.get(Station, station_id)
        assert station.status == "approved"
        assert station.owner.status == "approved"

    client.post(
        f"/admin/users/{driver_id}/status", data={"status": "deactivated"}, follow_redirects=True
    )
    with app.app_context():
        assert db.session.get(User, driver_id).status == "deactivated"

    client.post("/logout")
    denied = client.post("/login", data={"email": "victim@example.com", "password": PASSWORD})
    assert denied.status_code == 403


def test_account_deletion_requires_confirmation_and_password(app, client):
    driver = make_user(email="delete-me@example.com", role=ROLE_DRIVER, full_name="Delete Me")
    login(client, driver.email)

    wrong_confirmation = client.post(
        "/account",
        data={"action": "delete_account", "delete_password": PASSWORD, "delete_confirmation": "NO"},
        follow_redirects=True,
    )
    assert wrong_confirmation.status_code == 200
    with app.app_context():
        assert db.session.get(User, driver.id).status == "approved"

    deleted = client.post(
        "/account",
        data={"action": "delete_account", "delete_password": PASSWORD, "delete_confirmation": "DELETE"},
        follow_redirects=True,
    )
    assert deleted.status_code == 200

    with app.app_context():
        saved = db.session.get(User, driver.id)
        assert saved.status == "deactivated"
        assert not saved.is_active
        assert saved.full_name == "Deleted account"
        assert saved.email == f"deleted-{driver.id}@deleted.local"


def test_admin_status_change_is_recorded(app, client):
    admin = make_user(email="audit-admin@example.com", role=ROLE_ADMIN, full_name="Audit Admin")
    driver = make_user(email="audit-driver@example.com", role=ROLE_DRIVER, full_name="Audit Driver")

    login(client, admin.email)
    response = client.post(
        f"/admin/users/{driver.id}/status",
        data={"status": "deactivated"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        from models import AuditLog

        log = AuditLog.query.filter_by(target_type="user", target_id=driver.id).one()
        assert log.admin_id == admin.id
        assert log.action == "status_changed"
        assert log.old_status == "approved"
        assert log.new_status == "deactivated"

    audit_page = client.get("/admin/audit")
    assert audit_page.status_code == 200
    assert b"audit-driver@example.com" in audit_page.data


def test_admin_cannot_deactivate_self(app, client):
    admin = make_user(email="admin@example.com", role=ROLE_ADMIN, full_name="Admin")
    login(client, admin.email)
    response = client.post(
        f"/admin/users/{admin.id}/status", data={"status": "deactivated"}, follow_redirects=True
    )
    assert b"cannot change your own account status" in response.data
    with app.app_context():
        assert db.session.get(User, admin.id).status == "approved"


def test_admin_login_is_role_restricted(app, client):
    make_user(email="admin@example.com", role=ROLE_ADMIN, full_name="Admin")
    make_user(email="driver@example.com", role=ROLE_DRIVER, full_name="Driver")

    page = client.get("/admin/login")
    assert page.status_code == 200
    assert b"Administrator sign in" in page.data

    response = client.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": PASSWORD},
    )
    assert response.status_code == 302
    assert "/admin/" in response.location

    client.post("/logout")
    denied = client.post(
        "/admin/login",
        data={"email": "driver@example.com", "password": PASSWORD},
    )
    assert denied.status_code == 403
    assert b"administrators only" in denied.data


def test_logout_clears_session(app, client, all_roles):
    login(client, "driver@example.com")
    assert client.get("/driver/").status_code == 200
    client.post("/logout", follow_redirects=True)
    assert client.get("/driver/").status_code == 302


def test_health_and_home_pages_public(client):
    assert client.get("/").status_code == 200
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
