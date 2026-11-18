"""Driver (end-user) workspace.

Routes
------
GET  /driver/                     dashboard: nearby stations + mechanics
GET  /driver/stations             full nearby-station list
GET  /driver/mechanics            full nearby-mechanic list
POST /driver/location             JSON endpoint, persists browser geolocation
POST /driver/orders               place a fuel order
GET  /driver/orders               my orders + tracking
POST /driver/orders/<id>/cancel   cancel a pending/accepted order
POST /driver/requests             request a mechanic
GET  /driver/requests             my service requests
POST /driver/requests/<id>/cancel cancel a service request
"""
from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

import security
import session_tokens
from auth_utils import role_required
from extensions import db
from geo import (
    LatLng,
    clamp_radius,
    distance_km,
    fuel_types_offered,
    is_within_service_area,
    nearby_mechanics,
    nearby_stations,
    parse_coords,
    resolve_driver_location,
)
from models import (
    ORDER_ACTIVE_STATUSES,
    ORDER_PENDING,
    REQUEST_ACTIVE_STATUSES,
    REQUEST_PENDING,
    ROLE_DRIVER,
    STATUS_APPROVED,
    FuelOrder,
    FuelProduct,
    Mechanic,
    ServiceRequest,
    Station,
    utcnow,
)
from blueprints.public import _reference

driver_bp = Blueprint("driver", __name__, template_folder="../templates")

MAX_LITRES = 500.0
MIN_LITRES = 1.0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _driver_stats(user) -> dict:
    orders = FuelOrder.query.filter_by(driver_id=user.id)
    requests = ServiceRequest.query.filter_by(driver_id=user.id)
    return {
        "active_orders": orders.filter(FuelOrder.status.in_(ORDER_ACTIVE_STATUSES)).count(),
        "total_orders": orders.count(),
        "active_requests": requests.filter(
            ServiceRequest.status.in_(REQUEST_ACTIVE_STATUSES)
        ).count(),
        "total_requests": requests.count(),
    }


def _location_banner(origin, from_browser: bool) -> dict:
    """Small dict the templates use to explain where the distances came from."""
    if origin is None:
        return {"state": "missing", "from_browser": False}
    return {
        "state": "ok",
        "from_browser": from_browser,
        "latitude": round(origin.latitude, 5),
        "longitude": round(origin.longitude, 5),
    }


# --------------------------------------------------------------------------- #
# Dashboard & listings
# --------------------------------------------------------------------------- #
@driver_bp.get("/")
@role_required(ROLE_DRIVER)
def dashboard():
    origin, from_browser = resolve_driver_location(request.args, current_user)
    radius = clamp_radius(request.args.get("radius"))
    fuel_type = (request.args.get("fuel_type") or "").strip() or None

    stations, mechanics = [], []
    if origin:
        stations = nearby_stations(origin, radius, fuel_type=fuel_type, limit=5)
        mechanics = nearby_mechanics(origin, radius, limit=5)

    recent_orders = (
        FuelOrder.query.filter_by(driver_id=current_user.id)
        .order_by(FuelOrder.created_at.desc())
        .limit(5)
        .all()
    )
    recent_requests = (
        ServiceRequest.query.filter_by(driver_id=current_user.id)
        .order_by(ServiceRequest.created_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "driver/dashboard.html",
        origin=origin,
        location=_location_banner(origin, from_browser),
        radius=radius,
        fuel_type=fuel_type,
        fuel_types=fuel_types_offered(),
        stations=stations,
        mechanics=mechanics,
        recent_orders=recent_orders,
        recent_requests=recent_requests,
        stats=_driver_stats(current_user),
    )


@driver_bp.get("/stations")
@role_required(ROLE_DRIVER)
def stations():
    origin, from_browser = resolve_driver_location(request.args, current_user)
    radius = clamp_radius(request.args.get("radius"))
    fuel_type = (request.args.get("fuel_type") or "").strip() or None
    results = nearby_stations(origin, radius, fuel_type=fuel_type) if origin else []

    return render_template(
        "driver/stations.html",
        origin=origin,
        location=_location_banner(origin, from_browser),
        radius=radius,
        fuel_type=fuel_type,
        fuel_types=fuel_types_offered(),
        results=results,
    )


@driver_bp.get("/mechanics")
@role_required(ROLE_DRIVER)
def mechanics():
    origin, from_browser = resolve_driver_location(request.args, current_user)
    radius = clamp_radius(request.args.get("radius"), default=25)
    results = nearby_mechanics(origin, radius) if origin else []

    return render_template(
        "driver/mechanics.html",
        origin=origin,
        location=_location_banner(origin, from_browser),
        radius=radius,
        results=results,
        issues=SERVICE_ISSUES,
    )


SERVICE_ISSUES = [
    "Engine won't start",
    "Battery / electrical",
    "Flat tyre",
    "Brakes",
    "Overheating",
    "Fuel system",
    "Transmission",
    "General inspection",
    "Other",
]


# --------------------------------------------------------------------------- #
# Location endpoint (called by LocationHelper in static/js/app.js)
# --------------------------------------------------------------------------- #
@driver_bp.post("/location")
@role_required(ROLE_DRIVER)
def save_location():
    payload = request.get_json(silent=True) or request.form
    coords = parse_coords(payload.get("latitude"), payload.get("longitude"))
    if coords is None:
        return jsonify(ok=False, error="Invalid coordinates"), 400

    current_user.latitude = coords.latitude
    current_user.longitude = coords.longitude
    current_user.location_updated_at = utcnow()
    db.session.commit()

    return jsonify(
        ok=True,
        latitude=coords.latitude,
        longitude=coords.longitude,
        redirect=url_for(
            "driver.dashboard", lat=coords.latitude, lng=coords.longitude
        ),
    )


# --------------------------------------------------------------------------- #
# Fuel orders
# --------------------------------------------------------------------------- #
@driver_bp.post("/orders")
@role_required(ROLE_DRIVER)
def place_order():
    form = request.form
    if not security.consume_idempotency_token(form.get("idempotency_token")):
        flash("This order may already have been submitted.", "warning")
        return redirect(url_for("driver.orders"))
    station_id = form.get("station_id", type=int)
    fuel_type = (form.get("fuel_type") or "").strip()
    litres_raw = form.get("quantity_litres")
    back_to = session_tokens.safe_next(form.get("next")) or url_for("driver.stations")

    station = db.session.get(Station, station_id) if station_id else None
    if station is None or station.status != STATUS_APPROVED:
        flash("That station is not available for orders.", "error")
        return redirect(back_to)

    try:
        litres = security.validate_float(
            litres_raw, minimum=MIN_LITRES, maximum=MAX_LITRES, field="quantity"
        )
    except security.FieldError as exc:
        flash(str(exc), "error")
        return redirect(back_to)

    # Fuel type must be one the station actually lists (already queried below),
    # and free-text fields are length- and markup-checked.
    try:
        vehicle_plate = security.validate_plate(form.get("vehicle_plate"))
        delivery_note = security.clean_text(
            form.get("delivery_note"), max_len=255
        ) or None
    except security.FieldError as exc:
        flash(str(exc), "error")
        return redirect(back_to)

    product = FuelProduct.query.filter_by(
        station_id=station.id, fuel_type=fuel_type
    ).first()
    if product is None:
        flash(f"{station.name} does not sell {fuel_type or 'that fuel type'}.", "error")
        return redirect(back_to)
    if not product.in_stock:
        flash(f"{fuel_type} is currently out of stock at {station.name}.", "error")
        return redirect(back_to)

    origin, _ = resolve_driver_location(form, current_user)
    dist = distance_km(origin, LatLng(station.latitude, station.longitude)) if origin else None

    order = FuelOrder(
        reference=_reference("FO"),
        driver_id=current_user.id,
        station_id=station.id,
        fuel_type=fuel_type,
        quantity_litres=round(litres, 2),
        unit_price=product.price_per_litre,
        total_price=round(litres * product.price_per_litre, 2),
        vehicle_plate=vehicle_plate,
        delivery_note=delivery_note,
        driver_latitude=origin.latitude if origin else None,
        driver_longitude=origin.longitude if origin else None,
        distance_km=round(dist, 2) if dist is not None else None,
        status=ORDER_PENDING,
    )
    db.session.add(order)
    db.session.commit()

    flash(
        f"Order {order.reference} placed at {station.name}. "
        f"Total {current_app_currency()} {order.total_price:,.2f}.",
        "success",
    )
    return redirect(url_for("driver.orders"))


def current_app_currency() -> str:
    from flask import current_app

    return current_app.config["DEFAULT_CURRENCY"]


@driver_bp.get("/orders")
@role_required(ROLE_DRIVER)
def orders():
    status = (request.args.get("status") or "").strip()
    query = FuelOrder.query.filter_by(driver_id=current_user.id)
    if status:
        query = query.filter_by(status=status)
    order_rows = query.order_by(FuelOrder.created_at.desc()).all()

    return render_template(
        "driver/orders.html",
        orders=order_rows,
        stats=_driver_stats(current_user),
        selected_status=status,
    )


@driver_bp.post("/orders/<int:order_id>/cancel")
@role_required(ROLE_DRIVER)
def cancel_order(order_id: int):
    order = db.session.get(FuelOrder, order_id)
    if order is None or order.driver_id != current_user.id:
        flash("Order not found.", "error")
        return redirect(url_for("driver.orders"))
    if not order.can_cancel:
        flash("This order can no longer be cancelled.", "error")
        return redirect(url_for("driver.orders"))

    order.status = "cancelled"
    order.updated_at = utcnow()
    db.session.commit()
    flash(f"Order {order.reference} cancelled.", "info")
    return redirect(url_for("driver.orders"))


# --------------------------------------------------------------------------- #
# Mechanic service requests
# --------------------------------------------------------------------------- #
@driver_bp.post("/requests")
@role_required(ROLE_DRIVER)
def create_request():
    form = request.form
    if not security.consume_idempotency_token(form.get("idempotency_token")):
        flash("This mechanic request may already have been submitted.", "warning")
        return redirect(url_for("driver.requests_view"))
    mechanic_id = form.get("mechanic_id", type=int)
    back_to = session_tokens.safe_next(form.get("next")) or url_for("driver.mechanics")
    try:
        issue = security.validate_choice(form.get("issue"), SERVICE_ISSUES, field="issue")
        description = security.clean_text(
            form.get("description"), max_len=1000, allow_newlines=True
        ) or None
        vehicle = security.clean_text(form.get("vehicle"), max_len=120) or None
    except security.FieldError as exc:
        flash(str(exc), "error")
        return redirect(back_to)

    mechanic = db.session.get(Mechanic, mechanic_id) if mechanic_id else None
    if mechanic is None or mechanic.status != STATUS_APPROVED:
        flash("That mechanic is not available.", "error")
        return redirect(back_to)
    if not mechanic.available:
        flash(f"{mechanic.business_name} is currently not accepting jobs.", "error")
        return redirect(back_to)
    origin, _ = resolve_driver_location(form, current_user)
    if origin is None:
        flash("We need your location to send this request. Allow location access first.", "error")
        return redirect(back_to)

    in_area, dist = is_within_service_area(mechanic, origin)
    if not in_area:
        flash(
            f"{mechanic.business_name} only covers {mechanic.service_radius_km:.0f} km "
            f"around {mechanic.city}; you are {dist:.1f} km away.",
            "error",
        )
        return redirect(back_to)

    service_request = ServiceRequest(
        reference=_reference("SR"),
        driver_id=current_user.id,
        mechanic_id=mechanic.id,
        issue=issue,
        description=description,
        vehicle=vehicle,
        latitude=origin.latitude,
        longitude=origin.longitude,
        distance_km=round(dist, 2),
        status=REQUEST_PENDING,
    )
    db.session.add(service_request)
    db.session.commit()

    flash(
        f"Request {service_request.reference} sent to {mechanic.business_name}.",
        "success",
    )
    return redirect(url_for("driver.requests_view"))


@driver_bp.get("/requests")
@role_required(ROLE_DRIVER)
def requests_view():
    rows = (
        ServiceRequest.query.filter_by(driver_id=current_user.id)
        .order_by(ServiceRequest.created_at.desc())
        .all()
    )
    return render_template(
        "driver/requests.html", requests=rows, stats=_driver_stats(current_user)
    )


@driver_bp.post("/requests/<int:request_id>/cancel")
@role_required(ROLE_DRIVER)
def cancel_request(request_id: int):
    service_request = db.session.get(ServiceRequest, request_id)
    if service_request is None or service_request.driver_id != current_user.id:
        flash("Request not found.", "error")
        return redirect(url_for("driver.requests_view"))
    if not service_request.can_cancel:
        flash("This request is already in progress and cannot be cancelled.", "error")
        return redirect(url_for("driver.requests_view"))

    service_request.status = "cancelled"
    service_request.updated_at = utcnow()
    db.session.commit()
    flash(f"Request {service_request.reference} cancelled.", "info")
    return redirect(url_for("driver.requests_view"))
