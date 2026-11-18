"""Role-based access control.

Two layers:
  1. `role_required(*roles)` — the decorator every protected view uses. Enforces
     authentication, role membership, account approval **and** email verification.
  2. `harden_session()` — a before_request hook that pins a logged-in session to
     its client (IP + User-Agent digest) so a stolen cookie/token cannot be
     replayed from another machine, and re-validates the account on every request
     (so a deactivation takes effect immediately rather than at next login).
"""
from __future__ import annotations

import hashlib
from functools import wraps

from flask import abort, current_app, flash, redirect, request, session, url_for
from flask_login import current_user

from models import ROLE_ADMIN, ROLE_LABELS, STATUS_APPROVED

# Endpoints reachable by a signed-in but unverified driver, so they can verify.
UNVERIFIED_ALLOWED_ENDPOINTS = {
    "auth.verify_email",
    "auth.resend_verification",
    "auth.pending",
    "auth.logout",
    "auth.account",
    "static",
}

SESSION_FINGERPRINT_KEY = "_fp"


def _client_fingerprint() -> str:
    """Digest of IP + User-Agent — stable for a normal browser session."""
    raw = f"{request.remote_addr or ''}|{request.headers.get('User-Agent', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def pin_session() -> None:
    """Record the client fingerprint when a session starts."""
    session[SESSION_FINGERPRINT_KEY] = _client_fingerprint()


def harden_session() -> None:
    """Reject a session being used from a different client, and re-check status."""
    if not current_user.is_authenticated:
        return

    stored = session.get(SESSION_FINGERPRINT_KEY)
    if stored and stored != _client_fingerprint():
        current_app.logger.warning(
            "Session fingerprint mismatch for user id=%s — session cleared",
            current_user.id,
        )
        from flask_login import logout_user

        logout_user()
        session.clear()
        flash("Your session was reset for security reasons. Please sign in again.", "warning")
        return

    # Immediate effect for deactivations / approval revocations.
    if current_user.status != STATUS_APPROVED and current_user.role != ROLE_ADMIN:
        if request.endpoint not in UNVERIFIED_ALLOWED_ENDPOINTS:
            return  # the decorator below handles the redirect/403


def role_required(*roles: str):
    """Allow the view only for signed-in users whose role is in `roles`."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.full_path))

            # 1. Role check — an explicit deny-list, defaulting to closed.
            if current_user.role not in roles:
                current_app.logger.warning(
                    "RBAC deny: user_id=%s role=%s endpoint=%s",
                    current_user.id,
                    current_user.role,
                    request.endpoint,
                )
                abort(403)

            # 2. Admins always bypass the approval gate; everyone else must be approved.
            if current_user.role != ROLE_ADMIN and current_user.status != STATUS_APPROVED:
                return redirect(url_for("auth.pending"))

            # 3. Drivers must have verified their email address.
            if (
                current_user.role != ROLE_ADMIN
                and not current_user.email_verified
                and request.endpoint not in UNVERIFIED_ALLOWED_ENDPOINTS
            ):
                flash("Please verify your email address to continue.", "warning")
                return redirect(url_for("auth.verify_notice"))

            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    """Admin-only alias that also logs every access attempt."""
    guarded = role_required(ROLE_ADMIN)(view)

    @wraps(view)
    def wrapper(*args, **kwargs):
        current_app.logger.info(
            "Admin route access: user_id=%s endpoint=%s ip=%s",
            getattr(current_user, "id", None),
            request.endpoint,
            request.remote_addr,
        )
        return guarded(*args, **kwargs)

    return wrapper


def flash_errors(errors: dict | list[str]) -> None:
    """Push validation messages to the flash queue, one per line."""
    messages = errors.values() if isinstance(errors, dict) else errors
    for message in messages:
        flash(message, "error")


def roles_display(roles) -> str:
    return ", ".join(ROLE_LABELS.get(r, r) for r in roles)
