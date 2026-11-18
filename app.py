"""Application factory.

Run with:  flask --app app run --debug
"""
import logging
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_login import current_user

from config import CONFIG_MAP, INSTANCE_DIR, ProductionConfig
from extensions import db, login_manager


def _static_version(static_dir: Path) -> str:
    """A cache-busting stamp derived from the newest static file."""
    latest = 0.0
    for path in static_dir.rglob("*"):
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    return str(int(latest))


def _configure_logging(app: Flask) -> None:
    """Structured-ish logging with secrets scrubbed from every record."""
    import security

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
    )
    handler.addFilter(security.ScrubFilter())

    app.logger.handlers.clear()
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO if not app.debug else logging.DEBUG)
    app.logger.propagate = False

    # Werkzeug's access log prints the raw request line, which would include
    # ?sid=... tokens. Route it through the same scrubber.
    logging.getLogger("werkzeug").addFilter(security.ScrubFilter())


def _check_production_safety(app: Flask) -> None:
    """Refuse to start production with insecure defaults."""
    if not isinstance(app.config.get("_config_object"), ProductionConfig) and not (
        os.environ.get("FLASK_CONFIG") == "production"
    ):
        return

    problems = []
    if not os.environ.get("SECRET_KEY"):
        problems.append("SECRET_KEY must be set explicitly")
    if len(os.environ.get("SECRET_KEY", "")) < 32:
        problems.append("SECRET_KEY must be at least 32 characters")
    if app.debug:
        problems.append("debug mode must be off")
    if not app.config.get("SESSION_COOKIE_SECURE"):
        problems.append("SESSION_COOKIE_SECURE must be enabled")
    if app.config.get("COOKIE_LESS_SESSIONS"):
        problems.append("COOKIE_LESS_SESSIONS must be off (tokens in URLs leak)")
    if not os.environ.get("DATABASE_URL"):
        problems.append("DATABASE_URL should point at a real database")
    if not app.config.get("MAIL_SERVER"):
        problems.append("MAIL_SERVER must be set so account emails can be delivered")

    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure settings:\n  - "
            + "\n  - ".join(problems)
        )


def create_app(config_name: str | None = None) -> Flask:
    import security

    config_name = config_name or os.environ.get("FLASK_CONFIG", "development")
    if config_name not in CONFIG_MAP:
        raise RuntimeError(f"Unknown FLASK_CONFIG '{config_name}'")

    app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=False)
    app.config.from_object(CONFIG_MAP[config_name])
    app.config["_config_object"] = CONFIG_MAP[config_name]
    app.config["ENV_NAME"] = config_name

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # --- extensions ---
    db.init_app(app)
    login_manager.init_app(app)

    # CSRF, security headers, CORS, input validators, log scrubbing.
    security.init_app(app)

    # Role enforcement + session pinning.
    import auth_utils

    app.before_request(auth_utils.harden_session)

    # Cookie-less session fallback for sandboxed preview iframes (no-op otherwise).
    import session_tokens

    session_tokens.init_app(app)

    _configure_logging(app)

    # --- blueprints ---
    from blueprints.auth import auth_bp
    from blueprints.driver import driver_bp
    from blueprints.station import station_bp
    from blueprints.mechanic import mechanic_bp
    from blueprints.admin import admin_bp
    from blueprints.public import public_bp
    from blueprints.webhooks import webhooks_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(driver_bp, url_prefix="/driver")
    app.register_blueprint(station_bp, url_prefix="/station")
    app.register_blueprint(mechanic_bp, url_prefix="/mechanic")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(webhooks_bp)

    # --- template globals / filters ---
    from models import (
        BAD_STATUSES,
        GOOD_STATUSES,
        ORDER_STATUSES,
        REQUEST_STATUSES,
        ROLE_LABELS,
        STATUS_LABELS,
    )

    static_version = _static_version(Path(app.static_folder))

    @app.after_request
    def _no_cached_html(response):
        """Never let HTML or static assets go stale in a preview iframe."""
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers.pop("Expires", None)
        return response

    @app.context_processor
    def inject_globals():
        import session_tokens

        return {
            "static_v": static_version,
            "session_token": session_tokens.current_token(),
            "current_user": current_user,
            "bad_statuses": BAD_STATUSES,
            "good_statuses": GOOD_STATUSES,
            "order_statuses": ORDER_STATUSES,
            "request_statuses": REQUEST_STATUSES,
            "role_labels": ROLE_LABELS,
            "status_labels": STATUS_LABELS,
            "currency": app.config["DEFAULT_CURRENCY"],
            "app_name": "FuelNow",
        }

    @app.template_filter("money")
    def money(value):
        if value is None:
            return "—"
        return f"{app.config['DEFAULT_CURRENCY']} {value:,.2f}"

    @app.template_filter("nice_status")
    def nice_status(value):
        return STATUS_LABELS.get(value, str(value).replace("_", " ").title())

    @app.template_filter("short_dt")
    def short_dt(value):
        return value.strftime("%d %b %Y, %H:%M") if value else "—"

    # --- error handlers (never leak stack traces to the client) ---
    @app.errorhandler(400)
    def bad_request(error):
        description = getattr(error, "description", "The request could not be processed.")
        if request.path.startswith("/webhooks/"):
            return jsonify(ok=False, error=description), 400
        return render_template("errors/400.html", message=description), 400

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(_error):
        return render_template(
            "errors/413.html",
            limit_mb=app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        ), 413

    @app.errorhandler(429)
    def rate_limited(_error):
        return render_template("errors/429.html"), 429

    @app.errorhandler(500)
    def server_error(_error):  # pragma: no cover - defensive
        db.session.rollback()
        # Log the detail server-side; show the user nothing specific.
        app.logger.exception("Unhandled error on %s", request.path)
        return render_template("errors/500.html"), 500

    # --- CLI commands (setup / seed / security helpers) ---
    from blueprints.public import register_cli

    register_cli(app)

    @login_manager.user_loader
    def load_user(user_id):
        from models import User

        return db.session.get(User, int(user_id))

    _check_production_safety(app)

    app.logger.info(
        "FuelNow started — env=%s debug=%s cookie_less=%s",
        config_name,
        app.debug,
        app.config.get("COOKIE_LESS_SESSIONS"),
    )
    return app


app = create_app()
