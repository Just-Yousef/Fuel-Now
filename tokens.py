"""Signed, expiring tokens for email verification and password resets.

Uses `itsdangerous` (a Flask dependency) so tokens are HMAC-signed with the app's
SECRET_KEY and carry an expiry. They are **single purpose** (a `scope` claim) and
bound to the user id + a fingerprint of the current password hash, so resetting a
password invalidates every outstanding reset link.
"""
from __future__ import annotations

import hashlib

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from flask import current_app, url_for

VERIFY_SCOPE = "verify-email"
RESET_SCOPE = "reset-password"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="fuelnow-tokens")


def _pw_fingerprint(user) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def make_token(user, scope: str) -> str:
    return _serializer().dumps(
        {"uid": user.id, "scope": scope, "pw": _pw_fingerprint(user)}
    )


def read_token(token: str, scope: str, *, max_age: int | None = None):
    """Return the user for a valid token, or None. Never raises."""
    from extensions import db
    from models import User

    if max_age is None:
        max_age = (
            current_app.config["EMAIL_VERIFICATION_MAX_AGE"]
            if scope == VERIFY_SCOPE
            else current_app.config["PASSWORD_RESET_MAX_AGE"]
        )
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (SignatureExpired, BadSignature):
        return None
    if not isinstance(data, dict) or data.get("scope") != scope:
        return None
    user = db.session.get(User, data.get("uid"))
    if user is None:
        return None
    if data.get("pw") != _pw_fingerprint(user):
        return None  # password changed since the link was issued
    return user


def verification_url(user) -> str:
    return url_for(
        "auth.verify_email", token=make_token(user, VERIFY_SCOPE), _external=True
    )


def reset_url(user) -> str:
    return url_for(
        "auth.reset_password", token=make_token(user, RESET_SCOPE), _external=True
    )
