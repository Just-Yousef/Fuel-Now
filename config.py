"""Application configuration.

Framework choice: Flask (see README.md -> "Why Flask?").
Security-relevant settings are grouped at the bottom of `BaseConfig` and
tightened further in `ProductionConfig`.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
# Must exist before anything tries to persist state into it (the secret key and
# the uploads folder both live here). A missing directory would silently defeat
# the persisted-key logic below and regenerate a key on every boot.
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)


def _bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _local_secret_key() -> str:
    """A stable development key, kept in instance/.secret_key (git-ignored).

    Production ignores this entirely: ``_check_production_safety`` refuses to
    boot unless SECRET_KEY is supplied via the environment.
    """
    path = INSTANCE_DIR / ".secret_key"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            # Enforce the mode on every load, not just on creation: a file
            # restored from a snapshot or copied in can arrive world-readable.
            _restrict(path)
            return existing
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(48)
    try:
        path.write_text(value, encoding="utf-8")
        _restrict(path)
    except OSError:
        pass  # read-only filesystem: fall back to an in-memory key
    return value


def _restrict(path) -> None:
    """Best-effort chmod 0600."""
    try:
        path.chmod(0o600)
    except OSError:
        pass


class BaseConfig:
    """Settings shared by every environment."""

    # Never fall back to a hardcoded secret in production (see ProductionConfig).
    # Set SECRET_KEY in the environment for any real deployment. When it is
    # absent we persist a generated key locally instead of regenerating per
    # process: a fresh key invalidates the signature of every session cookie,
    # which silently loses logins and CSRF tokens on each restart (and looks
    # exactly like "Invalid or missing CSRF token" in the browser).
    SECRET_KEY = os.environ.get("SECRET_KEY") or _local_secret_key()
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'fuelnow.sqlite3'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- Domain defaults (documented in README "Assumptions") ---
    DEFAULT_CURRENCY = "NGN"
    DEFAULT_SEARCH_RADIUS_KM = 25.0
    MAX_SEARCH_RADIUS_KM = 200.0
    DEFAULT_SERVICE_RADIUS_KM = 15.0

    # CSRF protection. Enabled everywhere by default; the bulk of the test
    # suite disables it so flow tests stay readable, and tests/test_security.py
    # re-enables it to prove enforcement.
    CSRF_ENABLED = _bool("CSRF_ENABLED", "1")

    # --- Sessions / cookies ---
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_NAME = "fuelnow_session"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"

    # Cookie policy. Defaults to "Lax", correct for a normal top-level
    # deployment. Relaxed to "None" (+ Secure) only when embedded in a
    # cross-site iframe (hosted preview), where Lax cookies are not sent.
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE")

    # Cookie-less session fallback for sandboxed iframes (preview only).
    COOKIE_LESS_SESSIONS = _bool(
        "COOKIE_LESS_SESSIONS",
        "1" if SESSION_COOKIE_SAMESITE.lower() == "none" else "0",
    )
    SESSION_TOKEN_TTL_HOURS = int(os.environ.get("SESSION_TOKEN_TTL_HOURS", "12"))

    # --- Uploads ---
    UPLOAD_FOLDER = str(INSTANCE_DIR / "uploads")
    MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 2 * 1024 * 1024))
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 4 * 1024 * 1024))

    # --- Email / verification ---
    EMAIL_VERIFICATION_REQUIRED = _bool("EMAIL_VERIFICATION_REQUIRED", "1")
    EMAIL_VERIFICATION_MAX_AGE = int(os.environ.get("EMAIL_VERIFICATION_MAX_AGE", 60 * 60 * 24))
    PASSWORD_RESET_MAX_AGE = int(os.environ.get("PASSWORD_RESET_MAX_AGE", 60 * 60))
    # Without an SMTP server the app surfaces links in the UI (demo mode).
    MAIL_SERVER = os.environ.get("MAIL_SERVER")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_USE_TLS = _bool("MAIL_USE_TLS", "1")
    MAIL_FROM = os.environ.get("MAIL_FROM", "no-reply@fuelnow.local")
    SHOW_VERIFICATION_LINK = _bool("SHOW_VERIFICATION_LINK", "1")

    # --- CORS: same-origin by default; an explicit allow-list only ---
    CORS_ALLOWED_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]

    # --- Place search (free, keyless) ---
    # Photon is the right tool for type-ahead; Nominatim's public server
    # forbids autocomplete. Requests are proxied server-side (see geo_search.py).
    PLACES_AUTOCOMPLETE = _bool("PLACES_AUTOCOMPLETE", "1")
    PLACES_PROVIDER = os.environ.get(
        "PLACES_PROVIDER", "https://photon.komoot.io/api/"
    )
    PLACES_REVERSE_PROVIDER = os.environ.get(
        "PLACES_REVERSE_PROVIDER", "https://photon.komoot.io/reverse"
    )

    # --- Webhooks ---
    WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
    WEBHOOK_TOLERANCE_SECONDS = int(os.environ.get("WEBHOOK_TOLERANCE_SECONDS", 300))

    # Show richer error detail (never in production).
    PROPAGATE_EXCEPTIONS = False


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    # Dev-only convenience: if no SECRET_KEY is set, a random one is generated
    # per process (sessions do not survive a restart, which is fine locally).
    TEMPLATES_AUTO_RELOAD = True


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    CSRF_ENABLED = False  # dedicated CSRF tests re-enable this explicitly
    EMAIL_VERIFICATION_REQUIRED = False
    SHOW_VERIFICATION_LINK = True
    SECRET_KEY = "test-secret-key-not-for-production"
    WEBHOOK_SECRET = "test-webhook-secret"


class ProductionConfig(BaseConfig):
    DEBUG = False
    TESTING = False
    PROPAGATE_EXCEPTIONS = False
    TEMPLATES_AUTO_RELOAD = False
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "None")
    EMAIL_VERIFICATION_REQUIRED = True
    SHOW_VERIFICATION_LINK = False


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
