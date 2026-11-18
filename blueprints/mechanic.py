"""Mechanic workspace.

GET  /mechanic/                        dashboard: availability, in-area requests
GET  /mechanic/availability            set availability + service area (POST)
GET  /mechanic/requests                matched requests, filter by status
POST /mechanic/requests/<id>/status    advance a service request
POST /mechanic/requests/<id>/decline   decline a pending request
GET  /mechanic/profile                 business profile (POST to save)
"""
from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

import security
from auth_utils import role_required
from extensions import db
from geo import LatLng, distance_km, is_within_service_area, parse_coords
from models import (
    REQUEST_ACTIVE_STATUSES,
    REQUEST_COMPLETED,
    REQUEST_PENDING,
    REQUEST_TRANSITIONS,
    ROLE_MECHANIC,
    STATUS_APPROVED,
    Mechanic,
    ServiceRequest,
    utcnow,
)

mechanic_bp = Blueprint("mechanic", __name__, template_folder="../templates")


def _mechanic_or_404() -> Mechanic:
    mechanic = Mechanic.query.filter_by(user_id=current_user.id).first()
    if mechanic is None:
        abort(404)
    return mechanic


def _matched_requests(mechanic: Mechanic, status: str | None = None):
    """Every request addressed to this mechanic, newest first.

    Drivers can only create a request when they are inside the mechanic's
    service radius (enforced on creation), so the queue is already "in area" —
    but we re-check each one so results stay correct after a radius change.
    Results are sorted nearest-first (reusing geo.is_within_service_area).
    """
    query = ServiceRequest.query.filter_by(mechanic_id=mechanic.id)
    if status:
        query = query.filter_by(status=status)
    rows = query.order_by(ServiceRequest.created_at.desc()).all()

    matched = []
    for service_request in rows:
        point = LatLng.from_obj(service_request)
        if point is None:
            continue
        in_area, dist = is_within_service_area(mechanic, point)
        if in_area or service_request.status != REQUEST_PENDING:
            # Keep accepted/completed history visible even if it drifted out of area.
            matched.append((service_request, dist))

    # Nearest jobs first, then most recent as the tie-breaker.
    matched.sort(key=lambda pair: (pair[1], -pair[0].created_at.timestamp()))
    return matched


def _stats(mechanic: Mechanic) -> dict:
    rows = ServiceRequest.query.filter_by(mechanic_id=mechanic.id).all()
    return {
        "pending": sum(1 for r in rows if r.status == REQUEST_PENDING),
        "active": sum(1 for r in rows if r.status in REQUEST_ACTIVE_STATUSES),
        "completed": sum(1 for r in rows if r.status == REQUEST_COMPLETED),
        "total": len(rows),
    }


@mechanic_bp.get("/")
@role_required(ROLE_MECHANIC)
def dashboard():
    mechanic = _mechanic_or_404()
    matched = _matched_requests(mechanic)
    return render_template(
        "mechanic/dashboard.html",
        mechanic=mechanic,
        matched=matched[:10],
        stats=_stats(mechanic),
        transitions=REQUEST_TRANSITIONS,
    )


@mechanic_bp.route("/availability", methods=["GET", "POST"])
@role_required(ROLE_MECHANIC)
def availability():
    mechanic = _mechanic_or_404()

    if request.method == "POST":
        try:
            service_radius = security.validate_float(
                request.form.get("service_radius_km"),
                minimum=1,
                maximum=200,
                field="service radius",
            )
        except security.FieldError as exc:
            flash(str(exc), "error")
            return redirect(url_for("mechanic.availability"))

        coords = parse_coords(request.form.get("latitude"), request.form.get("longitude"))
        try:
            city = security.clean_text(request.form.get("city"), max_len=80)
        except security.FieldError as exc:
            flash(str(exc), "error")
            return redirect(url_for("mechanic.availability"))

        mechanic.available = request.form.get("available") == "on"
        mechanic.service_radius_km = service_radius
        if coords:
            mechanic.latitude = coords.latitude
            mechanic.longitude = coords.longitude
        elif request.form.get("latitude") or request.form.get("longitude"):
            flash("Invalid latitude/longitude — base location not changed.", "warning")
        if city:
            mechanic.city = city

        db.session.commit()
        flash(
            "Availability updated — you are "
            + ("accepting jobs." if mechanic.available else "not accepting jobs."),
            "success",
        )
        return redirect(url_for("mechanic.availability"))

    return render_template(
        "mechanic/availability.html",
        mechanic=mechanic,
        stats=_stats(mechanic),
        matched=_matched_requests(mechanic, REQUEST_PENDING),
    )


@mechanic_bp.get("/requests")
@role_required(ROLE_MECHANIC)
def requests_view():
    mechanic = _mechanic_or_404()
    status = (request.args.get("status") or "").strip()
    matched = _matched_requests(mechanic, status or None)
    return render_template(
        "mechanic/requests.html",
        mechanic=mechanic,
        matched=matched,
        stats=_stats(mechanic),
        selected_status=status,
        transitions=REQUEST_TRANSITIONS,
    )


@mechanic_bp.post("/requests/<int:request_id>/status")
@role_required(ROLE_MECHANIC)
def update_request_status(request_id: int):
    mechanic = _mechanic_or_404()
    service_request = db.session.get(ServiceRequest, request_id)
    if service_request is None or service_request.mechanic_id != mechanic.id:
        flash("Request not found.", "error")
        return redirect(url_for("mechanic.requests_view"))

    new_status = (request.form.get("status") or "").strip()
    if new_status not in REQUEST_TRANSITIONS.get(service_request.status, ()):
        flash(
            f"Cannot move a request from “{service_request.status_label}” to "
            f"“{new_status.replace('_', ' ').title()}”.",
            "error",
        )
        return redirect(url_for("mechanic.requests_view"))

    if not mechanic.available and new_status != "cancelled":
        flash(
            "You are marked unavailable — turn availability on before accepting jobs.",
            "error",
        )
        return redirect(url_for("mechanic.requests_view"))

    service_request.status = new_status
    service_request.updated_at = utcnow()
    db.session.commit()
    flash(
        f"Request {service_request.reference} is now “{service_request.status_label}”.",
        "success",
    )
    return redirect(url_for("mechanic.requests_view"))


@mechanic_bp.post("/requests/<int:request_id>/decline")
@role_required(ROLE_MECHANIC)
def decline_request(request_id: int):
    mechanic = _mechanic_or_404()
    service_request = db.session.get(ServiceRequest, request_id)
    if service_request is None or service_request.mechanic_id != mechanic.id:
        flash("Request not found.", "error")
    elif service_request.status != REQUEST_PENDING:
        flash("Only pending requests can be declined.", "error")
    else:
        service_request.status = "cancelled"
        service_request.updated_at = utcnow()
        db.session.commit()
        flash(f"Request {service_request.reference} declined.", "info")
    return redirect(url_for("mechanic.requests_view"))


@mechanic_bp.route("/profile", methods=["GET", "POST"])
@role_required(ROLE_MECHANIC)
def profile():
    mechanic = _mechanic_or_404()

    if request.method == "POST":
        try:
            business_name = security.clean_text(
                request.form.get("business_name") or mechanic.business_name,
                min_len=2, max_len=120,
            )
            specialties = security.clean_text(
                request.form.get("specialties") or mechanic.specialties, max_len=255
            )
            address = security.clean_text(
                request.form.get("address"), max_len=255
            ) or None
            city = security.clean_text(
                request.form.get("city") or mechanic.city, min_len=2, max_len=80
            )
            full_name = security.clean_text(
                request.form.get("full_name") or current_user.full_name,
                min_len=2, max_len=120,
            )
            phone = security.validate_phone(request.form.get("phone"))
            rate_raw = request.form.get("hourly_rate")
            hourly_rate = (
                None
                if rate_raw in (None, "")
                else security.validate_float(
                    rate_raw, minimum=0, maximum=1_000_000, field="hourly rate"
                )
            )
        except security.FieldError as exc:
            flash(str(exc), "error")
            return redirect(url_for("mechanic.profile"))

        coords = parse_coords(request.form.get("latitude"), request.form.get("longitude"))
        mechanic.business_name = business_name
        mechanic.specialties = specialties
        mechanic.address = address
        mechanic.city = city
        current_user.full_name = full_name
        current_user.phone = phone
        mechanic.hourly_rate = hourly_rate
        if coords:
            mechanic.latitude = coords.latitude
            mechanic.longitude = coords.longitude

        db.session.commit()
        flash("Profile saved.", "success")
        return redirect(url_for("mechanic.profile"))

    return render_template(
        "mechanic/profile.html", mechanic=mechanic, stats=_stats(mechanic)
    )
