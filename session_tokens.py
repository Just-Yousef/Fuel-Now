"""Cookie-less session fallback for sandboxed iframes.

Why this exists
---------------
Normally Flask stores the login in a signed session cookie. Inside a sandboxed
preview iframe (`sandbox="allow-scripts"`, which gives the document an *opaque*
origin) or behind a proxy that strips `Set-Cookie`, the browser refuses to keep
that cookie. The result is the classic "login won't stick":

    POST /login   -> 302 to /driver/
    GET  /driver/ -> 302 back to /login      (the follow-up had no session)

This module lets the same app authenticate **without cookies**. On login we mint
an opaque random token, store it server-side, and hand it to the browser in the
URL (`?sid=...`). Every generated link and form action then carries the token
because `url_for` is wrapped. The token lasts `SESSION_TOKEN_TTL_HOURS`.

Enabled only when `COOKIE_LESS_SESSIONS` is true (preview mode). A normal
deployment keeps the cookie and never puts a token in the URL, so there is no
token-leak surface (Referer headers, shared links) in production.
"""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import current_app, redirect, request, url_for as flask_url_for
from flask_login import current_user, login_user

# token -> (user_id, expires_at)
_TOKENS: dict[str, tuple[int, datetime]] = {}
_LOCK = threading.Lock()

TOKEN_KEYS = ("sid", "session_token")


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prune() -> None:
    now = _now()
    for token in [t for t, (_, exp) in _TOKENS.items() if exp <= now]:
        _TOKENS.pop(token, None)


def create_token(user_id: int) -> str:
    """Mint a session token for a user id."""
    ttl = current_app.config.get("SESSION_TOKEN_TTL_HOURS", 12)
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _prune()
        _TOKENS[token] = (user_id, _now() + timedelta(hours=ttl))
    return token


def resolve_token(token: str | None) -> int | None:
    """Return the user id for a live token, or None."""
    if not token:
        return None
    with _LOCK:
        entry = _TOKENS.get(token)
        if entry is None:
            return None
        user_id, expires = entry
        if expires <= _now():
            _TOKENS.pop(token, None)
            return None
        return user_id


def revoke_token(token: str | None) -> None:
    if not token:
        return
    with _LOCK:
        _TOKENS.pop(token, None)


def revoke_user_tokens(user_id: int) -> None:
    with _LOCK:
        for token in [t for t, (uid, _) in _TOKENS.items() if uid == user_id]:
            _TOKENS.pop(token, None)


def active_token_count() -> int:
    with _LOCK:
        _prune()
        return len(_TOKENS)


# --------------------------------------------------------------------------- #
# Request helpers
# --------------------------------------------------------------------------- #
def token_from_request() -> str | None:
    """Read the token from the query string, form body or JSON payload."""
    for key in TOKEN_KEYS:
        value = request.args.get(key) or request.form.get(key)
        if value:
            return value
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for key in TOKEN_KEYS:
            if payload.get(key):
                return payload[key]
    return None


def login_with_token(user) -> str | None:
    """Log a user in and, in preview mode, return a URL token for redirects.

    Returns None (and does nothing extra) when cookie-less sessions are off, so
    a normal deployment keeps using the signed session cookie alone.
    """
    login_user(user)
    if not current_app.config.get("COOKIE_LESS_SESSIONS"):
        return None
    return create_token(user.id)


def current_token() -> str | None:
    if not current_app.config.get("COOKIE_LESS_SESSIONS"):
        return None
    return getattr(request, "_fno_token", None) or token_from_request()


def with_token(url: str, token: str | None = None) -> str:
    """Append the session token to an internal URL."""
    token = token or current_token()
    if not token:
        return url
    separator = "&" if "?" in url else "?"
    if f"sid={token}" in url:
        return url
    return f"{url}{separator}sid={token}"


def redirect_with_token(endpoint: str, token: str | None = None, **values):
    """redirect() to an endpoint, carrying the token when preview mode is on."""
    return redirect(with_token(flask_url_for(endpoint, **values), token))


def safe_next(raw: str | None) -> str | None:
    """Only allow same-site relative redirect targets."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return None
    if any(ord(char) < 32 for char in raw):
        return None
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        return None
    return raw


# --------------------------------------------------------------------------- #
# App wiring
# --------------------------------------------------------------------------- #
def init_app(app) -> None:
    """Install the token hook + url_for wrapper. No-op when the mode is off."""

    @app.after_request
    def _append_token_to_redirects(response):
        """Any internal redirect must carry the token, whichever view made it.

        Views call `url_for()` directly in `redirect(url_for(...))`, and those
        call sites bind the original function at import time, so patching is not
        enough. Rewriting the Location header here covers every redirect from
        every blueprint in one place.
        """
        if not app.config.get("COOKIE_LESS_SESSIONS"):
            return response
        if response.status_code not in (301, 302, 303, 305, 307, 308):
            return response
        location = response.headers.get("Location", "")
        if not location.startswith("/") or location.startswith("//"):
            return response
        token = getattr(request, "_fno_token", None) or token_from_request()
        if token:
            response.headers["Location"] = with_token(location, token)
        return response

    @app.before_request
    def _restore_session_from_token():  # runs on every request
        if not app.config.get("COOKIE_LESS_SESSIONS"):
            return None

        token = token_from_request()
        request._fno_token = token
        if token is None:
            return None

        # Already signed in via the cookie? Nothing to restore.
        if current_user.is_authenticated:
            return None

        user_id = resolve_token(token)
        if user_id is None:
            return None

        from models import User

        user = app.extensions["sqlalchemy"].session.get(User, user_id)
        if user is None or not user.is_active:
            revoke_token(token)
            return None
        login_user(user)
        return None

    if not app.config.get("COOKIE_LESS_SESSIONS"):
        return

    # Make every url_for() used *inside templates* carry the token, so links and
    # form actions keep the session. Redirects are handled by the after_request
    # hook above (whichever view built them), so no global monkey-patching of
    # flask.url_for is needed -- that would recurse through current_app.url_for.
    original_url_for = flask_url_for

    def tokenized_url_for(endpoint, **values):
        url = original_url_for(endpoint, **values)
        if endpoint == "static" or url.startswith(("http://", "https://", "#")):
            return url
        return with_token(url)

    app.jinja_env.globals["url_for"] = tokenized_url_for
