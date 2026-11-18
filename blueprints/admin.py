"""Administrator workspace: approvals, deactivation and monitoring.

GET  /admin/                          dashboard with KPIs + pending queue
GET  /admin/users                     all accounts (filter by role/status)
POST /admin/users/<id>/status         approve / deactivate / reactivate a user
GET  /admin/stations                  all stations
POST /admin/stations/<id>/status      approve / deactivate a station
GET  /admin/mechanics                 all mechanics
POST /admin/mechanics/<id>/status     approve / deactivate a mechanic
GET  /admin/orders                    every fuel order
GET  /admin/requests                  every service request
"""
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func

import security
import session_tokens
from auth_utils import admin_required
from extensions import db
from models import (  # noqa: F401
    ACCOUNT_STATUSES,
    AuditLog,
    ORDER_ACTIVE_STATUSES,
    ORDER_COMPLETED,
    REQUEST_ACTIVE_STATUSES,
    REQUEST_COMPLETED,
    ROLE_ADMIN,
    ROLES,
    STATUS_APPROVED,
    STATUS_DEACTIVATED,
    STATUS_PENDING,
    FuelOrder,
    Mechanic,
    ServiceRequest,
    Station,
    User,
)

admin_bp = Blueprint("admin", __name__, template_folder="../templates")

VALID_STATUSES = (STATUS_APPROVED, STATUS_PENDING, STATUS_DEACTIVATED)


def _platform_stats() -> dict:
    completed_orders = FuelOrder.query.filter_by(status=ORDER_COMPLETED).all()
    return {
        "drivers": User.query.filter_by(role="driver").count(),
        "operators": User.query.filter_by(role="station").count(),
        "mechanics": Mechanic.query.count(),
        "stations": Station.query.count(),
        "pending_users": User.query.filter_by(status=STATUS_PENDING).count(),
        "pending_stations": Station.query.filter_by(status=STATUS_PENDING).count(),
        "pending_mechanics": Mechanic.query.filter_by(status=STATUS_PENDING).count(),
        "orders": FuelOrder.query.count(),
        "active_orders": FuelOrder.query.filter(
            FuelOrder.status.in_(ORDER_ACTIVE_STATUSES)
        ).count(),
        "active_requests": ServiceRequest.query.filter(
            ServiceRequest.status.in_(REQUEST_ACTIVE_STATUSES)
        ).count(),
        "requests": ServiceRequest.query.count(),
        "completed_requests": ServiceRequest.query.filter_by(
            status=REQUEST_COMPLETED
        ).count(),
        "revenue": round(sum(o.total_price for o in completed_orders), 2),
        "litres": round(sum(o.quantity_litres for o in completed_orders), 2),
    }


def _set_status(entity, new_status: str, label: str) -> str | None:
    """Shared approve/deactivate logic. Returns an error message or None.

    `new_status` is validated against the allow-list before it can touch the DB,
    so an unexpected value can never reach a column.
    """
    if new_status not in VALID_STATUSES:
        return "Unknown status."
    old_status = entity.status
    entity.status = new_status
    # Keep the user account and its business record in sync.
    user = None
    if isinstance(entity, User):
        user = entity
    elif isinstance(entity, Station):
        user = entity.owner
        if new_status == STATUS_DEACTIVATED:
            entity.is_open = False
    elif isinstance(entity, Mechanic):
        user = entity.user
        if new_status == STATUS_DEACTIVATED:
            entity.available = False
    if user is not None:
        if new_status == STATUS_DEACTIVATED:
            user.status = STATUS_DEACTIVATED
            user.is_active_flag = False
        elif new_status == STATUS_APPROVED:
            user.status = STATUS_APPROVED
            user.is_active_flag = True
        else:
            user.status = STATUS_PENDING
    target_type = (
        "user" if isinstance(entity, User)
        else "station" if isinstance(entity, Station)
        else "mechanic"
    )
    db.session.add(AuditLog(
        admin_id=current_user.id,
        action="status_changed",
        target_type=target_type,
        target_id=entity.id,
        old_status=old_status,
        new_status=new_status,
        details=label,
        ip_address=request.remote_addr,
    ))
    db.session.commit()
    flash(f"{label} set to “{new_status}”.", "success")
    return None


@admin_bp.get("/")
@admin_required
def dashboard():
    pending_stations = Station.query.filter_by(status=STATUS_PENDING).all()
    pending_mechanics = Mechanic.query.filter_by(status=STATUS_PENDING).all()
    recent_orders = (
        FuelOrder.query.order_by(FuelOrder.created_at.desc()).limit(8).all()
    )
    recent_requests = (
        ServiceRequest.query.order_by(ServiceRequest.created_at.desc()).limit(8).all()
    )
    return render_template(
        "admin/dashboard.html",
        stats=_platform_stats(),
        pending_stations=pending_stations,
        pending_mechanics=pending_mechanics,
        recent_orders=recent_orders,
        recent_requests=recent_requests,
    )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@admin_bp.get("/users")
@admin_required
def users():
    role = (request.args.get("role") or "").strip()
    status = (request.args.get("status") or "").strip()
    query = User.query
    if role in ROLES:
        query = query.filter_by(role=role)
    if status in ACCOUNT_STATUSES:
        query = query.filter_by(status=status)
    rows = query.order_by(User.created_at.desc()).all()
    return render_template(
        "admin/users.html",
        users=rows,
        stats=_platform_stats(),
        selected_role=role,
        selected_status=status,
        roles=ROLES,
        statuses=ACCOUNT_STATUSES,
    )


@admin_bp.post("/users/<int:user_id>/status")
@admin_required
def update_user_status(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        flash("User not found.", "error")
        return redirect(url_for("admin.users"))

    if user.id == current_user.id:
        flash("You cannot change your own account status.", "error")
        return redirect(url_for("admin.users"))

    try:
        new_status = security.validate_choice(
            request.form.get("status"), VALID_STATUSES, field="status"
        )
    except security.FieldError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.users"))

    if user.role == ROLE_ADMIN and new_status == STATUS_DEACTIVATED:
        admins = User.query.filter_by(role=ROLE_ADMIN, status=STATUS_APPROVED).count()
        if admins <= 1:
            flash("At least one active administrator must remain.", "error")
            return redirect(url_for("admin.users"))

    error = _set_status(user, new_status, f"{user.full_name} ({user.email})")
    if error:
        flash(error, "error")
    return redirect(
        session_tokens.safe_next(request.form.get("next")) or url_for("admin.users")
    )


# --------------------------------------------------------------------------- #
# Stations
# --------------------------------------------------------------------------- #
@admin_bp.get("/stations")
@admin_required
def stations():
    status = (request.args.get("status") or "").strip()
    query = Station.query
    if status in ACCOUNT_STATUSES:
        query = query.filter_by(status=status)
    return render_template(
        "admin/stations.html",
        stations=query.order_by(Station.created_at.desc()).all(),
        stats=_platform_stats(),
        selected_status=status,
    )


@admin_bp.post("/stations/<int:station_id>/status")
@admin_required
def update_station_status(station_id: int):
    station = db.session.get(Station, station_id)
    if station is None:
        flash("Station not found.", "error")
        return redirect(url_for("admin.stations"))
    error = _set_status(
        station, (request.form.get("status") or "").strip(), station.name
    )
    if error:
        flash(error, "error")
    return redirect(
        session_tokens.safe_next(request.form.get("next"))
        or url_for("admin.stations")
    )


# --------------------------------------------------------------------------- #
# Mechanics
# --------------------------------------------------------------------------- #
@admin_bp.get("/mechanics")
@admin_required
def mechanics():
    status = (request.args.get("status") or "").strip()
    query = Mechanic.query
    if status in ACCOUNT_STATUSES:
        query = query.filter_by(status=status)
    return render_template(
        "admin/mechanics.html",
        mechanics=query.order_by(Mechanic.created_at.desc()).all(),
        stats=_platform_stats(),
        selected_status=status,
    )


@admin_bp.post("/mechanics/<int:mechanic_id>/status")
@admin_required
def update_mechanic_status(mechanic_id: int):
    mechanic = db.session.get(Mechanic, mechanic_id)
    if mechanic is None:
        flash("Mechanic not found.", "error")
        return redirect(url_for("admin.mechanics"))
    error = _set_status(
        mechanic, (request.form.get("status") or "").strip(), mechanic.business_name
    )
    if error:
        flash(error, "error")
    return redirect(
        session_tokens.safe_next(request.form.get("next"))
        or url_for("admin.mechanics")
    )


# --------------------------------------------------------------------------- #
# Monitoring
# --------------------------------------------------------------------------- #
@admin_bp.get("/audit")
@admin_required
def audit():
    rows = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(250).all()
    return render_template("admin/audit.html", audit_logs=rows)


@admin_bp.get("/orders")
@admin_required
def orders():
    status = (request.args.get("status") or "").strip()
    query = FuelOrder.query
    if status:
        query = query.filter_by(status=status)
    return render_template(
        "admin/orders.html",
        orders=query.order_by(FuelOrder.created_at.desc()).all(),
        stats=_platform_stats(),
        selected_status=status,
    )


@admin_bp.get("/requests")
@admin_required
def requests_view():
    status = (request.args.get("status") or "").strip()
    query = ServiceRequest.query
    if status:
        query = query.filter_by(status=status)
    return render_template(
        "admin/requests.html",
        requests=query.order_by(ServiceRequest.created_at.desc()).all(),
        stats=_platform_stats(),
        selected_status=status,
    )
