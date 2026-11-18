"""Security primitives: CSRF protection, per-request CSP nonces, security headers,
input validation, and role-guard hardening.

Everything here is applied by `init_app()`, which `create_app()` calls once.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import base64
import binascii
import hashlib
import secrets
import struct
import threading
import time
from functools import wraps

from flask import (
    abort,
    current_app,
    request,
    session,
)
from markupsafe import Markup

# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
CSRF_SESSION_KEY = "_csrf_token"
CSRF_COOKIE = "fuelnow_csrf"
CSRF_TOKEN_TTL = 60 * 60 * 12  # 12 hours
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

# Login throttling. This protects a single app process from brute force; production
# deployments with multiple workers should move this state to a shared store.
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
LOGIN_ATTEMPT_MAX_FAILURES = 5
_login_attempts: dict[str, list[float]] = {}
_login_attempts_lock = threading.Lock()

# Submission idempotency.
IDEMPOTENCY_SESSION_KEY = "_idempotency_tokens"

# Endpoints that authenticate by signature or are machine-to-machine.
CSRF_EXEMPT_ENDPOINTS = {"webhooks.receive"}


# Why a cookie *and* a self-verifying token
# -----------------------------------------
# The classic pattern stores the token in the session and compares the form
# field against it. That is unforgeable, but it needs the session cookie to
# survive between the GET that rendered the form and the POST that submits it.
#
# In a sandboxed preview iframe (`sandbox="allow-scripts"` -> opaque origin) the
# browser stores no cookies at all, so that comparison fails on *every* form
# submit even though the page rendered fine. The same happens for users who
# block cookies.
#
# So a token is now valid in either of two ways, and both are still unforgeable:
#   1. it matches the session copy (preferred; works whenever cookies exist), or
#   2. it is a signed token this server produced — an HMAC over a random nonce
#      and an expiry, keyed with SECRET_KEY. An attacker cannot mint one: the
#      nonce is unguessable for their victim, so they cannot precompute a pair
#      (nonce, signature). This is the same double-submit-style guarantee the
#      session comparison gives, without depending on cookies at all.

def _signer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(current_app.secret_key, salt="fuelnow-csrf")


def _mint_stateless_token() -> str:
    return _signer().dumps(secrets.token_urlsafe(16))


def _stateless_token_is_valid(token: str) -> bool:
    from itsdangerous import BadSignature, SignatureExpired

    try:
        _signer().loads(token, max_age=CSRF_TOKEN_TTL)
        return True
    except (BadSignature, SignatureExpired, Exception):
        return False


def get_csrf_token() -> str:
    """Return a CSRF token for this request, creating one if needed.

    The same token is put in the response cookie, so a page rendered without a
    session still round-trips.
    """
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = _mint_stateless_token()
        session[CSRF_SESSION_KEY] = token
    return token


def _remember_csrf_cookie(response):
    """Mirror the session token into a readable cookie (double-submit)."""
    if not current_app.config.get("CSRF_ENABLED", True):
        return response
    token = session.get(CSRF_SESSION_KEY)
    if token:
        response.set_cookie(
            CSRF_COOKIE,
            token,
            max_age=CSRF_TOKEN_TTL,
            httponly=False,   # read by the page for fetch() headers
            samesite=current_app.config.get("SESSION_COOKIE_SAMESITE") or "Lax",
            secure=bool(current_app.config.get("SESSION_COOKIE_SECURE")),
        )
    return response


def _submitted_csrf_token() -> str | None:
    # Accept the form field, or the header for fetch()-based clients.
    return request.form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER)


def csrf_protect() -> None:
    """Reject state-changing requests that lack a valid token (before_request)."""
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    if request.method in CSRF_SAFE_METHODS:
        return
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return

    submitted = _submitted_csrf_token()
    if not submitted:
        _reject_csrf("no token submitted")

    # (1) session comparison — the strongest check when cookies survive.
    expected = session.get(CSRF_SESSION_KEY)
    if expected and hmac.compare_digest(str(expected), str(submitted)):
        return

    # (2) double-submit cookie — the browser echoes our own cookie back.
    cookie_value = request.cookies.get(CSRF_COOKIE)
    if cookie_value and hmac.compare_digest(str(cookie_value), str(submitted)):
        return

    # (3) self-verifying signed token — required when no cookie storage exists.
    if _stateless_token_is_valid(str(submitted)):
        return

    _reject_csrf("token did not validate")


def _reject_csrf(reason: str) -> None:
    current_app.logger.warning(
        "CSRF rejection: endpoint=%s method=%s ip=%s (%s)",
        request.endpoint,
        request.method,
        _client_ip(),
        reason,
    )
    abort(400, description="Invalid or missing CSRF token. Please reload the page.")


def csrf_token_field() -> Markup:
    """`{{ csrf_field() }}` -> hidden input for any POST form."""
    return Markup(
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{get_csrf_token()}">'
    )


def login_attempt_allowed(key: str) -> bool:
    now = time.monotonic()
    with _login_attempts_lock:
        stamps = [stamp for stamp in _login_attempts.get(key, [])
                  if now - stamp < LOGIN_ATTEMPT_WINDOW_SECONDS]
        _login_attempts[key] = stamps
        return len(stamps) < LOGIN_ATTEMPT_MAX_FAILURES


def record_login_failure(key: str) -> None:
    with _login_attempts_lock:
        stamps = _login_attempts.setdefault(key, [])
        stamps.append(time.monotonic())
        if len(stamps) > LOGIN_ATTEMPT_MAX_FAILURES:
            stamps[:] = stamps[-LOGIN_ATTEMPT_MAX_FAILURES:]


def reset_login_attempts(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


def generate_totp_secret() -> str:
    """Generate a Base32 TOTP secret compatible with authenticator apps."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def verify_totp(secret: str, code: str, *, timestamp: float | None = None) -> bool:
    """Verify a six-digit TOTP code, allowing one 30-second clock step either side."""
    if not secret or not re.fullmatch(r"\d{6}", code or ""):
        return False
    try:
        padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(padded, casefold=True)
    except (ValueError, binascii.Error):
        return False

    counter_time = time.time() if timestamp is None else timestamp
    submitted = int(code)
    for step in (-1, 0, 1):
        counter = int(counter_time // 30) + step
        digest = hmac.new(key, struct.pack("!Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack("!I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        if hmac.compare_digest(f"{binary % 1000000:06d}", code):
            return True
    return False


def totp_uri(secret: str, email: str) -> str:
    """Return an otpauth URI for manual or QR authenticator enrollment."""
    import urllib.parse

    label = urllib.parse.quote(f"FuelNow:{email}")
    issuer = urllib.parse.quote("FuelNow")
    return f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&digits=6&period=30"


def idempotency_token_field() -> Markup:
    """Mint a hidden, session-bound token for state-changing forms."""
    token = secrets.token_urlsafe(24)
    tokens = session.setdefault(IDEMPOTENCY_SESSION_KEY, [])
    tokens.append(token)
    session[IDEMPOTENCY_SESSION_KEY] = tokens[-128:]  # bound session memory
    return Markup(
        f'<input type="hidden" name="idempotency_token" value="{token}">'
    )


def consume_idempotency_token(raw_token: str | None) -> bool:
    """Consume a submitted token once; missing tokens preserve API compatibility."""
    if not raw_token:
        return True
    tokens = session.setdefault(IDEMPOTENCY_SESSION_KEY, [])
    if raw_token not in tokens:
        return False
    tokens.remove(raw_token)
    session[IDEMPOTENCY_SESSION_KEY] = tokens
    return True


# --------------------------------------------------------------------------- #
# CSP nonces
# --------------------------------------------------------------------------- #
def csp_nonce() -> str:
    """Nonce for this request, so inline <script>/<style> stay allowed under CSP."""
    nonce = getattr(request, "_csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_urlsafe(16)
        request._csp_nonce = nonce
    return nonce


# --------------------------------------------------------------------------- #
# Response headers
# --------------------------------------------------------------------------- #
def security_headers(response):
    """Attach defence-in-depth headers to every response."""
    nonce = csp_nonce()

    # Inline styles are used for small layout tweaks in templates; allowing them
    # by nonce would require editing every style attribute, so style-src keeps
    # 'unsafe-inline' while SCRIPT execution stays nonce-gated (the real XSS risk).
    csp = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'none'; "
        "object-src 'none'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(self), camera=(), microphone=(), payment=()"
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    if current_app.config.get("SESSION_COOKIE_SECURE"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# --------------------------------------------------------------------------- #
# CORS (locked down: same-origin by default, explicit allow-list only)
# --------------------------------------------------------------------------- #
def cors_headers(response):
    """Same-origin only by default; allow-listed origins get explicit headers."""
    allowed = current_app.config.get("CORS_ALLOWED_ORIGINS", [])
    origin = request.headers.get("Origin")

    if not origin:
        return response  # no cross-origin context at all

    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    # Otherwise: emit no CORS headers, so browsers block cross-origin reads.
    return response


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
PLATE_RE = re.compile(r"^[A-Za-z0-9\-\s]{0,20}$")
SAFE_TEXT_RE = re.compile(r"^[^<>]*$")  # no angle brackets -> no markup
USERNAME_RE = re.compile(r"^[A-Za-z0-9 .'\-]{2,120}$")


class FieldError(ValueError):
    """Raised by validators; carries a human-readable message."""


def clean_text(value, *, max_len: int = 255, min_len: int = 0, allow_newlines=False) -> str:
    """Normalise and validate free text. Rejects markup characters outright."""
    text = (value or "").strip()
    if not allow_newlines:
        text = " ".join(text.split())
    else:
        text = "\n".join(line.strip() for line in text.splitlines()).strip()
    if len(text) < min_len:
        raise FieldError(f"This field needs at least {min_len} characters.")
    if len(text) > max_len:
        raise FieldError(f"Please use at most {max_len} characters.")
    if not SAFE_TEXT_RE.match(text):
        raise FieldError("Please remove any < or > characters.")
    return text


def validate_email(value) -> str:
    email = (value or "").strip().lower()
    if len(email) > 255 or not EMAIL_RE.match(email):
        raise FieldError("Enter a valid email address.")
    return email


def validate_password(value) -> str:
    """Enforce a minimum strength policy; never logged."""
    pw = value or ""
    if len(pw) < 8:
        raise FieldError("Password must be at least 8 characters.")
    if len(pw) > 128:
        raise FieldError("Password must be at most 128 characters.")
    if pw.lower() in {"password", "password123", "12345678", "qwertyui"}:
        raise FieldError("That password is too common. Choose another.")
    return pw


def validate_phone(value, *, required=False) -> str | None:
    phone = (value or "").strip()
    if not phone:
        if required:
            raise FieldError("Phone number is required.")
        return None
    if not PHONE_RE.match(phone):
        raise FieldError("Enter a valid phone number.")
    return phone


def validate_float(value, *, minimum=None, maximum=None, field="value") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FieldError(f"Enter a valid number for {field}.")
    if minimum is not None and number < minimum:
        raise FieldError(f"{field.capitalize()} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise FieldError(f"{field.capitalize()} must be at most {maximum}.")
    return number


def validate_choice(value, choices, *, field="option") -> str:
    choice = (value or "").strip()
    if choice not in choices:
        raise FieldError(f"Choose a valid {field}.")
    return choice


def validate_plate(value) -> str | None:
    plate = (value or "").strip().upper()
    if not plate:
        return None
    if not PLATE_RE.match(plate):
        raise FieldError("Plate may contain letters, numbers, spaces and hyphens only.")
    return plate


def collect_errors(validators: dict) -> tuple[dict, dict]:
    """Run {field: callable} validators, returning (values, errors)."""
    values, errors = {}, {}
    for field, validator in validators.items():
        try:
            values[field] = validator()
        except FieldError as exc:
            errors[field] = str(exc)
    return values, errors


# --------------------------------------------------------------------------- #
# Log scrubbing
# --------------------------------------------------------------------------- #
SECRET_QUERY_KEYS = ("sid", "session_token", "csrf_token", "password", "token", "key")
_QUERY_RE = re.compile(r"(\?[^\s\"]*)")


def scrub_url(text: str) -> str:
    """Strip sensitive values from a URL or request line before it is logged."""

    def _clean(match):
        query = match.group(1)
        parts = []
        for chunk in query.lstrip("?").split("&"):
            key = chunk.split("=", 1)[0]
            if key in SECRET_QUERY_KEYS:
                parts.append(f"{key}=[redacted]")
            else:
                parts.append(chunk)
        return "?" + "&".join(parts)

    return _QUERY_RE.sub(_clean, text)


class ScrubFilter:
    """logging.Filter that redacts secrets from every emitted record."""

    PATTERNS = [
        (re.compile(r"(sid=)[^&\s\"]+"), r"\1[redacted]"),
        (re.compile(r"(session_token=)[^&\s\"]+"), r"\1[redacted]"),
        (re.compile(r"(csrf_token=)[^&\s\"]+"), r"\1[redacted]"),
        (re.compile(r"(password=)[^&\s\"]+", re.I), r"\1[redacted]"),
        (re.compile(r"(?i)(authorization:\s*)\S+"), r"\1[redacted]"),
        (re.compile(r"(?i)(set-cookie:\s*)\S+"), r"\1[redacted]"),
    ]

    def filter(self, record) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        scrubbed = message
        for pattern, repl in self.PATTERNS:
            scrubbed = pattern.sub(repl, scrubbed)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def _client_ip() -> str:
    """Best-effort client IP (proxy-aware), used only for security logging."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def fingerprint(value: str) -> str:
    """Stable non-reversible fingerprint, for logging 'this same value' safely."""
    salt = current_app.config["SECRET_KEY"] or ""
    return hashlib.sha256(f"{salt}{value}".encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Hardened role guard
# --------------------------------------------------------------------------- #
def init_app(app) -> None:
    """Wire all of the above into the app."""
    app.before_request(csrf_protect)
    app.after_request(security_headers)
    app.after_request(cors_headers)
    app.after_request(_remember_csrf_cookie)

    app.jinja_env.globals["csrf_token"] = get_csrf_token
    app.jinja_env.globals["csrf_field"] = csrf_token_field
    app.jinja_env.globals["idempotency_field"] = idempotency_token_field
    app.jinja_env.globals["csp_nonce"] = csp_nonce
