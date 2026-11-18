"""Fueling-station operator workspace.

GET  /station/                         dashboard (KPIs + incoming orders)
GET  /station/orders                   all orders with status filter
POST /station/orders/<id>/status       advance order (accepted/fueling/completed/cancelled)
GET  /station/products                 manage fuel types & prices
POST /station/products                 add a fuel type
POST /station/products/<id>/update     change price / stock
POST /station/products/<id>/delete     remove a fuel type
GET|POST /station/profile              station details, open/closed toggle
"""
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user

import security
from auth_utils import role_required
from extensions import db
import security
import uploads
import session_tokens
from geo import LatLng, distance_km, parse_coords
from models import (
    ORDER_ACTIVE_STATUSES,
    ORDER_COMPLETED,
    ORDER_PENDING,
    ORDER_STATUSES,
    ORDER_TRANSITIONS,
    ROLE_STATION,
    STATUS_APPROVED,
    STATUS_DEACTIVATED,
    STATUS_PENDING,
    FuelOrder,
    FuelProduct,
    Station,
    utcnow,
)

station_bp = Blueprint("station", __name__, template_folder="../templates")

FUEL_TYPE_CHOICES = [
    "Petrol (PMS)",
    "Diesel (AGO)",
    "Kerosene (DPK)",
    "CNG",
    "LPG",
    "EV charging",
]


def _station_or_404() -> Station:
    station = Station.query.filter_by(owner_id=current_user.id).first()
    if station is None:
        abort(404)
    return station


def _stats(station: Station) -> dict:
    orders = FuelOrder.query.filter_by(station_id=station.id)
    completed = orders.filter_by(status=ORDER_COMPLETED).all()
    return {
        "pending": orders.filter_by(status=ORDER_PENDING).count(),
        "active": orders.filter(FuelOrder.status.in_(ORDER_ACTIVE_STATUSES)).count(),
        "completed": len(completed),
        "revenue": round(sum(o.total_price for o in completed), 2),
        "litres": round(sum(o.quantity_litres for o in completed), 2),
        "products": len(station.products),
    }


@station_bp.get("/")
@role_required(ROLE_STATION)
def dashboard():
    station = _station_or_404()
    incoming = (
        FuelOrder.query.filter_by(station_id=station.id)
        .filter(FuelOrder.status.in_((ORDER_PENDING, "accepted", "fueling")))
        .order_by(FuelOrder.created_at.asc())
        .all()
    )
    recent = (
        FuelOrder.query.filter_by(station_id=station.id)
        .order_by(FuelOrder.created_at.desc())
        .limit(8)
        .all()
    )
    return render_template(
        "station/dashboard.html",
        station=station,
        incoming=incoming,
        recent=recent,
        stats=_stats(station),
        transitions=ORDER_TRANSITIONS,
    )


@station_bp.get("/orders")
@role_required(ROLE_STATION)
def orders():
    station = _station_or_404()
    status = (request.args.get("status") or "").strip()
    query = FuelOrder.query.filter_by(station_id=station.id)
    if status:
        query = query.filter_by(status=status)
    rows = query.order_by(FuelOrder.created_at.desc()).all()
    return render_template(
        "station/orders.html",
        station=station,
        orders=rows,
        stats=_stats(station),
        selected_status=status,
        transitions=ORDER_TRANSITIONS,
    )


@station_bp.post("/orders/<int:order_id>/status")
@role_required(ROLE_STATION)
def update_order_status(order_id: int):
    station = _station_or_404()
    order = db.session.get(FuelOrder, order_id)
    if order is None or order.station_id != station.id:
        flash("Order not found for this station.", "error")
        return redirect(url_for("station.orders"))

    new_status = (request.form.get("status") or "").strip()
    if new_status not in ORDER_STATUSES:
        flash("Unknown order status.", "error")
        return redirect(url_for("station.orders"))
    if new_status not in ORDER_TRANSITIONS.get(order.status, ()):
        flash(
            f"Cannot move an order from “{order.status_label}” to "
            f"“{new_status.replace('_', ' ').title()}”.",
            "error",
        )
        return redirect(
            session_tokens.safe_next(request.form.get("next"))
            or url_for("station.orders")
        )

    order.status = new_status
    order.updated_at = utcnow()
    db.session.commit()
    flash(
        f"Order {order.reference} is now “{order.status_label}”.", "success"
    )
    return redirect(
        session_tokens.safe_next(request.form.get("next"))
        or url_for("station.orders")
    )


# --------------------------------------------------------------------------- #
# Fuel products / prices
# --------------------------------------------------------------------------- #
@station_bp.get("/products")
@role_required(ROLE_STATION)
def products():
    station = _station_or_404()
    return render_template(
        "station/products.html",
        station=station,
        products=station.products,
        fuel_choices=FUEL_TYPE_CHOICES,
        stats=_stats(station),
    )


@station_bp.post("/products")
@role_required(ROLE_STATION)
def add_product():
    station = _station_or_404()
    fuel_type = (request.form.get("fuel_type") or "").strip()
    price_raw = request.form.get("price_per_litre")

    try:
        fuel_type = security.validate_choice(fuel_type, FUEL_TYPE_CHOICES, field="fuel type")
        price = security.validate_float(price_raw, minimum=0.01, maximum=100_000, field="price")
    except security.FieldError as exc:
        flash(str(exc), "error")
        return redirect(url_for("station.products"))

    if FuelProduct.query.filter_by(station_id=station.id, fuel_type=fuel_type).first():
        flash(f"{fuel_type} is already listed — edit its price below.", "warning")
        return redirect(url_for("station.products"))

    db.session.add(
        FuelProduct(
            station_id=station.id,
            fuel_type=fuel_type,
            price_per_litre=round(price, 2),
            in_stock=True,
        )
    )
    db.session.commit()
    flash(f"{fuel_type} added at {price:,.2f} per litre.", "success")
    return redirect(url_for("station.products"))


@station_bp.post("/products/<int:product_id>/update")
@role_required(ROLE_STATION)
def update_product(product_id: int):
    station = _station_or_404()
    product = db.session.get(FuelProduct, product_id)
    if product is None or product.station_id != station.id:
        flash("Fuel type not found.", "error")
        return redirect(url_for("station.products"))

    price_raw = request.form.get("price_per_litre")
    if price_raw not in (None, ""):
        try:
            product.price_per_litre = round(
                security.validate_float(
                    price_raw, minimum=0.01, maximum=100_000, field="price"
                ),
                2,
            )
        except security.FieldError as exc:
            flash(str(exc), "error")
            return redirect(url_for("station.products"))

    # Checkbox: absent means unchecked -> out of stock.
    product.in_stock = request.form.get("in_stock") == "on"
    product.updated_at = utcnow()
    db.session.commit()
    flash(f"{product.fuel_type} updated.", "success")
    return redirect(url_for("station.products"))


@station_bp.post("/products/<int:product_id>/delete")
@role_required(ROLE_STATION)
def delete_product(product_id: int):
    station = _station_or_404()
    product = db.session.get(FuelProduct, product_id)
    if product is None or product.station_id != station.id:
        flash("Fuel type not found.", "error")
        return redirect(url_for("station.products"))

    name = product.fuel_type
    db.session.delete(product)
    db.session.commit()
    flash(f"{name} removed from the price list.", "info")
    return redirect(url_for("station.products"))


# --------------------------------------------------------------------------- #
# Station logo upload (validated: type, size, magic bytes, random filename)
# --------------------------------------------------------------------------- #
@station_bp.post("/logo")
@role_required(ROLE_STATION)
def upload_logo():
    station = _station_or_404()
    file_storage = request.files.get("logo")
    try:
        payload = uploads.validate_image_upload(
            file_storage, max_bytes=current_app.config["MAX_UPLOAD_BYTES"]
        )
    except uploads.UploadError as exc:
        current_app.logger.warning(
            "Rejected upload for station_id=%s: %s", station.id, exc
        )
        flash(str(exc), "error")
        return redirect(url_for("station.profile"))

    extension = Path(file_storage.filename).suffix.lower()
    name = uploads.safe_stored_name(extension)
    destination = Path(current_app.config["UPLOAD_FOLDER"]) / name
    destination.write_bytes(payload)

    station.logo_filename = name
    db.session.commit()
    current_app.logger.info("Logo stored for station_id=%s as %s", station.id, name)
    flash("Station logo updated.", "success")
    return redirect(url_for("station.profile"))


@station_bp.get("/logo")
@role_required(ROLE_STATION)
def logo():
    """Serve the current station logo (always as an attachment-safe image)."""
    station = _station_or_404()
    if not station.logo_filename:
        abort(404)
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"],
        station.logo_filename,
        mimetype=None,
        as_attachment=False,
        download_name=None,
    )


# --------------------------------------------------------------------------- #
# Station profile
# --------------------------------------------------------------------------- #
@station_bp.route("/profile", methods=["GET", "POST"])
@role_required(ROLE_STATION)
def profile():
    station = _station_or_404()

    if request.method == "POST":
        try:
            station.name = security.clean_text(
                request.form.get("name") or station.name, min_len=2, max_len=120
            )
            station.address = security.clean_text(
                request.form.get("address") or station.address, min_len=3, max_len=255
            )
            station.city = security.clean_text(
                request.form.get("city") or station.city, min_len=2, max_len=80
            )
            station.opening_hours = security.clean_text(
                request.form.get("opening_hours") or station.opening_hours, max_len=80
            )
            station.phone = security.validate_phone(request.form.get("phone"))
        except security.FieldError as exc:
            flash(str(exc), "error")
            return redirect(url_for("station.profile"))

        station.is_open = request.form.get("is_open") == "on"

        coords = parse_coords(request.form.get("latitude"), request.form.get("longitude"))
        if coords:
            station.latitude = coords.latitude
            station.longitude = coords.longitude
        else:
            flash("Invalid latitude/longitude — location not changed.", "warning")

        db.session.commit()
        flash("Station profile saved.", "success")
        return redirect(url_for("station.profile"))

    return render_template(
        "station/profile.html", station=station, stats=_stats(station)
    )
