"""Signed webhook endpoint (e.g. a payment or dispatch provider).

Security model
--------------
* The request body is authenticated with an **HMAC-SHA256 signature** using a
  shared secret (`WEBHOOK_SECRET`), delivered in `X-Signature` as `sha256=<hex>`.
* Comparison uses `hmac.compare_digest` (constant time) so signatures cannot be
  discovered by timing.
* A **timestamp header** (`X-Timestamp`) is required and must be within
  `WEBHOOK_TOLERANCE_SECONDS` (default 300) to stop replay attacks.
* The signature covers `timestamp + "." + raw_body`, so a captured body cannot be
  re-signed with a different timestamp.
* The parsed JSON is validated field-by-field; nothing from the payload is ever
  interpolated into SQL or HTML.
* CSRF does not apply (there is no browser session); the signature replaces it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from flask import Blueprint, current_app, jsonify, request

from extensions import db
from models import ORDER_STATUSES

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@webhooks_bp.post("/fuel-order")
def receive():
    secret = current_app.config.get("WEBHOOK_SECRET")
    if not secret:
        # Fail closed: an unconfigured webhook must never accept unsigned input.
        current_app.logger.error("Webhook rejected: WEBHOOK_SECRET is not configured")
        return jsonify(ok=False, error="webhook not configured"), 503

    raw = request.get_data() or b""
    signature_header = request.headers.get("X-Signature", "")
    timestamp = request.headers.get("X-Timestamp", "")

    if not signature_header.startswith("sha256=") or not timestamp.isdigit():
        current_app.logger.warning("Webhook rejected: malformed signature headers")
        return jsonify(ok=False, error="missing or malformed signature"), 401

    tolerance = current_app.config.get("WEBHOOK_TOLERANCE_SECONDS", 300)
    if abs(time.time() - int(timestamp)) > tolerance:
        current_app.logger.warning("Webhook rejected: timestamp outside tolerance")
        return jsonify(ok=False, error="stale timestamp"), 401

    expected = _signature(secret, timestamp, raw)
    provided = signature_header.split("=", 1)[1]
    if not hmac.compare_digest(expected, provided):
        current_app.logger.warning("Webhook rejected: signature mismatch")
        return jsonify(ok=False, error="invalid signature"), 401

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return jsonify(ok=False, error="invalid JSON"), 400
    if not isinstance(payload, dict):
        return jsonify(ok=False, error="payload must be an object"), 400

    reference = str(payload.get("reference", ""))[:20]
    status = str(payload.get("status", ""))
    if not reference:
        return jsonify(ok=False, error="reference is required"), 400
    if status not in ORDER_STATUSES:
        return jsonify(ok=False, error="unknown status"), 400

    from models import FuelOrder

    order = FuelOrder.query.filter_by(reference=reference).first()
    if order is None:
        return jsonify(ok=False, error="order not found"), 404

    order.status = status
    db.session.commit()
    current_app.logger.info(
        "Webhook applied: order=%s status=%s", reference, status
    )
    return jsonify(ok=True, reference=reference, status=status)
