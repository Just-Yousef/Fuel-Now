"""Public pages: landing, health check, place search, and the setup CLI commands."""
import random
import string

import click
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

import geo_search
from extensions import db
from security import clean_text

public_bp = Blueprint("public", __name__)


@public_bp.get("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for(current_user.home_endpoint))
    return render_template("index.html")


@public_bp.get("/healthz")
def healthz():
    return jsonify(status="ok")


def _client_key() -> str:
    """Rate-limit key: the client address (the Secure session cookie is not
    sent over plain HTTP, so keying on the session would never bucket)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "unknown"


@public_bp.get("/api/places")
def places():
    """Type-ahead place search for the address box.

    GET-only and read-only, so it carries no CSRF token; it is rate limited
    per session and disabled entirely with PLACES_AUTOCOMPLETE=0.
    """
    if not current_app.config.get("PLACES_AUTOCOMPLETE", True):
        return jsonify(results=[], detail="Autocomplete is switched off."), 200

    raw = request.args.get("q", "")
    try:
        query = clean_text(raw, max_len=geo_search.MAX_QUERY_LEN)
    except Exception:
        return jsonify(results=[], detail="Search text had unsupported characters."), 200

    if len(query) < geo_search.MIN_QUERY_LEN:
        return jsonify(results=[], detail="Keep typing…"), 200

    if not geo_search.allowed(_client_key()):
        current_app.logger.info("Places search throttled")
        return jsonify(results=[], detail="Too many lookups — pause for a second."), 429

    try:
        results = geo_search.search(
            query,
            provider=current_app.config.get("PLACES_PROVIDER", geo_search.DEFAULT_PROVIDER),
        )
    except Exception:  # provider outage, missing network, bad payload
        current_app.logger.warning("Places search failed; returning no suggestions")
        results = []
    if not results:
        return jsonify(
            results=[],
            detail="No matches. Try a different spelling, or use “Use my current location”.",
        ), 200
    return jsonify(results=results)


@public_bp.get("/api/places/reverse")
def places_reverse():
    """Turn a GPS fix into a readable place name for the address box."""
    if not current_app.config.get("PLACES_AUTOCOMPLETE", True):
        return jsonify(place=None), 200
    if not geo_search.allowed(_client_key()):
        return jsonify(place=None, detail="Too many lookups — pause for a second."), 429

    try:
        latitude = float(request.args.get("lat", ""))
        longitude = float(request.args.get("lng", ""))
    except (TypeError, ValueError):
        return jsonify(place=None, detail="Invalid coordinates."), 400
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return jsonify(place=None, detail="Coordinates out of range."), 400

    try:
        place = geo_search.reverse(
            latitude,
            longitude,
            provider=current_app.config.get(
                "PLACES_REVERSE_PROVIDER", geo_search.DEFAULT_REVERSE_PROVIDER
            ),
        )
    except Exception:
        current_app.logger.warning("Reverse lookup failed; returning no name")
        place = None
    return jsonify(place=place), 200


def _reference(prefix: str) -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{suffix}"


def register_cli(app):
    """`flask init-db` and `flask seed-db` commands."""

    @app.cli.command("init-db")
    def init_db():
        """Create all tables (idempotent)."""
        db.create_all()
        click.echo("Database tables created.")

    @app.cli.command("seed-db")
    @click.option("--reset", is_flag=True, help="Drop all tables first.")
    def seed_db(reset):
        """Create tables and load demo users, stations, mechanics and orders."""
        from seed import seed_database

        if reset:
            db.drop_all()
            click.echo("Existing tables dropped.")
        db.create_all()
        summary = seed_database()
        for line in summary:
            click.echo(line)

    @app.cli.command("list-users")
    def list_users():
        """Print every account (handy after seeding)."""
        from models import User

        for user in User.query.order_by(User.role, User.email).all():
            click.echo(
                f"{user.role:9s} {user.status:12s} {user.email:28s} {user.full_name}"
            )


__all__ = ["public_bp", "register_cli"]
