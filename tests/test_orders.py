"""Core flow: a driver places a fuel order and a station moves it along."""
from extensions import db
from models import FuelOrder, FuelProduct, StationReview
from tests.conftest import (
    DRIVER_LAT,
    DRIVER_LNG,
    login,
    make_station,
)


def place_order(client, station_id, fuel_type="Petrol (PMS)", litres="30", **extra):
    data = {
        "station_id": station_id,
        "fuel_type": fuel_type,
        "quantity_litres": litres,
        "lat": str(DRIVER_LAT),
        "lng": str(DRIVER_LNG),
    }
    data.update(extra)
    return client.post("/driver/orders", data=data, follow_redirects=True)


def test_place_order_creates_record_with_correct_total(app, client, driver):
    with app.app_context():
        station = make_station(products=(("Petrol (PMS)", 900.0),))
        station_id = station.id

    login(client, driver.email)
    response = place_order(client, station_id, litres="30")

    assert response.status_code == 200
    with app.app_context():
        order = FuelOrder.query.one()
        assert order.status == "pending"
        assert order.driver_id == driver.id
        assert order.station_id == station_id
        assert order.fuel_type == "Petrol (PMS)"
        assert order.quantity_litres == 30.0
        assert order.unit_price == 900.0
        assert order.total_price == 27_000.0
        assert order.reference.startswith("FO-")
        # The driver's position is snapshotted and the distance recorded.
        assert order.driver_latitude is not None
        assert order.distance_km is not None and order.distance_km < 5
        assert order.reference in order.__repr__()


def test_order_appears_in_station_queue_and_advances_through_statuses(
    app, client, driver
):
    with app.app_context():
        station = make_station(owner_email="op@example.com")
        station_id = station.id

    login(client, driver.email)
    place_order(client, station_id, litres="45")
    with app.app_context():
        order_id = FuelOrder.query.one().id
        reference = FuelOrder.query.one().reference
    client.post("/logout")

    assert login(client, "op@example.com").status_code == 200
    queue = client.get("/station/orders").get_data(as_text=True)
    assert reference in queue

    # pending -> accepted -> fueling -> completed
    for status in ("accepted", "fueling", "completed"):
        response = client.post(
            f"/station/orders/{order_id}/status",
            data={"status": status},
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            assert db.session.get(FuelOrder, order_id).status == status

    # A completed order is terminal.
    client.post(
        f"/station/orders/{order_id}/status", data={"status": "accepted"}, follow_redirects=True
    )
    with app.app_context():
        assert db.session.get(FuelOrder, order_id).status == "completed"


def test_cannot_skip_order_status(app, client, driver):
    with app.app_context():
        station = make_station(owner_email="op2@example.com")
        station_id = station.id

    login(client, driver.email)
    place_order(client, station_id)
    with app.app_context():
        order_id = FuelOrder.query.one().id
    client.post("/logout")

    login(client, "op2@example.com")
    response = client.post(
        f"/station/orders/{order_id}/status", data={"status": "completed"}, follow_redirects=True
    )
    assert b"Cannot move" in response.data
    with app.app_context():
        assert db.session.get(FuelOrder, order_id).status == "pending"


def test_order_validation_quantity_and_fuel_type(app, client, driver):
    with app.app_context():
        station = make_station(products=(("Petrol (PMS)", 900.0),))
        station_id = station.id

    login(client, driver.email)

    too_many = place_order(client, station_id, litres="900")
    assert b"between" in too_many.data

    zero = place_order(client, station_id, litres="0")
    assert b"between" in zero.data

    wrong_fuel = place_order(client, station_id, fuel_type="CNG")
    assert b"does not sell" in wrong_fuel.data

    with app.app_context():
        assert FuelOrder.query.count() == 0


def test_order_rejected_when_fuel_out_of_stock(app, client, driver):
    with app.app_context():
        station = make_station(products=(("Diesel (AGO)", 1100.0),))
        product = FuelProduct.query.filter_by(station_id=station.id).one()
        product.in_stock = False
        db.session.commit()
        station_id = station.id

    login(client, driver.email)
    response = place_order(client, station_id, fuel_type="Diesel (AGO)")
    assert b"out of stock" in response.data
    with app.app_context():
        assert FuelOrder.query.count() == 0


def test_order_rejected_for_unapproved_station(app, client, driver):
    with app.app_context():
        station = make_station(status="pending", owner_email="pending@example.com")
        station_id = station.id

    login(client, driver.email)
    response = place_order(client, station_id)
    assert b"not available for orders" in response.data
    with app.app_context():
        assert FuelOrder.query.count() == 0


def test_completed_order_can_be_reviewed_once(app, client, driver):
    with app.app_context():
        station = make_station(owner_email="review-station@example.com")
        station_id = station.id

    login(client, driver.email)
    assert client.get("/driver/").status_code == 200
    place_order(client, station_id)

    with app.app_context():
        order = FuelOrder.query.one()
        order_id = order.id

    client.post("/logout")
    login(client, "review-station@example.com")
    for status in ("accepted", "fueling", "completed"):
        response = client.post(
            f"/station/orders/{order_id}/status",
            data={"status": status},
            follow_redirects=True,
        )
        assert response.status_code == 200

    client.post("/logout")
    login(client, driver.email)

    first = client.post(
        f"/driver/orders/{order_id}/review",
        data={"rating": "5", "comment": "Fast and professional"},
    )
    assert first.status_code == 302
    with client.session_transaction() as browser_session:
        flashes = browser_session.get("_flashes", [])
        assert ("success", "Thank you. Your station review was submitted.") in flashes

    duplicate = client.post(
        f"/driver/orders/{order_id}/review",
        data={"rating": "1", "comment": "Changed my mind"},
    )
    assert duplicate.status_code == 302

    with app.app_context():
        reviews = StationReview.query.filter_by(order_id=order_id).all()
        assert len(reviews) == 1
        assert reviews[0].rating == 5
        assert reviews[0].comment == "Fast and professional"


def test_driver_can_cancel_own_pending_order(app, client, driver):
    with app.app_context():
        station = make_station()
        station_id = station.id

    login(client, driver.email)
    place_order(client, station_id)
    with app.app_context():
        order_id = FuelOrder.query.one().id

    client.post(f"/driver/orders/{order_id}/cancel", follow_redirects=True)
    with app.app_context():
        assert db.session.get(FuelOrder, order_id).status == "cancelled"


def test_driver_cannot_cancel_someone_elses_order(app, client, driver):
    from tests.conftest import make_user

    with app.app_context():
        station = make_station()
        other = make_user(email="other@example.com", full_name="Other Driver")
        order = FuelOrder(
            reference="FO-OWNED",
            driver_id=other.id,
            station_id=station.id,
            fuel_type="Petrol (PMS)",
            quantity_litres=10,
            unit_price=900,
            total_price=9000,
            status="pending",
        )
        db.session.add(order)
        db.session.commit()
        order_id = order.id

    login(client, driver.email)
    response = client.post(f"/driver/orders/{order_id}/cancel", follow_redirects=True)
    assert b"not found" in response.data.lower()
    with app.app_context():
        assert db.session.get(FuelOrder, order_id).status == "pending"


def test_station_price_management(app, client):
    with app.app_context():
        station = make_station(owner_email="mgr@example.com")
        station_id = station.id

    login(client, "mgr@example.com")

    # add
    client.post(
        "/station/products",
        data={"fuel_type": "CNG", "price_per_litre": "250"},
        follow_redirects=True,
    )
    with app.app_context():
        product = FuelProduct.query.filter_by(station_id=station_id, fuel_type="CNG").one()
        product_id = product.id
        assert product.price_per_litre == 250.0

    # update price + mark out of stock (checkbox omitted)
    client.post(
        f"/station/products/{product_id}/update",
        data={"price_per_litre": "265.50"},
        follow_redirects=True,
    )
    with app.app_context():
        product = db.session.get(FuelProduct, product_id)
        assert product.price_per_litre == 265.50
        assert product.in_stock is False

    # delete
    client.post(f"/station/products/{product_id}/delete", follow_redirects=True)
    with app.app_context():
        assert db.session.get(FuelProduct, product_id) is None


def test_station_cannot_edit_another_stations_product(app, client):
    with app.app_context():
        mine = make_station(owner_email="mine@example.com")
        theirs = make_station(name="Theirs", owner_email="theirs@example.com")
        product = FuelProduct.query.filter_by(station_id=theirs.id).one()
        product_id = product.id

    login(client, "mine@example.com")
    response = client.post(
        f"/station/products/{product_id}/update",
        data={"price_per_litre": "1.00"},
        follow_redirects=True,
    )
    assert b"not found" in response.data.lower()
    with app.app_context():
        assert db.session.get(FuelProduct, product_id).price_per_litre == 900.0
