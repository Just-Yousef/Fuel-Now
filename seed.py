"""Demo data for Veyro.

Coordinates are real-ish points around Abuja (FCT, Nigeria) so that distance
sorting produces sensible results. One station is deliberately placed in Lagos
(~500 km away) to prove the radius filter works.

Run:  flask --app app seed-db --reset
"""
from __future__ import annotations

from datetime import timedelta

from extensions import db
from models import (
    ORDER_ACCEPTED,
    ORDER_COMPLETED,
    ORDER_FUELING,
    ORDER_PENDING,
    REQUEST_ACCEPTED,
    REQUEST_COMPLETED,
    REQUEST_PENDING,
    ROLE_ADMIN,
    ROLE_DRIVER,
    ROLE_MECHANIC,
    ROLE_STATION,
    STATUS_APPROVED,
    STATUS_PENDING,
    FuelOrder,
    FuelProduct,
    Mechanic,
    ServiceRequest,
    Station,
    User,
    utcnow,
)
from blueprints.public import _reference

DEMO_PASSWORD = "FuelNow!2026"

# (name, address, city, lat, lon, status, products)
STATION_SEEDS = [
    (
        "Maitama Total Energies",
        "Plot 1234 Aguiyi Ironsi Street, Maitama",
        "Abuja",
        9.0778,
        7.4922,
        STATUS_APPROVED,
        [("Petrol (PMS)", 897.50), ("Diesel (AGO)", 1140.00), ("CNG", 230.00)],
    ),
    (
        "Wuse II NNPC Retail",
        "Ademola Adetokunbo Crescent, Wuse II",
        "Abuja",
        9.0772,
        7.4723,
        STATUS_APPROVED,
        [("Petrol (PMS)", 890.00), ("Diesel (AGO)", 1125.50)],
    ),
    (
        "Garki Mobile Filling Station",
        "Ahmadu Bello Way, Garki Area 11",
        "Abuja",
        9.0360,
        7.4890,
        STATUS_APPROVED,
        [("Petrol (PMS)", 902.00), ("Kerosene (DPK)", 1250.00)],
    ),
    (
        "Lugbe Airport Road Fuel Point",
        "Airport Road, Lugbe",
        "Abuja",
        8.9667,
        7.3800,
        STATUS_APPROVED,
        [("Petrol (PMS)", 899.00), ("Diesel (AGO)", 1130.00), ("LPG", 1300.00)],
    ),
    (
        "Kubwa Sunrise Energy",
        "Gado Nasko Road, Kubwa",
        "Abuja",
        9.1560,
        7.3270,
        STATUS_PENDING,  # awaiting admin approval -> invisible to drivers
        [("Petrol (PMS)", 895.00)],
    ),
    (
        "Lekki Phase 1 Ardova",
        "Admiralty Way, Lekki Phase 1",
        "Lagos",
        6.4474,
        3.4736,
        STATUS_APPROVED,
        [("Petrol (PMS)", 910.00), ("Diesel (AGO)", 1150.00)],
    ),
]

MECHANIC_SEEDS = [
    (
        "AutoFix Mobile Mechanics",
        "Engine, brakes, electrical",
        "Wuse II, Abuja",
        "Abuja",
        9.0700,
        7.4800,
        15.0,
        True,
        8500.0,
        STATUS_APPROVED,
    ),
    (
        "Karu Roadside Rescue",
        "Tyres, battery, towing",
        "Karu Site, Abuja",
        "Abuja",
        9.0200,
        7.5600,
        20.0,
        True,
        6000.0,
        STATUS_APPROVED,
    ),
    (
        "Gwarinpa Diagnostic Garage",
        "Diagnostics, transmission, AC",
        "3rd Avenue, Gwarinpa",
        "Abuja",
        9.1070,
        7.4040,
        12.0,
        True,
        12000.0,
        STATUS_APPROVED,
    ),
    (
        "Asokoro Diesel Specialists",
        "Diesel engines, fuel systems",
        "Yakubu Gowon Crescent, Asokoro",
        "Abuja",
        9.0420,
        7.5290,
        10.0,
        False,  # approved but unavailable -> hidden from "available" searches
        15000.0,
        STATUS_APPROVED,
    ),
    (
        "Lugbe Auto Clinic",
        "General repairs, panel beating",
        "Airport Road, Lugbe",
        "Abuja",
        8.9700,
        7.3900,
        18.0,
        True,
        7000.0,
        STATUS_PENDING,  # awaiting admin approval
    ),
]

DRIVER_SEEDS = [
    ("Amina Bello", "amina@example.com", 9.0579, 7.4951),
    ("Chidi Okafor", "chidi@example.com", 9.0900, 7.4300),
]


def seed_database() -> list[str]:
    log: list[str] = []
    now = utcnow()

    # ---------------- admin ----------------
    admin = User(
        email="admin@fuelnow.ng",
        full_name="Platform Admin",
        phone="+2348000000000",
        role=ROLE_ADMIN,
        status=STATUS_APPROVED,
        # Demo account: pre-verified, otherwise the admin is confined to the
        # "verify your email" screen and can never reach /admin/.
        email_verified=True,
        email_verified_at=now,
    )
    admin.set_password(DEMO_PASSWORD)
    db.session.add(admin)
    log.append(f"admin        admin@fuelnow.ng / {DEMO_PASSWORD}")

    # ---------------- drivers ----------------
    drivers = []
    for name, email, lat, lon in DRIVER_SEEDS:
        driver = User(
            email=email,
            full_name=name,
            phone="+2348011111111",
            role=ROLE_DRIVER,
            status=STATUS_APPROVED,
            email_verified=True,
            latitude=lat,
            longitude=lon,
            location_updated_at=now,
        )
        driver.set_password(DEMO_PASSWORD)
        db.session.add(driver)
        drivers.append(driver)
        log.append(f"driver       {email} / {DEMO_PASSWORD}")

    # ---------------- stations ----------------
    stations = []
    for idx, (name, address, city, lat, lon, status, products) in enumerate(
        STATION_SEEDS, start=1
    ):
        email = f"station{idx}@fuelnow.ng"
        owner = User(
            email=email,
            full_name=f"{name} Manager",
            phone="+2348022222222",
            role=ROLE_STATION,
            status=status,
            email_verified=True,
            latitude=lat,
            longitude=lon,
            location_updated_at=now,
        )
        owner.set_password(DEMO_PASSWORD)
        db.session.add(owner)
        db.session.flush()

        station = Station(
            owner_id=owner.id,
            name=name,
            address=address,
            city=city,
            phone="+2348022222222",
            latitude=lat,
            longitude=lon,
            status=status,
            opening_hours="06:00 - 22:00",
            is_open=True,
        )
        db.session.add(station)
        db.session.flush()
        stations.append(station)

        for fuel_type, price in products:
            db.session.add(
                FuelProduct(
                    station_id=station.id,
                    fuel_type=fuel_type,
                    price_per_litre=price,
                    in_stock=True,
                )
            )
        log.append(f"station      {email} / {DEMO_PASSWORD}  ({name})")

    # ---------------- mechanics ----------------
    mechanics = []
    for idx, (
        business,
        specialties,
        address,
        city,
        lat,
        lon,
        radius,
        available,
        rate,
        status,
    ) in enumerate(MECHANIC_SEEDS, start=1):
        email = f"mechanic{idx}@fuelnow.ng"
        user = User(
            email=email,
            full_name=f"{business} (Owner)",
            phone="+2348033333333",
            role=ROLE_MECHANIC,
            status=status,
            email_verified=True,
            latitude=lat,
            longitude=lon,
            location_updated_at=now,
        )
        user.set_password(DEMO_PASSWORD)
        db.session.add(user)
        db.session.flush()

        mechanic = Mechanic(
            user_id=user.id,
            business_name=business,
            specialties=specialties,
            address=address,
            city=city,
            latitude=lat,
            longitude=lon,
            service_radius_km=radius,
            available=available,
            hourly_rate=rate,
            status=status,
        )
        db.session.add(mechanic)
        mechanics.append(mechanic)
        log.append(f"mechanic     {email} / {DEMO_PASSWORD}  ({business})")

    db.session.flush()

    # ---------------- fuel orders ----------------
    order_specs = [
        (drivers[0], stations[0], "Petrol (PMS)", 40.0, ORDER_PENDING, 5),
        (drivers[0], stations[1], "Diesel (AGO)", 60.0, ORDER_ACCEPTED, 90),
        (drivers[0], stations[2], "Petrol (PMS)", 25.0, ORDER_COMPLETED, 60 * 26),
        (drivers[1], stations[1], "Petrol (PMS)", 30.0, ORDER_FUELING, 20),
        (drivers[1], stations[3], "Diesel (AGO)", 80.0, ORDER_COMPLETED, 60 * 50),
        (drivers[1], stations[0], "CNG", 15.0, ORDER_COMPLETED, 60 * 72),
    ]
    for driver, station, fuel_type, litres, status, minutes_ago in order_specs:
        product = FuelProduct.query.filter_by(
            station_id=station.id, fuel_type=fuel_type
        ).first()
        unit_price = product.price_per_litre if product else 900.0
        created = now - timedelta(minutes=minutes_ago)
        db.session.add(
            FuelOrder(
                reference=_reference("FO"),
                driver_id=driver.id,
                station_id=station.id,
                fuel_type=fuel_type,
                quantity_litres=litres,
                unit_price=unit_price,
                total_price=round(litres * unit_price, 2),
                vehicle_plate="ABC-123-XY",
                delivery_note="Please fill the tank, no jerrycan.",
                driver_latitude=driver.latitude,
                driver_longitude=driver.longitude,
                status=status,
                created_at=created,
                updated_at=created,
            )
        )

    # ---------------- service requests ----------------
    request_specs = [
        (drivers[0], mechanics[0], "Engine won't start", REQUEST_PENDING, 10),
        (drivers[0], mechanics[2], "Flat tyre", REQUEST_COMPLETED, 60 * 30),
        (drivers[1], mechanics[1], "Battery / electrical", REQUEST_ACCEPTED, 25),
    ]
    for driver, mechanic, issue, status, minutes_ago in request_specs:
        created = now - timedelta(minutes=minutes_ago)
        db.session.add(
            ServiceRequest(
                reference=_reference("SR"),
                driver_id=driver.id,
                mechanic_id=mechanic.id,
                issue=issue,
                description="Vehicle is parked on the roadside, hazard lights on.",
                vehicle="Toyota Corolla 2012",
                latitude=driver.latitude,
                longitude=driver.longitude,
                status=status,
                created_at=created,
                updated_at=created,
            )
        )

    db.session.commit()
    log.append("")
    log.append(
        f"Seeded {len(stations)} stations, {len(mechanics)} mechanics, "
        f"{len(order_specs)} orders, {len(request_specs)} service requests."
    )
    log.append("All demo accounts use the password: " + DEMO_PASSWORD)
    return log
