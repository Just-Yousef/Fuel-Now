"""SQLAlchemy models: users, stations, mechanics, fuel orders, service requests.

Schema overview
---------------
users               one row per account, `role` drives all authorisation
fuel_products       a fuel type + price offered by a station (many per station)
stations            a fueling station, owned by a `station_operator` user
mechanics           a mechanic profile, owned by a `mechanic` user
fuel_orders         a driver ordering fuel at a station
service_requests    a driver requesting help from a mechanic

`latitude` / `longitude` live on stations, mechanics and users so that distance
maths always has a single source of truth (see `geo.py`).
"""
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

# Werkzeug's default is PBKDF2-SHA256; scrypt is memory-hard and available in the
# standard library builds we target. Chosen explicitly so the policy is auditable.
PASSWORD_HASH_METHOD = "scrypt:32768:8:1"

from extensions import db

# --------------------------------------------------------------------------- #
# Enumerated values (stored as short strings so the DB stays readable)
# --------------------------------------------------------------------------- #
ROLE_DRIVER = "driver"
ROLE_STATION = "station"
ROLE_MECHANIC = "mechanic"
ROLE_ADMIN = "admin"
ROLES = (ROLE_DRIVER, ROLE_STATION, ROLE_MECHANIC, ROLE_ADMIN)
ROLE_LABELS = {
    ROLE_DRIVER: "Driver",
    ROLE_STATION: "Fueling station operator",
    ROLE_MECHANIC: "Mechanic",
    ROLE_ADMIN: "Administrator",
}

# Moderation / approval lifecycle shared by stations, mechanics and users.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DEACTIVATED = "deactivated"
ACCOUNT_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DEACTIVATED)

# Fuel order lifecycle.
ORDER_PENDING = "pending"
ORDER_ACCEPTED = "accepted"
ORDER_FUELING = "fueling"
ORDER_COMPLETED = "completed"
ORDER_CANCELLED = "cancelled"
ORDER_STATUSES = (
    ORDER_PENDING,
    ORDER_ACCEPTED,
    ORDER_FUELING,
    ORDER_COMPLETED,
    ORDER_CANCELLED,
)
ORDER_TRANSITIONS = {
    ORDER_PENDING: (ORDER_ACCEPTED, ORDER_CANCELLED),
    ORDER_ACCEPTED: (ORDER_FUELING, ORDER_CANCELLED),
    ORDER_FUELING: (ORDER_COMPLETED, ORDER_CANCELLED),
    ORDER_COMPLETED: (),
    ORDER_CANCELLED: (),
}
ORDER_ACTIVE_STATUSES = (ORDER_PENDING, ORDER_ACCEPTED, ORDER_FUELING)

# Service request lifecycle.
REQUEST_PENDING = "pending"
REQUEST_ACCEPTED = "accepted"
REQUEST_EN_ROUTE = "en_route"
REQUEST_IN_PROGRESS = "in_progress"
REQUEST_COMPLETED = "completed"
REQUEST_CANCELLED = "cancelled"
REQUEST_STATUSES = (
    REQUEST_PENDING,
    REQUEST_ACCEPTED,
    REQUEST_EN_ROUTE,
    REQUEST_IN_PROGRESS,
    REQUEST_COMPLETED,
    REQUEST_CANCELLED,
)
REQUEST_TRANSITIONS = {
    REQUEST_PENDING: (REQUEST_ACCEPTED, REQUEST_CANCELLED),
    REQUEST_ACCEPTED: (REQUEST_EN_ROUTE, REQUEST_CANCELLED),
    REQUEST_EN_ROUTE: (REQUEST_IN_PROGRESS, REQUEST_CANCELLED),
    REQUEST_IN_PROGRESS: (REQUEST_COMPLETED, REQUEST_CANCELLED),
    REQUEST_COMPLETED: (),
    REQUEST_CANCELLED: (),
}
REQUEST_ACTIVE_STATUSES = (
    REQUEST_PENDING,
    REQUEST_ACCEPTED,
    REQUEST_EN_ROUTE,
    REQUEST_IN_PROGRESS,
)

STATUS_LABELS = {
    ORDER_PENDING: "Pending",
    ORDER_ACCEPTED: "Accepted",
    ORDER_FUELING: "Fueling",
    ORDER_COMPLETED: "Completed",
    ORDER_CANCELLED: "Cancelled",
    REQUEST_EN_ROUTE: "On the way",
    REQUEST_IN_PROGRESS: "In progress",
}

# Statuses that make a badge red / green in the templates.
BAD_STATUSES = {ORDER_CANCELLED, REQUEST_CANCELLED, STATUS_DEACTIVATED}
GOOD_STATUSES = {ORDER_COMPLETED, REQUEST_COMPLETED, STATUS_APPROVED}


def utcnow() -> datetime:
    """Timezone-aware UTC now (SQLite stores it naive, so we normalise)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Admin audit history
# --------------------------------------------------------------------------- #
class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    target_type = db.Column(db.String(40), nullable=False, index=True)
    target_id = db.Column(db.Integer, nullable=False, index=True)
    old_status = db.Column(db.String(20))
    new_status = db.Column(db.String(20), nullable=False)
    details = db.Column(db.String(255))
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    admin = db.relationship("User", foreign_keys=[admin_id])


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(40))
    role = db.Column(db.String(20), nullable=False, default=ROLE_DRIVER, index=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_APPROVED)
    is_active_flag = db.Column("is_active", db.Boolean, nullable=False, default=True)

    # Email verification. Drivers must verify before they can place orders or
    # request a mechanic; station/mechanic accounts are verified by an admin.
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    email_verified_at = db.Column(db.DateTime)

    # Optional TOTP two-factor authentication for administrators.
    admin_totp_secret = db.Column(db.String(64))
    admin_totp_enabled = db.Column(db.Boolean, nullable=False, default=False)

    # Last known driver position (used to pre-fill the location prompt).
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    location_updated_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    station = db.relationship(
        "Station", back_populates="owner", uselist=False, cascade="all, delete-orphan"
    )
    mechanic = db.relationship(
        "Mechanic", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    notifications = db.relationship(
        "Notification", back_populates="recipient", cascade="all, delete-orphan", order_by="Notification.created_at.desc()"
    )

    # Flask-Login override: a deactivated account is simply not "active".
    @property
    def is_active(self):  # noqa: D401 - Flask-Login protocol
        return bool(self.is_active_flag) and self.status != STATUS_DEACTIVATED

    @property
    def is_superuser(self) -> bool:
        return self.role == ROLE_ADMIN

    def set_password(self, raw_password: str) -> None:
        """Hash with scrypt. The plaintext is never stored or logged."""
        self.password_hash = generate_password_hash(
            raw_password, method=PASSWORD_HASH_METHOD
        )

    def check_password(self, raw_password: str) -> bool:
        """Constant-time verification via Werkzeug."""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw_password)

    def needs_rehash(self) -> bool:
        """True when the stored hash uses an older/weaker scheme."""
        from werkzeug.security import _hash_internal  # noqa: PLC0415

        try:
            method = self.password_hash.split("$", 1)[0]
        except AttributeError:
            return True
        return method != PASSWORD_HASH_METHOD.split(":", 1)[0]

    def mark_email_verified(self) -> None:
        self.email_verified = True
        self.email_verified_at = utcnow()

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def home_endpoint(self) -> str:
        """Landing page for this user's role (also used after login)."""
        return {
            ROLE_DRIVER: "driver.dashboard",
            ROLE_STATION: "station.dashboard",
            ROLE_MECHANIC: "mechanic.dashboard",
            ROLE_ADMIN: "admin.dashboard",
        }.get(self.role, "auth.login")

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email} ({self.role})>"


# --------------------------------------------------------------------------- #
# Stations
# --------------------------------------------------------------------------- #
class Station(db.Model):
    __tablename__ = "stations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    owner_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True
    )
    address = db.Column(db.String(255), nullable=False)
    city = db.Column(db.String(80), nullable=False)
    phone = db.Column(db.String(40))
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)
    opening_hours = db.Column(db.String(80), default="06:00 - 22:00")
    # Server-generated filename only (see uploads.safe_stored_name).
    logo_filename = db.Column(db.String(80))
    is_open = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    owner = db.relationship("User", back_populates="station")
    products = db.relationship(
        "FuelProduct",
        back_populates="station",
        cascade="all, delete-orphan",
        order_by="FuelProduct.fuel_type",
    )
    orders = db.relationship(
        "FuelOrder", back_populates="station", cascade="all, delete-orphan"
    )
    reviews = db.relationship(
        "StationReview", back_populates="station", cascade="all, delete-orphan"
    )

    @property
    def average_rating(self) -> float | None:
        if not self.reviews:
            return None
        return round(sum(review.rating for review in self.reviews) / len(self.reviews), 1)

    @property
    def is_operational(self) -> bool:
        return self.status == STATUS_APPROVED and self.is_open

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Station {self.name}>"


class FuelProduct(db.Model):
    """One fuel type offered by one station, with its own price per litre."""

    __tablename__ = "fuel_products"
    __table_args__ = (
        db.UniqueConstraint("station_id", "fuel_type", name="uq_station_fuel_type"),
    )

    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(
        db.Integer, db.ForeignKey("stations.id"), nullable=False, index=True
    )
    fuel_type = db.Column(db.String(40), nullable=False)
    price_per_litre = db.Column(db.Float, nullable=False)
    in_stock = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    station = db.relationship("Station", back_populates="products")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FuelProduct {self.fuel_type} @ {self.price_per_litre}>"


# --------------------------------------------------------------------------- #
# Mechanics
# --------------------------------------------------------------------------- #
class Mechanic(db.Model):
    __tablename__ = "mechanics"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True
    )
    business_name = db.Column(db.String(120), nullable=False)
    specialties = db.Column(db.String(255), default="General repairs")
    address = db.Column(db.String(255))
    city = db.Column(db.String(80), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    service_radius_km = db.Column(db.Float, nullable=False, default=15.0)
    available = db.Column(db.Boolean, nullable=False, default=True)
    hourly_rate = db.Column(db.Float)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="mechanic")
    requests = db.relationship(
        "ServiceRequest", back_populates="mechanic", cascade="all, delete-orphan"
    )
    reviews = db.relationship(
        "MechanicReview", back_populates="mechanic", cascade="all, delete-orphan"
    )

    @property
    def average_rating(self) -> float | None:
        if not self.reviews:
            return None
        return round(sum(review.rating for review in self.reviews) / len(self.reviews), 1)

    @property
    def is_listed(self) -> bool:
        """Visible + acceptable by drivers."""
        return self.status == STATUS_APPROVED and self.available

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Mechanic {self.business_name}>"


# --------------------------------------------------------------------------- #
# Fuel orders
# --------------------------------------------------------------------------- #
class FuelOrder(db.Model):
    __tablename__ = "fuel_orders"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True, nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    station_id = db.Column(db.Integer, db.ForeignKey("stations.id"), nullable=False, index=True)
    fuel_type = db.Column(db.String(40), nullable=False)
    quantity_litres = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    total_price = db.Column(db.Float, nullable=False)
    vehicle_plate = db.Column(db.String(20))
    delivery_note = db.Column(db.String(255))
    # Snapshot of where the driver was when the order was placed.
    driver_latitude = db.Column(db.Float)
    driver_longitude = db.Column(db.Float)
    distance_km = db.Column(db.Float)
    status = db.Column(db.String(20), nullable=False, default=ORDER_PENDING, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    driver = db.relationship("User", foreign_keys=[driver_id])
    station = db.relationship("Station", back_populates="orders")
    review = db.relationship("StationReview", back_populates="order", uselist=False, cascade="all, delete-orphan")

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.replace("_", " ").title())

    @property
    def can_cancel(self) -> bool:
        return self.status in (ORDER_PENDING, ORDER_ACCEPTED)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<FuelOrder {self.reference} {self.status}>"


# --------------------------------------------------------------------------- #
# Mechanic service requests
# --------------------------------------------------------------------------- #
class ServiceRequest(db.Model):
    __tablename__ = "service_requests"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True, nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    mechanic_id = db.Column(db.Integer, db.ForeignKey("mechanics.id"), nullable=False, index=True)
    issue = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    vehicle = db.Column(db.String(120))
    # Where the driver needs help (falls back to the mechanic's area check).
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    distance_km = db.Column(db.Float)
    status = db.Column(db.String(20), nullable=False, default=REQUEST_PENDING, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    driver = db.relationship("User", foreign_keys=[driver_id])
    mechanic = db.relationship("Mechanic", back_populates="requests")
    review = db.relationship("MechanicReview", back_populates="request", uselist=False, cascade="all, delete-orphan")

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.replace("_", " ").title())

    @property
    def can_cancel(self) -> bool:
        return self.status in (REQUEST_PENDING, REQUEST_ACCEPTED)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ServiceRequest {self.reference} {self.status}>"


# --------------------------------------------------------------------------- #
# Driver reviews
# --------------------------------------------------------------------------- #
class StationReview(db.Model):
    __tablename__ = "station_reviews"
    __table_args__ = (db.UniqueConstraint("order_id", name="uq_station_review_order"),)

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("fuel_orders.id"), nullable=False, unique=True, index=True)
    station_id = db.Column(db.Integer, db.ForeignKey("stations.id"), nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    order = db.relationship("FuelOrder", back_populates="review")
    station = db.relationship("Station", back_populates="reviews")
    driver = db.relationship("User", foreign_keys=[driver_id])


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    link = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    recipient = db.relationship("User", back_populates="notifications")


class MechanicReview(db.Model):
    __tablename__ = "mechanic_reviews"
    __table_args__ = (db.UniqueConstraint("request_id", name="uq_mechanic_review_request"),)

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("service_requests.id"), nullable=False, unique=True, index=True)
    mechanic_id = db.Column(db.Integer, db.ForeignKey("mechanics.id"), nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    request = db.relationship("ServiceRequest", back_populates="review")
    mechanic = db.relationship("Mechanic", back_populates="reviews")
    driver = db.relationship("User", foreign_keys=[driver_id])
