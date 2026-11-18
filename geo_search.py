"""Place search (geocoding) for the "where are you?" autocomplete.

Why this module exists
----------------------
The address box needs type-ahead suggestions, so the browser cannot call a
geocoder directly on every keystroke:

* Nominatim's public server **forbids autocomplete outright** (its usage
  policy bans "auto-complete search") and caps traffic at 1 req/sec.
* Photon (photon.komoot.io, komoot) is built for search-as-you-type, needs
  no API key, and returns OpenStreetMap data. It is the right free tool.

Even so, requests go through *this* server rather than straight from the
browser, which buys us:

* **Rate limiting per session** (see ``_throttle``) so typing cannot hammer a
  free public endpoint we do not own.
* **A short server-side cache** so "mait", "maita", "maitam" cost one lookup.
* **CSP stays tight** — ``connect-src 'self'`` is unchanged, no third-party
  origin is added to the policy.
* **A provider swap is one constant**, not a template change.

Results are trimmed to the fields the UI actually shows, so the response
stays small even for a broad query.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterable

import requests

DEFAULT_PROVIDER = "https://photon.komoot.io/api/"
DEFAULT_REVERSE_PROVIDER = "https://photon.komoot.io/reverse"
# Photon asks clients to identify themselves; also gives them someone to email
# if traffic ever becomes a problem.
USER_AGENT = "Veyro/1.0 (demo app; +https://github.com/)"
REQUEST_TIMEOUT = 4  # seconds; the UI degrades to "no suggestions"

MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 120
MAX_RESULTS = 6

# Result biasing. Photon sorts by distance from the middle of this box, which
# matters: a search for "Maitama" otherwise returns towns in Niger and Chad
# before the Abuja district the user almost certainly means.
# Format: "min_lon,min_lat,max_lon,max_lat" — but ONLY as a *bias*, not a
# filter: if a biased search finds nothing we retry unbounded (see search()).
DEFAULT_BBOX = "2.6,4.2,14.7,13.9"  # Nigeria

# --- per-session throttle -------------------------------------------------- #
# One lookup per user action is plenty. This is deliberately generous enough
# for real typing but blocks a script from looping the endpoint.
# Budget sized for human typing (the UI debounces at ~320 ms, so a fast
# typist peaks around 3 lookups/second) but still stops a script.
_THROTTLE_WINDOW = 10.0  # seconds
_THROTTLE_MAX = 25       # lookups allowed inside the window
_throttle_lock = threading.Lock()
_throttle_state: dict[str, list[float]] = {}

# --- tiny TTL cache -------------------------------------------------------- #
_CACHE_TTL = 120.0
_CACHE_MAX = 512
_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class Place:
    """One suggestion, already flattened for the browser."""

    label: str
    detail: str
    latitude: float
    longitude: float

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "detail": self.detail,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }


def _throttle(key: str) -> bool:
    """True if this session may make a lookup now."""
    now = time.monotonic()
    with _throttle_lock:
        stamps = [t for t in _throttle_state.get(key, []) if now - t < _THROTTLE_WINDOW]
        if len(stamps) >= _THROTTLE_MAX:
            _throttle_state[key] = stamps
            return False
        stamps.append(now)
        _throttle_state[key] = stamps
        # Keep the dict from growing without bound.
        if len(_throttle_state) > 2048:
            for stale in [k for k, v in _throttle_state.items() if not v or now - v[-1] > 60]:
                _throttle_state.pop(stale, None)
        return True


def _cache_get(key: str) -> list[dict] | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        stamped, value = hit
        if now - stamped > _CACHE_TTL:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: list[dict]) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = (time.monotonic(), value)


def _label_and_detail(props: dict) -> tuple[str, str]:
    """Build a short name plus a disambiguating line from Photon properties."""
    name = props.get("name") or props.get("street") or "Unnamed place"
    if props.get("housenumber"):
        name = f"{props['housenumber']} {name}"

    parts = [
        props.get("district"),
        props.get("city") or props.get("county"),
        props.get("state"),
        props.get("country"),
    ]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen and part != name:
            seen.append(part)
    return name, ", ".join(seen)


def _as_float(value) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _fetch(text: str, provider: str, bbox: str | None) -> list[dict]:
    """One provider call. Returns [] on any failure rather than raising."""
    params = {"q": text, "limit": MAX_RESULTS, "lang": "en"}
    if bbox:
        params["bbox"] = bbox
    try:
        response = requests.get(
            provider,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []

    results: list[dict] = []
    for feature in payload.get("features", []) or []:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if len(coords) < 2:
            continue
        longitude = _as_float(coords[0])   # GeoJSON is [lng, lat] — easy to flip
        latitude = _as_float(coords[1])
        if latitude is None or longitude is None:
            continue
        label, detail = _label_and_detail(props)
        results.append(
            Place(label=label, detail=detail, latitude=latitude, longitude=longitude).as_dict()
        )
        if len(results) >= MAX_RESULTS:
            break
    return results


def search(
    query: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    bbox: str | None = DEFAULT_BBOX,
) -> list[dict]:
    """Return up to MAX_RESULTS suggestions for ``query``.

    Never raises: a provider outage, a timeout, or malformed JSON all come
    back as an empty list so the page simply shows no suggestions and the
    user can still type coordinates by hand.
    """
    text = " ".join((query or "").split())
    if len(text) < MIN_QUERY_LEN:
        return []
    text = text[:MAX_QUERY_LEN]

    cache_key = f"{provider}|{bbox}|{text.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Prefer results near the user's region; fall back to an unbounded search
    # so someone outside the default box is never left with zero suggestions.
    results = _fetch(text, provider, bbox)
    if not results and bbox:
        results = _fetch(text, provider, None)

    if results:
        _cache_put(cache_key, results)
    return results


def reverse(
    latitude: float,
    longitude: float,
    *,
    provider: str = DEFAULT_REVERSE_PROVIDER,
) -> dict | None:
    """Human-readable name for a coordinate pair (used after GPS).

    Returns ``{"label": ..., "detail": ...}`` or ``None``. Never raises: the
    caller only uses this to prettify the address box, so a failure simply
    leaves the box as it was.
    """
    lat = _as_float(latitude)
    lng = _as_float(longitude)
    if lat is None or lng is None:
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return None

    cache_key = f"rev|{provider}|{lat:.5f},{lng:.5f}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[0] if cached else None

    try:
        response = requests.get(
            provider,
            params={"lat": lat, "lon": lng, "limit": 1, "lang": "en"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        features = (response.json() or {}).get("features") or []
    except (requests.RequestException, ValueError):
        return None

    if not features:
        return None
    label, detail = _label_and_detail(features[0].get("properties") or {})
    result = {"label": label, "detail": detail}
    _cache_put(cache_key, [result])
    return result


def allowed(key: str) -> bool:
    """Public wrapper for the per-session throttle."""
    return _throttle(key)


def reset_state() -> None:
    """Clear throttle + cache (used by tests)."""
    with _throttle_lock:
        _throttle_state.clear()
    with _cache_lock:
        _cache.clear()
