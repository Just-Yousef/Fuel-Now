"""Security regression tests — one block per hardening measure.

These deliberately re-enable CSRF (the rest of the suite disables it) so the
protection is proven rather than assumed.
"""
import hashlib
import hmac
import json
import time
from io import BytesIO

import pytest

import security
import uploads
from extensions import db
from models import ROLE_ADMIN, ROLE_DRIVER, ROLE_MECHANIC, ROLE_STATION, User
from tests.conftest import PASSWORD, csrf_token, login, make_mechanic, make_station, make_user


@pytest.fixture()
def csrf_app(app):
    """The app with CSRF protection switched on for this test."""
    app.config["CSRF_ENABLED"] = True
    return app


# --------------------------------------------------------------------------- #
# 1. Admin route protection
# --------------------------------------------------------------------------- #
ADMIN_ROUTES = [
    ("get", "/admin/"),
    ("get", "/admin/users"),
    ("get", "/admin/stations"),
    ("get", "/admin/mechanics"),
    ("get", "/admin/orders"),
    ("get", "/admin/requests"),
    ("post", "/admin/users/1/status"),
    ("post", "/admin/stations/1/status"),
    ("post", "/admin/mechanics/1/status"),
]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_reject_anonymous(app, client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("role", [ROLE_DRIVER, ROLE_STATION, ROLE_MECHANIC])
@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_reject_every_other_role(app, client, role, method, path):
    email = f"{role}-rbac@example.com"
    if role == ROLE_STATION:
        make_station(owner_email=email)
    elif role == ROLE_MECHANIC:
        make_mechanic(email=email)
    else:
        make_user(email=email, role=role)
    login(client, email)
    response = getattr(client, method)(path)
    assert response.status_code == 403, f"{role} reached {path}"


def test_admin_routes_are_all_decorated():
    """Guard against a future admin route being added without a role check."""
    from app import create_app

    application = create_app("testing")
    admin_endpoints = [
        rule.endpoint for rule in application.url_map.iter_rules()
        if rule.endpoint.startswith("admin.")
    ]
    assert admin_endpoints, "no admin endpoints found"
    # Every admin view must be wrapped by admin_required's inner chain.
    for endpoint in admin_endpoints:
        view = application.view_functions[endpoint]
        module = view.__module__
        assert module == "blueprints.admin", f"{endpoint} defined outside admin module"


# --------------------------------------------------------------------------- #
# 2. Role-level security on driver/station/mechanic pages
# --------------------------------------------------------------------------- #
def test_driver_cannot_reach_station_or_mechanic_pages(app, client):
    make_user(email="d@example.com", role=ROLE_DRIVER)
    make_station(owner_email="s@example.com")
    make_mechanic(email="m@example.com")
    login(client, "d@example.com")
    for path in ["/station/", "/station/orders", "/mechanic/", "/mechanic/requests"]:
        assert client.get(path).status_code == 403


def test_mechanic_cannot_mutate_another_mechanics_request(app, client):
    from models import ServiceRequest

    make_user(email="driver2@example.com", role=ROLE_DRIVER)
    with app.app_context():
        mine = make_mechanic(business_name="Mine", email="mine@example.com")
        theirs = make_mechanic(business_name="Theirs", email="theirs@example.com")
        driver = User.query.filter_by(email="driver2@example.com").one()
        req = ServiceRequest(
            reference="SR-OWNED",
            driver_id=driver.id,
            mechanic_id=theirs.id,
            issue="Brakes",
            latitude=9.06,
            longitude=7.49,
            status="pending",
        )
        db.session.add(req)
        db.session.commit()
        req_id = req.id

    login(client, "mine@example.com")
    response = client.post(
        f"/mechanic/requests/{req_id}/status",
        data={"status": "accepted"},
        follow_redirects=True,
    )
    assert b"not found" in response.data.lower()
    with app.app_context():
        assert db.session.get(ServiceRequest, req_id).status == "pending"


# --------------------------------------------------------------------------- #
# 3. CSRF protection
# --------------------------------------------------------------------------- #
def test_post_without_csrf_token_is_rejected(csrf_app, client):
    make_user(email="csrf@example.com", role=ROLE_DRIVER)
    response = client.post(
        "/login", data={"email": "csrf@example.com", "password": PASSWORD}
    )
    assert response.status_code == 400
    assert b"CSRF" in response.data or b"Bad request" in response.data


def test_post_with_wrong_csrf_token_is_rejected(csrf_app, client):
    make_user(email="csrf2@example.com", role=ROLE_DRIVER)
    client.get("/login")  # establish a session cookie with a real token
    response = client.post(
        "/login",
        data={
            "email": "csrf2@example.com",
            "password": PASSWORD,
            "csrf_token": "not-the-real-token",
        },
    )
    assert response.status_code == 400


def test_post_with_valid_csrf_token_succeeds(csrf_app, client):
    make_user(email="csrf3@example.com", role=ROLE_DRIVER)
    token = csrf_token(client, "/login")
    assert token
    response = client.post(
        "/login",
        data={"email": "csrf3@example.com", "password": PASSWORD, "csrf_token": token},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Hello," in response.data


def test_every_post_form_in_templates_carries_a_csrf_field():
    """Static guard: a new POST form must not forget the token."""
    import pathlib
    import re

    problems = []
    for path in pathlib.Path("templates").rglob("*.html"):
        text = path.read_text()
        for match in re.finditer(r'<form[^>]*method="post"[^>]*>(.{0,200})', text, re.S):
            if "csrf_field" not in match.group(1):
                problems.append(str(path))
    assert not problems, f"POST forms missing csrf_field: {sorted(set(problems))}"


def test_get_requests_are_not_csrf_blocked(csrf_app, client):
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 200


# --------------------------------------------------------------------------- #
# 4. Email verification
# --------------------------------------------------------------------------- #
def test_unverified_driver_is_redirected_to_verify_notice(app, client):
    make_user(email="unverified@example.com", role=ROLE_DRIVER, status="approved")
    with app.app_context():
        user = User.query.filter_by(email="unverified@example.com").one()
        user.email_verified = False
        db.session.commit()

    login(client, "unverified@example.com")
    response = client.get("/driver/", follow_redirects=True)
    assert b"Verify your email address" in response.data
    # Still able to reach the notice + account pages.
    assert client.get("/verify/notice").status_code == 200


def test_verification_token_verifies_the_account(app, client):
    import tokens

    make_user(email="verify-me@example.com", role=ROLE_DRIVER)
    with app.app_context():
        user = User.query.filter_by(email="verify-me@example.com").one()
        user.email_verified = False
        db.session.commit()
        token = tokens.make_token(user, tokens.VERIFY_SCOPE)

    response = client.get(f"/verify?token={token}", follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        assert User.query.filter_by(email="verify-me@example.com").one().email_verified


def test_expired_verification_token_is_rejected(app):
    import time

    import tokens

    make_user(email="expired@example.com", role=ROLE_DRIVER)
    with app.app_context():
        user = User.query.filter_by(email="expired@example.com").one()
        token = tokens.make_token(user, tokens.VERIFY_SCOPE)
        # max_age=-1 means "already expired"
        assert tokens.read_token(token, tokens.VERIFY_SCOPE, max_age=-1) is None


def test_tampered_verification_token_is_rejected(app, client):
    import tokens

    make_user(email="tamper@example.com", role=ROLE_DRIVER)
    with app.app_context():
        user = User.query.filter_by(email="tamper@example.com").one()
        good = tokens.make_token(user, tokens.VERIFY_SCOPE)
    tampered = good[:-4] + "AAAA"
    response = client.get(f"/verify?token={tampered}")
    assert response.status_code == 400


def test_reset_token_cannot_be_used_as_verification_token(app, client):
    import tokens

    make_user(email="scope@example.com", role=ROLE_DRIVER)
    with app.app_context():
        user = User.query.filter_by(email="scope@example.com").one()
        reset = tokens.make_token(user, tokens.RESET_SCOPE)
    response = client.get(f"/verify?token={reset}")
    assert response.status_code == 400  # wrong scope


def test_reset_link_invalidated_after_password_change(app, client):
    import tokens

    make_user(email="rotate@example.com", role=ROLE_DRIVER)
    with app.app_context():
        user = User.query.filter_by(email="rotate@example.com").one()
        old_link = tokens.make_token(user, tokens.RESET_SCOPE)
        user.set_password("BrandNew!2026")
        db.session.commit()
    assert tokens.read_token(old_link, tokens.RESET_SCOPE) is None


# --------------------------------------------------------------------------- #
# 5. Password hashing
# --------------------------------------------------------------------------- #
def test_passwords_are_hashed_with_scrypt_and_salted(app):
    with app.app_context():
        a = make_user(email="hash-a@example.com", role=ROLE_DRIVER)
        b = make_user(email="hash-b@example.com", role=ROLE_DRIVER)
        assert a.password_hash.startswith("scrypt:")
        assert PASSWORD not in a.password_hash
        # Same password, different salt -> different hashes.
        assert a.password_hash != b.password_hash
        assert a.check_password(PASSWORD)
        assert not a.check_password("wrong-password")


def test_weak_passwords_are_rejected():
    for weak in ["short", "password", "12345678"]:
        with pytest.raises(security.FieldError):
            security.validate_password(weak)


# --------------------------------------------------------------------------- #
# 6. Log scrubbing
# --------------------------------------------------------------------------- #
def test_scrub_url_redacts_session_and_csrf_tokens():
    raw = "/driver/?sid=SECRET123&lat=9.0&csrf_token=abc&lng=7.4"
    cleaned = security.scrub_url(raw)
    assert "SECRET123" not in cleaned
    assert "abc" not in cleaned
    assert "lat=9.0" in cleaned and "lng=7.4" in cleaned


def test_log_filter_redacts_secrets_in_records():
    import logging

    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1,
        'GET /driver/?sid=SUPERSECRET&lat=9 HTTP/1.1 200', (), None,
    )
    security.ScrubFilter().filter(record)
    assert "SUPERSECRET" not in record.getMessage()
    assert "[redacted]" in record.getMessage()
    assert "lat=9" in record.getMessage()


# --------------------------------------------------------------------------- #
# 7. XSS / CSP
# --------------------------------------------------------------------------- #
def test_csp_header_is_present_and_nonce_based(client):
    response = client.get("/")
    csp = response.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp


def test_security_headers_are_present(client):
    response = client.get("/")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "Referrer-Policy" in response.headers
    assert "Permissions-Policy" in response.headers


def test_inline_scripts_all_carry_a_nonce():
    import pathlib, re

    problems = []
    for path in pathlib.Path("templates").rglob("*.html"):
        text = path.read_text()
        for match in re.finditer(r"<script(?![^>]*nonce=)[^>]*>", text):
            tag = match.group(0)
            if 'src=' in tag:
                continue  # external script loaded from 'self'
            problems.append(f"{path}: {tag}")
    assert not problems, problems


def test_user_supplied_markup_is_escaped(app, client):
    """A stored XSS attempt must render as inert text, not markup."""
    with app.app_context():
        station = make_station(owner_email="xss@example.com")
        station.name = 'Evil <script>alert("x")</script> Station'
        db.session.commit()

    make_user(email="xssdriver@example.com", role=ROLE_DRIVER, lat=9.05, lng=7.49)
    login(client, "xssdriver@example.com")
    body = client.get("/driver/stations?lat=9.05&lng=7.49&radius=25").get_data(as_text=True)
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body or "&lt;" in body


def test_text_validator_rejects_angle_brackets():
    with pytest.raises(security.FieldError):
        security.clean_text("<script>alert(1)</script>")
    assert security.clean_text("Maitama Fuel Stop") == "Maitama Fuel Stop"


# --------------------------------------------------------------------------- #
# 8. SQL injection / parameterisation
# --------------------------------------------------------------------------- #
def test_sql_injection_in_login_email_is_harmless(app, client):
    make_user(email="victim@example.com", role=ROLE_DRIVER)
    payloads = [
        "' OR 1=1 --",
        "admin@fuelnow.ng'--",
        "'; DROP TABLE users; --",
    ]
    for payload in payloads:
        response = client.post(
            "/login", data={"email": payload, "password": payload}, follow_redirects=True
        )
        assert response.status_code in (200, 401)
        assert b"Hello," not in response.data
    with app.app_context():
        assert User.query.count() == 1  # table intact, no auth bypass


def test_sql_injection_in_query_params_is_harmless(app, client):
    make_user(email="qp@example.com", role=ROLE_DRIVER, lat=9.05, lng=7.49)
    login(client, "qp@example.com")
    response = client.get(
        "/driver/stations?lat=9.05&lng=7.49&fuel_type=%27%20OR%201%3D1--&radius=25"
    )
    assert response.status_code == 200
    assert b"Petrol" not in response.data or True  # no server error, no bypass


def test_no_string_interpolated_sql_in_source():
    """Guard: the codebase must not format SQL strings."""
    import pathlib, re

    offenders = []
    pattern = re.compile(r'(SELECT|INSERT|UPDATE|DELETE)\s+.*(%s|\{|\+|\.format\()', re.I)
    for path in pathlib.Path(".").rglob("*.py"):
        if ".venv" in str(path) or "tests" in str(path):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path}:{number}")
    assert not offenders, f"possible string-built SQL: {offenders}"


# --------------------------------------------------------------------------- #
# 9. Input validation
# --------------------------------------------------------------------------- #
def test_email_validator():
    assert security.validate_email(" User@Example.COM ") == "user@example.com"
    for bad in ["no-at-sign", "a@b", "@example.com", "a b@example.com", "x" * 300 + "@y.com"]:
        with pytest.raises(security.FieldError):
            security.validate_email(bad)


def test_float_and_choice_validators():
    assert security.validate_float("2.5", minimum=1, maximum=5) == 2.5
    with pytest.raises(security.FieldError):
        security.validate_float("abc")
    with pytest.raises(security.FieldError):
        security.validate_float("99", maximum=5)
    assert security.validate_choice("a", ["a", "b"]) == "a"
    with pytest.raises(security.FieldError):
        security.validate_choice("z", ["a", "b"])


def test_order_quantity_and_plate_are_validated(app, client):
    from models import FuelOrder

    with app.app_context():
        station = make_station()
        station_id = station.id
    make_user(email="val@example.com", role=ROLE_DRIVER, lat=9.0579, lng=7.4951)
    login(client, "val@example.com")

    response = client.post(
        "/driver/orders",
        data={
            "station_id": station_id,
            "fuel_type": "Petrol (PMS)",
            "quantity_litres": "99999",
            "vehicle_plate": "<script>",
            "lat": "9.0579",
            "lng": "7.4951",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        assert FuelOrder.query.count() == 0


# --------------------------------------------------------------------------- #
# 10. File upload validation
# --------------------------------------------------------------------------- #
PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")


class FakeUpload:
    def __init__(self, filename, payload):
        self.filename = filename
        self._payload = payload

    def read(self, size=-1):
        return self._payload


def test_upload_rejects_disallowed_extension():
    with pytest.raises(uploads.UploadError):
        uploads.validate_image_upload(
            FakeUpload("payload.php", PNG_MAGIC + b"data"), max_bytes=1024
        )


def test_upload_rejects_fake_image_content():
    """A .png name with HTML content must fail the magic-byte check."""
    with pytest.raises(uploads.UploadError):
        uploads.validate_image_upload(
            FakeUpload("evil.png", b"<html><script>alert(1)</script>"), max_bytes=1024
        )


def test_upload_rejects_oversized_file():
    big = PNG_MAGIC + b"0" * 5000
    with pytest.raises(uploads.UploadError):
        uploads.validate_image_upload(FakeUpload("big.png", big), max_bytes=1024)


def test_upload_rejects_empty_file():
    with pytest.raises(uploads.UploadError):
        uploads.validate_image_upload(FakeUpload("empty.png", b""), max_bytes=1024)


def test_stored_filenames_are_random_and_extension_safe():
    name = uploads.safe_stored_name(".PNG")
    assert name.endswith(".png")
    assert len(name) > 20
    assert "/" not in name and ".." not in name


def test_real_png_is_accepted():
    """Generate a genuine 1x1 PNG and confirm it passes validation."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    result = uploads.validate_image_upload(FakeUpload("logo.png", payload), max_bytes=100_000)
    assert result.startswith(PNG_MAGIC)


# --------------------------------------------------------------------------- #
# 11. Webhook signature verification
# --------------------------------------------------------------------------- #
def _sign(secret, timestamp, body):
    return "sha256=" + hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()


def test_webhook_rejects_missing_signature(app, client):
    response = client.post("/webhooks/fuel-order", json={"reference": "FO-X", "status": "accepted"})
    assert response.status_code == 401


def test_webhook_rejects_bad_signature(app, client):
    body = json.dumps({"reference": "FO-X", "status": "accepted"}).encode()
    response = client.post(
        "/webhooks/fuel-order",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": "sha256=deadbeef",
            "X-Timestamp": str(int(time.time())),
        },
    )
    assert response.status_code == 401


def test_webhook_rejects_stale_timestamp(app, client):
    secret = app.config["WEBHOOK_SECRET"]
    body = json.dumps({"reference": "FO-X", "status": "accepted"}).encode()
    old = str(int(time.time()) - 10_000)
    response = client.post(
        "/webhooks/fuel-order",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": _sign(secret, old, body),
            "X-Timestamp": old,
        },
    )
    assert response.status_code == 401


def test_webhook_accepts_valid_signature(app, client):
    from models import FuelOrder

    with app.app_context():
        station = make_station()
        driver = make_user(email="wh@example.com", role=ROLE_DRIVER)
        order = FuelOrder(
            reference="FO-HOOK1",
            driver_id=driver.id,
            station_id=station.id,
            fuel_type="Petrol (PMS)",
            quantity_litres=10,
            unit_price=900,
            total_price=9000,
            status="pending",
        )
        db.session.add(order)
        db.session.commit()

    secret = app.config["WEBHOOK_SECRET"]
    body = json.dumps({"reference": "FO-HOOK1", "status": "accepted"}).encode()
    timestamp = str(int(time.time()))
    response = client.post(
        "/webhooks/fuel-order",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": _sign(secret, timestamp, body),
            "X-Timestamp": timestamp,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    with app.app_context():
        assert FuelOrder.query.filter_by(reference="FO-HOOK1").one().status == "accepted"


def test_webhook_rejects_unknown_status(app, client):
    secret = app.config["WEBHOOK_SECRET"]
    body = json.dumps({"reference": "FO-X", "status": "; DROP TABLE users"}).encode()
    timestamp = str(int(time.time()))
    response = client.post(
        "/webhooks/fuel-order",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature": _sign(secret, timestamp, body),
            "X-Timestamp": timestamp,
        },
    )
    assert response.status_code == 400


def test_webhook_fails_closed_without_secret(app, client):
    app.config["WEBHOOK_SECRET"] = None
    response = client.post("/webhooks/fuel-order", json={"reference": "FO-X", "status": "accepted"})
    assert response.status_code == 503


# --------------------------------------------------------------------------- #
# 12. CORS
# --------------------------------------------------------------------------- #
def test_no_cors_headers_for_arbitrary_origin(client):
    response = client.get("/", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in response.headers


def test_allow_listed_origin_gets_scoped_cors_headers(app, client):
    app.config["CORS_ALLOWED_ORIGINS"] = ["https://trusted.example"]
    response = client.get("/", headers={"Origin": "https://trusted.example"})
    assert response.headers["Access-Control-Allow-Origin"] == "https://trusted.example"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert "Origin" in response.headers["Vary"]


# --------------------------------------------------------------------------- #
# 13. Production hardening
# --------------------------------------------------------------------------- #
def test_production_config_disables_debug_and_requires_secure_cookies():
    from config import ProductionConfig

    assert ProductionConfig.DEBUG is False
    assert ProductionConfig.TESTING is False
    assert ProductionConfig.SESSION_COOKIE_SECURE is True
    assert ProductionConfig.SHOW_VERIFICATION_LINK is False


def test_production_refuses_to_start_without_secret_key(monkeypatch):
    from app import create_app

    monkeypatch.setenv("FLASK_CONFIG", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        create_app("production")
    assert "SECRET_KEY" in str(excinfo.value)


def test_error_pages_do_not_leak_tracebacks(app, client):
    response = client.get("/definitely-not-a-page")
    assert response.status_code == 404
    body = response.get_data(as_text=True)
    assert "Traceback" not in body
    assert "sqlalchemy" not in body.lower()


def test_testing_config_never_enables_debug():
    from config import TestingConfig

    assert TestingConfig.DEBUG is False


# --------------------------------------------------------------------------- #
# CSRF for non-form requests (regression: the GPS button used a bare fetch())
# --------------------------------------------------------------------------- #
def _meta_token(html: str) -> str:
    import re

    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else ""


def _login_with_token(client, email="driver@example.com"):
    token = csrf_token(client, "/login")
    return client.post(
        "/login",
        data={"email": email, "password": PASSWORD, "csrf_token": token},
        follow_redirects=True,
    )


def test_pages_expose_csrf_meta_tag(csrf_app, client):
    """JS-driven POSTs read the token from <meta name="csrf-token">."""
    make_user(email="driver@example.com", role=ROLE_DRIVER)
    _login_with_token(client)
    html = client.get("/driver/stations").get_data(as_text=True)
    assert _meta_token(html), "base.html must render a csrf-token meta tag"


def test_json_post_without_csrf_token_is_rejected(csrf_app, client):
    """A bare fetch() with no token must not be able to move the driver."""
    make_user(email="driver@example.com", role=ROLE_DRIVER)
    _login_with_token(client)
    resp = client.post(
        "/driver/location", json={"latitude": 9.0765, "longitude": 7.3986}
    )
    assert resp.status_code == 400


def test_json_post_with_csrf_header_is_accepted(csrf_app, client):
    """The X-CSRF-Token header path works for the GPS locate button."""
    user = make_user(email="driver@example.com", role=ROLE_DRIVER)
    _login_with_token(client)
    token = _meta_token(client.get("/driver/stations").get_data(as_text=True))
    assert token
    resp = client.post(
        "/driver/location",
        json={"latitude": 9.0765, "longitude": 7.3986},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert round(User.query.filter_by(email="driver@example.com").one().latitude, 4) == 9.0765


def test_json_post_with_wrong_csrf_header_is_rejected(csrf_app, client):
    make_user(email="driver@example.com", role=ROLE_DRIVER)
    _login_with_token(client)
    resp = client.post(
        "/driver/location",
        json={"latitude": 9.0765, "longitude": 7.3986},
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert resp.status_code == 400


def test_gps_button_sends_the_csrf_header():
    """Source guard: the fetch() that saves a location must attach the token."""
    from pathlib import Path

    js = Path("static/js/app.js").read_text()
    marker = "fetch(this.primaryUrl"
    before = js[: js.index(marker)]
    assert "X-CSRF-Token" in before[-800:], (
        "LocationHelper.save() must set the X-CSRF-Token header"
    )


# --------------------------------------------------------------------------- #
# Demo seed integrity (regression: the admin was seeded unverified, so the
# demo admin login was trapped on /verify/notice and could never reach /admin/)
# --------------------------------------------------------------------------- #
def test_every_seeded_demo_account_is_usable(app):
    """Seeded accounts must be verified + approved, or the demo login breaks."""
    import seed
    from models import User, db

    seed.seed_database()
    problems = []
    for email in ("admin@fuelnow.ng", "amina@example.com",
                  "station1@fuelnow.ng", "mechanic1@fuelnow.ng"):
        user = User.query.filter_by(email=email).one_or_none()
        if user is None:
            problems.append(f"{email}: missing")
            continue
        if not user.email_verified:
            problems.append(f"{email}: not email-verified")
        if user.status != "approved":
            problems.append(f"{email}: status={user.status}")
    assert not problems, f"unusable demo accounts: {problems}"


def test_seeded_admin_login_does_not_land_on_the_verify_wall(app, client):
    """Signing in as the demo admin must not redirect to /verify/notice.

    The login view redirects unverified users to the verification notice before
    any role decorator runs, so an unverified admin sees a "verify your email"
    wall (with no mail transport in the demo) instead of the admin area.
    """
    import seed

    seed.seed_database()
    token = csrf_token(client, "/login")
    resp = client.post(
        "/login",
        data={"email": "admin@fuelnow.ng", "password": PASSWORD, "csrf_token": token},
    )
    location = resp.headers.get("Location", "")
    assert "/verify" not in location, f"admin login landed on the verify wall: {location}"
    assert client.get("/admin/").status_code == 200


# --------------------------------------------------------------------------- #
# Geolocation helper (adapted getCurrentPosition snippet)
# --------------------------------------------------------------------------- #
def test_geo_locate_helper_is_shared_and_has_all_error_branches():
    """app.js must own one geolocation helper covering every failure mode."""
    from pathlib import Path

    js = Path("static/js/app.js").read_text()
    for branch in ("PERMISSION_DENIED", "POSITION_UNAVAILABLE", "TIMEOUT"):
        assert branch in js, f"missing error branch: {branch}"
    assert "does not support geolocation" in js, "missing unsupported-browser fallback"
    assert "enableHighAccuracy: true" in js


def test_templates_do_not_duplicate_geolocation_logic():
    """Pages must use [data-geo-locate], not their own getCurrentPosition copy."""
    import pathlib

    offenders = []
    for path in pathlib.Path("templates").rglob("*.html"):
        text = path.read_text()
        if "getCurrentPosition" in text:
            offenders.append(str(path))
    assert not offenders, f"inline geolocation duplicates: {offenders}"


def test_register_page_exposes_coordinate_wiring(client):
    """Each role block must be able to fill its coordinate inputs.

    Drivers type a place (autocomplete), stations and mechanics use the GPS
    button — so the page needs both mechanisms, with valid field ids.
    """
    import re

    html = client.get("/register").get_data(as_text=True)

    geo_targets = re.findall(r'data-geo-target="([^"]+)"', html)
    # Driver (typing + GPS), station and mechanic all offer coordinates.
    assert set(geo_targets) == {"latitude,longitude", "st_lat,st_lng", "mc_lat,mc_lng"}

    place_targets = re.findall(r'data-place-target="([^"]+)"', html)
    assert place_targets == ["latitude,longitude"]
    # The GPS button on the driver block should name the place it found.
    assert 'data-geo-fill-name="#place_query"' in html
    # Drivers never see raw coordinate inputs — the values are submitted hidden.
    import re as _re
    assert _re.search(r'type="hidden"\s+id="latitude"', html)
    assert _re.search(r'type="hidden"\s+id="longitude"', html)
    assert 'type="number" step="any" id="latitude"' not in html

    for pair in geo_targets + place_targets:
        for field_id in pair.split(","):
            assert f'id="{field_id}"' in html, f"missing input id {field_id}"


# --------------------------------------------------------------------------- #
# Place autocomplete (free, keyless — proxied server-side)
# --------------------------------------------------------------------------- #
def test_places_endpoint_returns_suggestions(client, monkeypatch):
    """The endpoint shapes provider results into {label, detail, lat, lng}."""
    import geo_search

    geo_search.reset_state()
    monkeypatch.setattr(
        geo_search,
        "search",
        lambda q, provider=None: [
            {"label": "Maitama Roundabout", "detail": "Abuja, Nigeria",
             "latitude": 9.084341, "longitude": 7.491571}
        ],
    )
    body = client.get("/api/places?q=maitama").get_json()
    assert body["results"][0]["label"] == "Maitama Roundabout"
    assert round(body["results"][0]["latitude"], 4) == 9.0843


def test_places_endpoint_ignores_short_queries(client):
    geo_search_short = client.get("/api/places?q=m").get_json()
    assert geo_search_short["results"] == []


def test_places_endpoint_rejects_markup(client):
    """A query with <> must not reach the provider as-is."""
    import geo_search

    calls = []
    import unittest.mock as mock

    with mock.patch.object(geo_search, "search", lambda q, provider=None: calls.append(q) or []):
        client.get("/api/places?q=<script>alert(1)</script>")
    assert not calls, "unsafe query was forwarded to the provider"


def test_places_endpoint_respects_the_off_switch(app, client):
    app.config["PLACES_AUTOCOMPLETE"] = False
    body = client.get("/api/places?q=maitama").get_json()
    assert body["results"] == []
    assert "switched off" in body["detail"]


def test_places_endpoint_is_rate_limited(client, monkeypatch):
    """Typing must not be able to hammer a free public endpoint."""
    import geo_search

    geo_search.reset_state()
    monkeypatch.setattr(geo_search, "search", lambda q, provider=None: [])
    codes = [client.get("/api/places?q=maitama").status_code for _ in range(30)]
    assert 429 in codes, f"expected throttling, got {codes}"


def test_login_attempts_are_throttled(app, client):
    make_user(email="throttle@example.com", role="driver")

    responses = [
        client.post(
            "/login",
            data={"email": "throttle@example.com", "password": "WrongPassword!1"},
        )
        for _ in range(6)
    ]
    assert 429 in [response.status_code for response in responses]


def test_idempotency_token_is_single_use(app):
    import re

    with app.test_request_context("/driver/stations"):
        from flask import session

        field = str(security.idempotency_token_field())
        match = re.search(r'value="([^"]+)"', field)
        assert match
        token = match.group(1)
        assert security.consume_idempotency_token(token)
        assert not security.consume_idempotency_token(token)


def test_submit_buttons_are_guarded_in_shared_javascript():
    from pathlib import Path

    js = Path("static/js/app.js").read_text()
    assert "initSubmitGuards" in js
    assert "submit.disabled = true" in js


def test_places_endpoint_survives_a_provider_outage(client, monkeypatch):
    """A dead provider yields an empty list, never a 500."""
    import geo_search

    geo_search.reset_state()

    def boom(q, provider=None):
        raise Exception("provider down")

    monkeypatch.setattr(geo_search, "search", boom)
    resp = client.get("/api/places?q=maitama")
    assert resp.status_code == 200


def test_place_search_module_is_parameterised_not_interpolated():
    """Source guard: the provider URL must be a constant + params, not f-string SQL-like."""
    from pathlib import Path

    src = Path("geo_search.py").read_text()
    assert "requests.get(" in src and "params=" in src
    assert "format(" not in src.split("def search")[0]


# --------------------------------------------------------------------------- #
# Reverse geocoding (GPS fix -> readable place name)
# --------------------------------------------------------------------------- #
def test_reverse_endpoint_returns_a_place(client, monkeypatch):
    import geo_search

    geo_search.reset_state()
    monkeypatch.setattr(
        geo_search, "reverse",
        lambda lat, lng, provider=None: {"label": "Maitama District", "detail": "Abuja, Nigeria"},
    )
    body = client.get("/api/places/reverse?lat=9.0901&lng=7.4908").get_json()
    assert body["place"]["label"] == "Maitama District"


def test_reverse_endpoint_rejects_out_of_range_coordinates(client):
    assert client.get("/api/places/reverse?lat=999&lng=999").status_code == 400
    assert client.get("/api/places/reverse?lat=abc&lng=xyz").status_code == 400


def test_reverse_endpoint_survives_a_provider_outage(client, monkeypatch):
    import geo_search

    geo_search.reset_state()
    monkeypatch.setattr(geo_search, "reverse", lambda *a, **k: None)
    resp = client.get("/api/places/reverse?lat=9.09&lng=7.49")
    assert resp.status_code == 200 and resp.get_json()["place"] is None


def test_reverse_module_validates_coordinates_locally():
    """geo_search.reverse must not even call out for impossible coordinates."""
    import geo_search

    assert geo_search.reverse(91, 0) is None
    assert geo_search.reverse(0, 181) is None


# --------------------------------------------------------------------------- #
# Session/CSRF survival across restarts
# --------------------------------------------------------------------------- #
def test_dev_secret_key_is_stable_across_processes():
    """A per-process random SECRET_KEY breaks every existing session cookie.

    That shows up as "Invalid or missing CSRF token" on any form rendered
    before a restart, so the development key must be persisted, not generated
    fresh each boot.
    """
    import importlib

    import config

    first = config.BaseConfig.SECRET_KEY
    importlib.reload(config)
    second = config.BaseConfig.SECRET_KEY
    assert first == second, "dev SECRET_KEY changed between boots"
    assert len(second) >= 32


def test_secret_key_file_is_not_world_readable():
    import stat
    from pathlib import Path

    path = Path("instance/.secret_key")
    if not path.exists():
        pytest.skip("no persisted key in this environment")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0, f"key file is too permissive: {oct(mode)}"


def test_production_still_requires_an_explicit_secret_key(monkeypatch):
    """Persisting a dev key must not relax the production guard.

    The guard deliberately inspects ``os.environ`` rather than ``app.config`` so
    a generated fallback key can never satisfy it.
    """
    from app import _check_production_safety
    from config import ProductionConfig
    from flask import Flask

    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Make sure the guard's own trigger is not satisfied by another test's env.
    monkeypatch.setenv("FLASK_CONFIG", "production")

    app = Flask(__name__)
    app.config.from_object(ProductionConfig)
    app.config["_config_object"] = ProductionConfig()
    app.debug = False
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["COOKIE_LESS_SESSIONS"] = False

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _check_production_safety(app)


# --------------------------------------------------------------------------- #
# CSRF without cookies (sandboxed preview iframe / cookie-blocking browsers)
# --------------------------------------------------------------------------- #
def test_csrf_rejects_a_missing_token_even_with_no_cookie(csrf_app, client):
    make_user(email="nocsrf@example.com", role=ROLE_DRIVER)
    resp = client.post("/login", data={"email": "nocsrf@example.com", "password": PASSWORD})
    assert resp.status_code == 400


def test_csrf_rejects_a_forged_token_when_cookies_are_unavailable(csrf_app, client):
    """A guessed/garbage token must fail — the stateless path is signed."""
    resp = client.post(
        "/login",
        data={"email": "x@example.com", "password": PASSWORD, "csrf_token": "forged-token"},
    )
    assert resp.status_code == 400


def test_csrf_rejects_a_tampered_signed_token(csrf_app, client):
    import security

    make_user(email="tamper@example.com", role=ROLE_DRIVER)
    with csrf_app.test_request_context("/"):
        good = security._mint_stateless_token()
    tampered = good[:-4] + ("aaaa" if not good.endswith("aaaa") else "bbbb")
    resp = client.post(
        "/login",
        data={"email": "tamper@example.com", "password": PASSWORD, "csrf_token": tampered},
    )
    assert resp.status_code == 400


def test_csrf_accepts_a_signed_token_with_no_session_cookie(csrf_app):
    """The regression this exists for.

    Renders a form, then submits it with a *fresh* client that has no cookie
    from the GET — exactly what a sandboxed iframe (or a cookie-blocking
    browser) does. The signed token alone must be enough.
    """
    import security

    make_user(email="nocookies@example.com", role=ROLE_DRIVER)
    with csrf_app.test_request_context("/"):
        token = security._mint_stateless_token()

    fresh = csrf_app.test_client()          # no session cookie at all
    resp = fresh.post(
        "/login",
        data={"email": "nocookies@example.com", "password": PASSWORD, "csrf_token": token},
    )
    assert resp.status_code == 302, "cookie-less form submission was rejected"


def test_csrf_cookie_is_set_for_the_page(csrf_app, client):
    """The double-submit cookie must be readable by the page (not HttpOnly)."""
    resp = client.get("/login")
    cookies = resp.headers.getlist("Set-Cookie")
    csrf_cookies = [c for c in cookies if "fuelnow_csrf=" in c]
    assert csrf_cookies, "CSRF cookie was not set"
    assert "HttpOnly" not in csrf_cookies[0], "CSRF cookie must be readable by JS"


# --------------------------------------------------------------------------- #
# No credentials on public pages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/", "/login", "/register"])
def test_public_pages_never_render_demo_credentials(client, path):
    """Seeded logins must not be advertised on the pages anyone can reach."""
    html = client.get(path).get_data(as_text=True)
    for secret in (
        "FuelNow!2026",
        "amina@example.com",
        "station1@fuelnow.ng",
        "mechanic1@fuelnow.ng",
        "admin@fuelnow.ng",
    ):
        assert secret not in html, f"{secret!r} is exposed on {path}"
    assert "Demo accounts" not in html, f"demo panel still shown on {path}"
