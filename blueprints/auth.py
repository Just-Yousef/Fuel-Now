"""Sign up, sign in, sign out, email verification, password reset, profile."""
import secrets
import smtplib
from email.message import EmailMessage

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    session,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

import auth_utils
import security
import session_tokens
import tokens
from extensions import db
from geo import parse_coords
from models import (
    ROLE_ADMIN,
    ROLE_DRIVER,
    ROLE_LABELS,
    ROLE_MECHANIC,
    ROLE_STATION,
    STATUS_APPROVED,
    STATUS_DEACTIVATED,
    STATUS_PENDING,
    Mechanic,
    Station,
    User,
    utcnow,
)

auth_bp = Blueprint("auth", __name__)

# Self-service registration is open to these roles only.
SELF_SIGNUP_ROLES = (ROLE_DRIVER, ROLE_STATION, ROLE_MECHANIC)


def _str(form, key: str) -> str:
    return (form.get(key) or "").strip()


def _send_verification(user) -> str:
    """Send a verification link when SMTP is configured; otherwise return it for demo use."""
    link = tokens.verification_url(user)
    if not current_app.config.get("MAIL_SERVER"):
        return link
    return link if _send_email(
        user,
        "Verify your FuelNow account",
        f"Open this link to verify your email address:\n\n{link}",
    ) else ""


def _send_email(user, subject: str, body: str) -> bool:
    """Deliver one transactional email through the configured SMTP server."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = current_app.config["MAIL_FROM"]
    message["To"] = user.email
    message.set_content(body)

    try:
        with smtplib.SMTP(
            current_app.config["MAIL_SERVER"],
            current_app.config["MAIL_PORT"],
            timeout=10,
        ) as smtp:
            if current_app.config["MAIL_USE_TLS"]:
                smtp.starttls()
            username = current_app.config.get("MAIL_USERNAME")
            if username:
                smtp.login(username, current_app.config.get("MAIL_PASSWORD") or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        current_app.logger.error(
            "Email delivery failed for user_id=%s: %s", user.id, type(exc).__name__
        )
        return False
    return True


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for(current_user.home_endpoint))

    form = request.form if request.method == "POST" else {}
    errors: list[str] = []

    if request.method == "POST":
        role = _str(form, "role") or ROLE_DRIVER
        coords = parse_coords(_str(form, "latitude"), _str(form, "longitude"))

        # --- validated inputs (never trust the client) ---
        values = {}
        for field, validator in {
            "email": lambda: security.validate_email(form.get("email")),
            "password": lambda: security.validate_password(form.get("password")),
            "full_name": lambda: security.clean_text(
                form.get("full_name"), min_len=2, max_len=120
            ),
            "phone": lambda: security.validate_phone(form.get("phone")),
        }.items():
            try:
                values[field] = validator()
            except security.FieldError as exc:
                errors.append(str(exc))

        if role not in SELF_SIGNUP_ROLES:
            errors.append("Please choose a valid account type.")
        if form.get("password") != form.get("confirm_password"):
            errors.append("Passwords do not match.")
        if values.get("email") and User.query.filter_by(email=values["email"]).first():
            errors.append("An account with that email already exists.")
        if role in (ROLE_STATION, ROLE_MECHANIC) and coords is None:
            errors.append(
                "Please enter a valid latitude and longitude for your business location."
            )

        # Role-specific business fields.
        business = {}
        radius = 15.0
        try:
            if role == ROLE_STATION:
                business["name"] = security.clean_text(
                    form.get("station_name"), min_len=2, max_len=120
                )
                business["city"] = security.clean_text(
                    form.get("city"), min_len=2, max_len=80
                )
                business["address"] = security.clean_text(
                    form.get("address") or "Not provided", max_len=255
                )
            elif role == ROLE_MECHANIC:
                business["business_name"] = security.clean_text(
                    form.get("business_name"), min_len=2, max_len=120
                )
                business["city"] = security.clean_text(
                    form.get("city"), min_len=2, max_len=80
                )
                business["specialties"] = security.clean_text(
                    form.get("specialties") or "General repairs", max_len=255
                )
                radius = security.validate_float(
                    form.get("service_radius_km") or 15,
                    minimum=1,
                    maximum=200,
                    field="service radius",
                )
        except security.FieldError as exc:
            errors.append(str(exc))

        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "auth/register.html",
                form=form,
                self_signup_roles=SELF_SIGNUP_ROLES,
                role_labels=ROLE_LABELS,
            ), 400

        # --- create the account ---
        user = User(
            email=values["email"],
            full_name=values["full_name"],
            phone=values["phone"],
            role=role,
            # Drivers self-activate; businesses need admin approval.
            status=STATUS_APPROVED if role == ROLE_DRIVER else STATUS_PENDING,
            # Email verification is separate from business approval.
            email_verified=not current_app.config["EMAIL_VERIFICATION_REQUIRED"],
        )
        user.set_password(values["password"])
        if coords:
            user.latitude, user.longitude = coords.latitude, coords.longitude
            user.location_updated_at = utcnow()
        db.session.add(user)
        db.session.flush()

        if role == ROLE_STATION:
            db.session.add(
                Station(
                    owner_id=user.id,
                    name=business["name"],
                    address=business["address"],
                    city=business["city"],
                    phone=values["phone"],
                    latitude=coords.latitude,
                    longitude=coords.longitude,
                    opening_hours=_str(form, "opening_hours") or "06:00 - 22:00",
                )
            )
        elif role == ROLE_MECHANIC:
            db.session.add(
                Mechanic(
                    user_id=user.id,
                    business_name=business["business_name"],
                    specialties=business["specialties"],
                    address=_str(form, "address") or None,
                    city=business["city"],
                    latitude=coords.latitude,
                    longitude=coords.longitude,
                    service_radius_km=radius,
                )
            )

        db.session.commit()

        link = _send_verification(user) if not user.email_verified else None
        if link and current_app.config["SHOW_VERIFICATION_LINK"] and not current_app.config.get("MAIL_SERVER"):
            flash(
                "Demo mode: use this link to verify your address — " + link, "info"
            )
        elif not link and current_app.config.get("MAIL_SERVER"):
            flash(
                "We couldn't send the verification email. Please try sending a new link.",
                "error",
            )

        token = session_tokens.login_with_token(user)
        auth_utils.pin_session()
        flash(f"Welcome to FuelNow, {user.full_name}!", "success")

        if not user.email_verified:
            return session_tokens.redirect_with_token("auth.verify_notice", token)
        if user.status != STATUS_APPROVED:
            return session_tokens.redirect_with_token("auth.pending", token)
        return session_tokens.redirect_with_token(user.home_endpoint, token)

    return render_template(
        "auth/register.html",
        form={},
        self_signup_roles=SELF_SIGNUP_ROLES,
        role_labels=ROLE_LABELS,
    )


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(current_user.home_endpoint))

    if request.method == "POST":
        try:
            email = security.validate_email(request.form.get("email"))
        except security.FieldError:
            throttle_key = f"login-invalid:{request.remote_addr or 'unknown'}"
            if not security.login_attempt_allowed(throttle_key):
                flash("Too many sign-in attempts. Try again later.", "error")
                return render_template("auth/login.html", email=""), 429
            security.record_login_failure(throttle_key)
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html", email=""), 401

        password = request.form.get("password") or ""
        throttle_key = f"login:{security.fingerprint(email)}:{request.remote_addr or 'unknown'}"
        if not security.login_attempt_allowed(throttle_key):
            flash("Too many sign-in attempts. Try again later.", "error")
            return render_template("auth/login.html", email=email), 429

        user = User.query.filter_by(email=email).first()

        # Uniform failure path for unknown user / bad password.
        if user is None or not user.check_password(password):
            current_app.logger.warning(
                "Failed login attempt: email_fp=%s ip=%s",
                security.fingerprint(email),
                request.remote_addr,
            )
            security.record_login_failure(throttle_key)
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html", email=email), 401

        if user.status == "deactivated" or not user.is_active:
            security.record_login_failure(throttle_key)
            flash("This account has been deactivated. Please contact support.", "error")
            return render_template("auth/login.html", email=email), 403

        # Transparently upgrade legacy password hashes on successful login.
        if user.needs_rehash():
            user.set_password(password)
            db.session.commit()

        security.reset_login_attempts(throttle_key)
        if user.role == ROLE_ADMIN and user.admin_totp_enabled:
            session["pending_totp_user_id"] = user.id
            session["pending_totp_expires"] = utcnow().timestamp() + 300
            session["pending_totp_remember"] = bool(request.form.get("remember"))
            return redirect(url_for("auth.admin_totp_verify"))
        login_user(user, remember=bool(request.form.get("remember")))
        auth_utils.pin_session()

        token = session_tokens.login_with_token(user)
        if not user.email_verified:
            return session_tokens.redirect_with_token("auth.verify_notice", token)

        next_url = session_tokens.safe_next(
            request.args.get("next") or request.form.get("next")
        )
        if next_url:
            return redirect(session_tokens.with_token(next_url, token))
        return session_tokens.redirect_with_token(user.home_endpoint, token)

    return render_template("auth/login.html", email="")


@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if current_user.is_authenticated:
        if current_user.role == ROLE_ADMIN:
            return redirect(url_for("admin.dashboard"))
        logout_user()

    if request.method == "POST":
        try:
            email = security.validate_email(request.form.get("email"))
        except security.FieldError:
            throttle_key = f"admin-login-invalid:{request.remote_addr or 'unknown'}"
            if not security.login_attempt_allowed(throttle_key):
                flash("Too many sign-in attempts. Try again later.", "error")
                return render_template("auth/admin_login.html", email=""), 429
            security.record_login_failure(throttle_key)
            flash("Invalid email or password.", "error")
            return render_template("auth/admin_login.html", email=""), 401

        password = request.form.get("password") or ""
        throttle_key = f"admin-login:{security.fingerprint(email)}:{request.remote_addr or 'unknown'}"
        if not security.login_attempt_allowed(throttle_key):
            flash("Too many sign-in attempts. Try again later.", "error")
            return render_template("auth/admin_login.html", email=email), 429

        user = User.query.filter_by(email=email).first()

        if user is None or not user.check_password(password):
            current_app.logger.warning(
                "Failed admin login attempt: email_fp=%s ip=%s",
                security.fingerprint(email),
                request.remote_addr,
            )
            security.record_login_failure(throttle_key)
            flash("Invalid email or password.", "error")
            return render_template("auth/admin_login.html", email=email), 401

        if user.status == "deactivated" or not user.is_active:
            security.record_login_failure(throttle_key)
            flash("This account has been deactivated. Please contact support.", "error")
            return render_template("auth/admin_login.html", email=email), 403

        if user.role != ROLE_ADMIN:
            security.record_login_failure(throttle_key)
            flash("This sign-in page is for administrators only.", "error")
            return render_template("auth/admin_login.html", email=email), 403

        if user.needs_rehash():
            user.set_password(password)
            db.session.commit()

        security.reset_login_attempts(throttle_key)
        if user.admin_totp_enabled:
            session["pending_totp_user_id"] = user.id
            session["pending_totp_expires"] = utcnow().timestamp() + 300
            session["pending_totp_remember"] = bool(request.form.get("remember"))
            return redirect(url_for("auth.admin_totp_verify"))
        login_user(user, remember=bool(request.form.get("remember")))
        auth_utils.pin_session()

        token = session_tokens.login_with_token(user)
        if not user.email_verified:
            return session_tokens.redirect_with_token("auth.verify_notice", token)
        return session_tokens.redirect_with_token("admin.dashboard", token)

    return render_template("auth/admin_login.html", email="")


@auth_bp.route("/admin/two-factor/setup", methods=["GET", "POST"])
@login_required
def admin_totp_setup():
    if current_user.role != ROLE_ADMIN:
        abort(403)

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        secret = session.get("pending_totp_setup_secret")
        if not secret or not security.verify_totp(secret, code):
            flash("That verification code is invalid or expired.", "error")
            return render_template("auth/admin_totp_setup.html", secret=secret), 400
        current_user.admin_totp_secret = secret
        current_user.admin_totp_enabled = True
        db.session.commit()
        session.pop("pending_totp_setup_secret", None)
        flash("Administrator two-factor authentication enabled.", "success")
        return redirect(url_for("auth.account"))

    secret = security.generate_totp_secret()
    session["pending_totp_setup_secret"] = secret
    return render_template(
        "auth/admin_totp_setup.html",
        secret=secret,
        otpauth_uri=security.totp_uri(secret, current_user.email),
    )


@auth_bp.route("/admin/two-factor/verify", methods=["GET", "POST"])
def admin_totp_verify():
    user_id = session.get("pending_totp_user_id")
    expires = session.get("pending_totp_expires")
    user = db.session.get(User, int(user_id)) if user_id else None

    if user is None or not expires or utcnow().timestamp() > float(expires):
        session.pop("pending_totp_user_id", None)
        session.pop("pending_totp_expires", None)
        flash("The two-factor verification session expired. Sign in again.", "error")
        return redirect(url_for("auth.admin_login"))

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        if not user.admin_totp_enabled or not security.verify_totp(user.admin_totp_secret or "", code):
            flash("Invalid authenticator code.", "error")
            return render_template("auth/admin_totp_verify.html", email=user.email), 401

        session.pop("pending_totp_user_id", None)
        session.pop("pending_totp_expires", None)
        login_user(user, remember=bool(session.pop("pending_totp_remember", False)))
        auth_utils.pin_session()
        token = session_tokens.login_with_token(user)
        return session_tokens.redirect_with_token("admin.dashboard", token)

    return render_template("auth/admin_totp_verify.html", email=user.email)


@auth_bp.post("/logout")
@login_required
def logout():
    session_tokens.revoke_token(session_tokens.current_token())
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("public.index"))


# --------------------------------------------------------------------------- #
# Email verification
# --------------------------------------------------------------------------- #
@auth_bp.get("/verify")
def verify_email():
    token = request.args.get("token", "")
    user = tokens.read_token(token, tokens.VERIFY_SCOPE)
    if user is None:
        flash("That verification link is invalid or has expired.", "error")
        return render_template("auth/verify_result.html", ok=False), 400

    if not user.email_verified:
        user.mark_email_verified()
        db.session.commit()
        current_app.logger.info("Email verified for user_id=%s", user.id)

    flash("Your email address is verified.", "success")
    if current_user.is_authenticated:
        return redirect(
            url_for("auth.pending")
            if current_user.status != STATUS_APPROVED
            and current_user.role != ROLE_ADMIN
            else url_for(current_user.home_endpoint)
        )
    return render_template("auth/verify_result.html", ok=True)


@auth_bp.get("/verify/notice")
@login_required
def verify_notice():
    if current_user.email_verified:
        return redirect(url_for(current_user.home_endpoint))
    return render_template("auth/verify_notice.html")


@auth_bp.post("/verify/resend")
@login_required
def resend_verification():
    if current_user.email_verified:
        flash("Your email address is already verified.", "info")
        return redirect(url_for(current_user.home_endpoint))
    link = _send_verification(current_user)
    if link and current_app.config["SHOW_VERIFICATION_LINK"] and not current_app.config.get("MAIL_SERVER"):
        flash("New verification link — " + link, "info")
    elif not link and current_app.config.get("MAIL_SERVER"):
        flash("We couldn't send the verification email. Please try again later.", "error")
    else:
        flash("A new verification email is on its way.", "info")
    return redirect(url_for("auth.verify_notice"))


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #
@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        try:
            email = security.validate_email(request.form.get("email"))
        except security.FieldError:
            email = None
        user = User.query.filter_by(email=email).first() if email else None
        if user is not None:
            current_app.logger.info("Password reset requested for user_id=%s", user.id)
            link = tokens.reset_url(user)
            if current_app.config.get("MAIL_SERVER"):
                _send_email(
                    user,
                    "Reset your FuelNow password",
                    f"Open this link to reset your password:\n\n{link}",
                )
            elif current_app.config["SHOW_VERIFICATION_LINK"]:
                flash("Demo mode: reset link — " + link, "info")
        # Always the same response: never disclose whether an account exists.
        flash(
            "If that address has an account, a reset link has been sent.", "info"
        )
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = request.values.get("token", "")
    user = tokens.read_token(token, tokens.RESET_SCOPE)
    if user is None:
        flash("That reset link is invalid or has expired.", "error")
        return render_template("auth/forgot_password.html"), 400

    if request.method == "POST":
        try:
            new_password = security.validate_password(request.form.get("password"))
            if request.form.get("password") != request.form.get("confirm_password"):
                raise security.FieldError("Passwords do not match.")
        except security.FieldError as exc:
            flash(str(exc), "error")
            return render_template("auth/reset_password.html", token=token), 400

        user.set_password(new_password)
        db.session.commit()
        current_app.logger.info("Password reset completed for user_id=%s", user.id)
        flash("Your password has been changed. Please sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)


# --------------------------------------------------------------------------- #
# Approval pending / account
# --------------------------------------------------------------------------- #
@auth_bp.get("/pending")
@login_required
def pending():
    """Shown to station operators / mechanics awaiting admin approval."""
    if current_user.status == STATUS_APPROVED or current_user.role == ROLE_ADMIN:
        return redirect(url_for(current_user.home_endpoint))
    return render_template("auth/pending.html")


@auth_bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """Every role can update their own name, phone and last-known position."""
    if request.method == "POST":
        action = request.form.get("action")

        if action == "profile":
            try:
                current_user.full_name = security.clean_text(
                    request.form.get("full_name") or current_user.full_name,
                    min_len=2,
                    max_len=120,
                )
                current_user.phone = security.validate_phone(request.form.get("phone"))
                flash("Profile updated.", "success")
            except security.FieldError as exc:
                flash(str(exc), "error")

        elif action == "location":
            coords = parse_coords(
                request.form.get("latitude"), request.form.get("longitude")
            )
            if coords is None:
                flash("Enter a valid latitude and longitude.", "error")
            else:
                current_user.latitude = coords.latitude
                current_user.longitude = coords.longitude
                current_user.location_updated_at = utcnow()
                flash("Saved location updated.", "success")

        elif action == "disable_totp":
            if current_user.role == ROLE_ADMIN:
                current_user.admin_totp_secret = None
                current_user.admin_totp_enabled = False
                flash("Administrator two-factor authentication disabled.", "success")
            else:
                flash("This action is only available to administrators.", "error")

        elif action == "delete_account":
            if current_user.role == ROLE_ADMIN:
                active_admin_count = User.query.filter_by(
                    role=ROLE_ADMIN, status=STATUS_APPROVED
                ).count()
                if active_admin_count <= 1:
                    flash("The last administrator account cannot be deleted.", "error")
                else:
                    flash("Administrator accounts are deleted through the admin panel.", "error")
            else:
                current_pw = request.form.get("delete_password") or ""
                confirmation = (request.form.get("delete_confirmation") or "").strip()
                if confirmation != "DELETE":
                    flash("Type DELETE to confirm account deletion.", "error")
                elif not current_user.check_password(current_pw):
                    current_app.logger.warning(
                        "Failed account deletion (bad password) user_id=%s",
                        current_user.id,
                    )
                    flash("Current password is incorrect.", "error")
                else:
                    deleted_email = f"deleted-{current_user.id}@deleted.local"
                    current_user.email = deleted_email
                    current_user.full_name = "Deleted account"
                    current_user.phone = None
                    current_user.status = STATUS_DEACTIVATED
                    current_user.is_active_flag = False
                    current_user.email_verified = False
                    current_user.email_verified_at = None
                    current_user.latitude = None
                    current_user.longitude = None
                    current_user.location_updated_at = None
                    current_user.admin_totp_secret = None
                    current_user.admin_totp_enabled = False
                    current_user.set_password(secrets.token_urlsafe(32))
                    if current_user.station is not None:
                        current_user.station.status = STATUS_DEACTIVATED
                        current_user.station.is_open = False
                    if current_user.mechanic is not None:
                        current_user.mechanic.status = STATUS_DEACTIVATED
                        current_user.mechanic.available = False
                    db.session.commit()
                    session_tokens.revoke_token(session_tokens.current_token())
                    logout_user()
                    flash("Your account has been deleted and signed out.", "info")
                    return redirect(url_for("public.index"))

        elif action == "password":
            current_pw = request.form.get("current_password") or ""
            if not current_user.check_password(current_pw):
                current_app.logger.warning(
                    "Failed password change (bad current password) user_id=%s",
                    current_user.id,
                )
                flash("Current password is incorrect.", "error")
            else:
                try:
                    current_user.set_password(
                        security.validate_password(request.form.get("new_password"))
                    )
                    flash("Password changed.", "success")
                except security.FieldError as exc:
                    flash(str(exc), "error")

        db.session.commit()
        return redirect(url_for("auth.account"))

    return render_template("auth/account.html")
