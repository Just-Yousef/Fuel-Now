# Veyro — fuel delivery & roadside mechanic matching

A four-role web application: **drivers** order fuel from nearby stations and request
nearby mechanics; **station operators** work the order queue and manage prices;
**mechanics** publish their availability and service area; **administrators** approve
accounts and monitor everything.

All location logic (haversine distance, radius filtering, nearest-first sorting) lives in
one reusable module — [`geo.py`](geo.py) — and is used by every blueprint and by the tests.

---

## 0. Design

The interface uses a **deep evergreen and brass** palette, warm high-contrast text, serif
display headings and a clean sans-serif UI. The landing page uses a responsive map-inspired
illustration made entirely in CSS; the shared layout adapts the role navigation, cards and
forms to smaller screens. Styling is driven by CSS custom properties at the top of
`static/css/app.css` — no external fonts, icon packs or CDNs, so it renders offline and
inside the sandboxed preview.

To re-theme, edit the `:root` block. The three that matter most:

| Variable | Role |
|---|---|
| `--bg` / `--surface` / `--bg-deep` | page, panel and header grounds |
| `--ink` / `--muted` | primary and secondary text |
| `--brass` / `--brass-bright` | accent: buttons, active nav, distances, focus rings |

Status colours are semantic (`--ok`, `--danger`, `--warn`, `--info`) with matching
`-soft` backgrounds and `-line` borders, so badges and flash messages stay legible on dark
ground without per-page overrides.

## 1. Quick start

```bash
cd fuelnow
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export FLASK_APP=app                                  # Windows: set FLASK_APP=app
flask seed-db --reset                                 # create tables + demo data
flask run --debug                                     # http://127.0.0.1:5000
```

Open <http://127.0.0.1:5000>, sign in, and allow the browser's location prompt.

### Demo accounts (password `FuelNow!2026` for all)

| Role | Email | Lands on |
|---|---|---|
| Driver | `amina@example.com` | `/driver/` |
| Driver | `chidi@example.com` | `/driver/` |
| Station operator | `station1@fuelnow.ng` … `station6@fuelnow.ng` | `/station/` |
| Mechanic | `mechanic1@fuelnow.ng` … `mechanic5@fuelnow.ng` | `/mechanic/` |
| Administrator | `admin@fuelnow.ng` | `/admin/` |

`station5` and `mechanic5` are seeded as **pending**, so you can practise the admin
approval flow. `station6` is in **Lagos (~530 km away)** to prove the radius filter works.
`flask list-users` prints every account.

### Tests

```bash
python -m pytest          # test suite
```

Coverage maps to the three requested flows:

| File | What it proves |
|---|---|
| `tests/test_orders.py` | Placing a fuel order (totals, snapshot, validation), station status transitions, price management |
| `tests/test_discovery.py` | Finding nearby stations/mechanics, distance sorting, radius + availability filtering, requesting a mechanic |
| `tests/test_auth_rbac.py` | Every role reaches only its own pages; anonymous → login, wrong role → 403, deactivated → no login |
| `tests/test_geo.py` | The `geo` module itself: haversine accuracy, coordinate validation, radius clamping |
| `tests/test_security.py` | All 14 hardening measures: admin protection, RBAC matrix, CSRF, email verification, hashing, log scrubbing, CSP/XSS, SQL injection, validation, uploads, webhooks, CORS, production config |

---

## 2. Why Flask?

**Flask + Flask-SQLAlchemy + Flask-Login**, not FastAPI/Django:

* **Server-rendered Jinja templates are the right shape for this brief.** Four
  role-specific UIs, lots of forms and tables, no real-time client state —
  FastAPI would mean hand-rolling a front end and an auth/session layer for no gain.
* **One dependency-light stack** (`flask`, `flask-sqlalchemy`, `flask-login`) stays readable
  for reviewers; Django's admin would have blurred the "build an admin page" requirement.
* **Flask-Login gives the role gate in a dozen lines** (`auth_utils.role_required`), and Flask
  blueprints map 1:1 onto the four requested pages.
* **The ORM is framework-independent**, so the models port to FastAPI/SQLModel later if the
  project grows an API-first client.

`app.py` uses the application-factory pattern so tests can spin up isolated in-memory
databases, and `config.py` switches between development / testing / production settings.

---

## 3. Project structure

```
fuelnow/
├── app.py                  # application factory, blueprint + filter registration, error handlers
├── config.py               # dev/testing/production config, domain defaults
├── extensions.py           # db + login_manager singletons (avoids import cycles)
├── models.py               # all 6 tables + status enums + transition rules
├── geo.py                  # ★ single reusable location module (haversine, nearby_*, sorting)
├── auth_utils.py           # role_required / admin_required + session pinning
├── security.py             # CSRF, CSP nonces, headers, CORS, validators, log scrubbing
├── uploads.py              # image upload validation (magic bytes, size, re-encode)
├── tokens.py               # signed email-verification / password-reset tokens
├── seed.py                 # demo Abuja/Lagos data
├── requirements.txt
├── pytest.ini
├── instance/               # SQLite database lives here (created at runtime)
├── blueprints/
│   ├── public.py           # landing page, /healthz, CLI commands (init-db, seed-db, list-users)
│   ├── auth.py             # register, login, logout, pending-approval, account
│   ├── driver.py           # nearby search, orders, service requests, location API
│   ├── station.py          # order queue, status transitions, fuel types & prices, profile
│   ├── mechanic.py         # availability/service area, matched requests, profile
│   ├── admin.py            # approvals, deactivation, platform monitoring
│   └── webhooks.py         # HMAC-signed provider callbacks
├── templates/
│   ├── base.html           # role-aware navigation
│   ├── index.html          # public landing page
│   ├── auth/               # login, register, pending, account
│   ├── driver/             # dashboard, stations, mechanics, orders, requests, _macros
│   ├── station/            # dashboard, orders, products, profile
│   ├── mechanic/           # dashboard, requests, availability, profile
│   ├── admin/              # dashboard, users, stations, mechanics, orders, requests
│   └── errors/             # 403, 404, 500
├── static/
│   ├── css/app.css         # hand-written stylesheet, no build step, no CDN
│   └── js/app.js           # geolocation helper, live order total, nav state
└── tests/                  # conftest + 4 test modules
```

---

## 4. Pages and what each role can do

### Driver (`/driver/`)
| Route | Purpose |
|---|---|
| `/driver/` | Dashboard: nearest 5 stations and mechanics, KPIs, recent orders |
| `/driver/stations` | All approved stations in radius, nearest first, filter by fuel type |
| `/driver/mechanics` | All available mechanics whose service area covers you |
| `/driver/orders` (`POST /driver/orders`) | Place an order (fuel type + litres), then track status |
| `/driver/orders/<id>/cancel` | Cancel while still pending/accepted |
| `/driver/requests` (`POST /driver/requests`) | Request a mechanic, then track status |
| `POST /driver/location` | JSON endpoint that stores the browser's GPS fix |

### Fueling station (`/station/`)
Dashboard with incoming orders and one-click next-step buttons; full order table with a
status filter; `accepted → fueling → completed` (or `cancelled` at any open step, enforced
server-side by `ORDER_TRANSITIONS`); add/edit/remove fuel types and prices, toggle stock;
edit station details, coordinates and open/closed state.

### Mechanic (`/mechanic/`)
Toggle availability, set service radius (1–200 km) and base coordinates (with a
"use my current position" button); requests matched to the area, nearest first; advance
`pending → accepted → on the way → in progress → completed` or decline; edit business
profile, specialties and hourly rate.

### Admin (`/admin/`)
Platform KPIs; approve/reject pending stations and mechanics inline; full user list with
role and status filters; approve / deactivate / reactivate any user, station or mechanic;
read-only monitoring of every fuel order and service request. Deactivating a user blocks
login immediately, and an admin cannot deactivate their own account or the last admin.

---

## 4b. Security

Every measure below is covered by `tests/test_security.py` (115 tests). Nothing here is
configured by hand at runtime — it is wired in `security.py`, `auth_utils.py` and
`create_app()`.

| Measure | Implementation | Test coverage |
|---|---|---|
| **Admin routes protected** | All 9 admin routes use `@admin_required`, which wraps `role_required(ROLE_ADMIN)`, logs every access, and denies by default | every admin route × anonymous + 3 other roles = 36 cases; plus a static guard that no admin view sits outside the admin module |
| **Role-level security** | `role_required(*roles)` on every view: anonymous → login, wrong role → **403**, unapproved → pending, unverified → verify notice. Object-level ownership checked too (a mechanic cannot touch another's request, a driver cannot cancel another's order) | role matrix, cross-role 403s, ownership tests |
| **Email verification** | `itsdangerous` signed tokens, single-purpose scope, 24 h expiry, bound to a fingerprint of the password hash. Drivers must verify before ordering; businesses are approved by an admin. Demo mode shows the link in the UI because no SMTP is configured | verify success, tampered token, wrong scope, expiry, invalidation after password change |
| **Secure password hashing** | **scrypt** (`scrypt:32768:8:1`) via Werkzeug, auto-salted; `check_password` is constant-time; legacy hashes are transparently re-hashed on successful login; weak/common passwords rejected | hash prefix, salt uniqueness, wrong-password rejection, weak-password policy |
| **Sensitive data out of logs** | `security.ScrubFilter` is attached to the app logger **and** Werkzeug's access logger. `sid`, `csrf_token`, `password`, `session_token`, `Authorization`, `Set-Cookie` are redacted. Failed logins log a **fingerprint**, never the address | scrubber unit tests + live log inspection |
| **Cross-site scripting** | Jinja autoescaping (no `\|safe` anywhere), a nonce-based **CSP** (`script-src 'self' 'nonce-…'`), `clean_text()` rejecting `<`/`>`, plus `nosniff`. All 6 inline scripts carry nonces | CSP header, nonce presence, stored-XSS attempt renders inert, validator tests |
| **Parameterised SQL** | SQLAlchemy ORM throughout — `select()`/`filter_by()` bind parameters; no f-strings or `%` in SQL. A static test fails the build if string-built SQL appears | injection payloads against login and query params, plus a source scan |
| **Form validation** | `security.py` validators: `validate_email`, `validate_password`, `validate_phone`, `validate_float` (min/max), `validate_choice` (allow-list), `validate_plate`, `clean_text` (length + markup). Applied in auth, driver, station, mechanic and admin views | per-validator tests + order-submission validation |
| **File uploads** | Extension allow-list **and** magic-byte sniffing (a `.png` containing HTML is rejected), 2 MB cap, double-extension rejection, server-generated random filename, Pillow re-encode to strip EXIF, `MAX_CONTENT_LENGTH` → 413 | extension, fake image, oversized, empty, real-PNG accepted, random naming |
| **Webhook signatures** | HMAC-SHA256 over `timestamp.body` in `X-Signature`, checked with `hmac.compare_digest`; `X-Timestamp` must be within 300 s (replay protection); **fails closed** with 503 if `WEBHOOK_SECRET` is unset | missing/bad signature, stale timestamp, valid signature applied, unknown status |
| **CORS** | Same-origin by default — no CORS headers are emitted at all. An explicit `CORS_ALLOWED_ORIGINS` allow-list adds scoped headers with `Vary: Origin` | arbitrary origin gets nothing; allow-listed origin gets exact-scope headers |
| **Production debugging off** | `ProductionConfig.DEBUG=False`, `PROPAGATE_EXCEPTIONS=False`, `TEMPLATES_AUTO_RELOAD=False`. `create_app("production")` **refuses to boot** unless `SECRET_KEY` (≥32 chars), `SESSION_COOKIE_SECURE`, and `DATABASE_URL` are set and `COOKIE_LESS_SESSIONS` is off. Error handlers never leak tracebacks | config assertions, boot-refusal test, 404 body contains no traceback |
| **Updated dependencies** | Flask 3.1.0→**3.1.3**, pytest 9.0.3→**9.2.0**, added Pillow **11.3.0** and gunicorn **27.0.0**; all verified against PyPI | full suite re-run on the new pins |

### Additional protections wired in the same pass
* **CSRF tokens on all 29 POST forms** (`{{ csrf_field() }}`), validated in a `before_request`
  hook with `hmac.compare_digest`; a static test fails if a future POST form omits the field.
  A token is accepted three ways, all unforgeable: (1) match against the session copy,
  (2) double-submit against the readable `fuelnow_csrf` cookie, or (3) a **self-verifying
  signed token** (HMAC of a random nonce + expiry, keyed with `SECRET_KEY`). Path (3)
  exists because a sandboxed preview iframe stores **no cookies at all**, so a
  session-only comparison rejected every form submit with
  *"Invalid or missing CSRF token"*. An attacker still cannot mint a token: they cannot
  guess their victim's nonce, so no valid (nonce, signature) pair can be precomputed.
  `fetch()`-driven requests (the GPS "use my location" button posts JSON) send the same token
  in an **`X-CSRF-Token`** header, read from `<meta name="csrf-token">` in `base.html` —
  a token-less or wrong-token POST is rejected with 400. Webhooks are exempt because they
  authenticate by signature.
* **Stable development secret.** Without `SECRET_KEY` in the environment the app
  persists a generated key to `instance/.secret_key` (mode `0600`, git-ignored)
  instead of making a new one per process. A per-process key invalidates the
  signature of every session cookie on restart, which discards logins *and* their
  CSRF tokens — the browser then shows **"Invalid or missing CSRF token"** on any
  form that was already open. Production is unaffected: it still refuses to boot
  without an explicit `SECRET_KEY`, because the guard reads `os.environ`, not config.
* **Session pinning** (`auth_utils.harden_session`) — a session is bound to a digest of IP +
  User-Agent; reuse from another client clears it.
* **Security headers**: `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`,
  `Cross-Origin-Opener-Policy`, and `Strict-Transport-Security` when cookies are `Secure`.
* **Uniform auth responses** — identical messages and timings for unknown-user and
  bad-password, so the login form cannot be used to enumerate accounts.

### Production run

```bash
export FLASK_CONFIG=production
export MAIL_SERVER="smtp.example.com"
export MAIL_PORT=587
export MAIL_USERNAME="fuelnow@example.com"
export MAIL_PASSWORD="replace-with-a-secret"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DATABASE_URL="postgresql+psycopg://user:pass@host/fuelnow"
export COOKIE_LESS_SESSIONS=0          # never put session tokens in URLs
export WEBHOOK_SECRET="…"              # required for /webhooks/*
export CORS_ALLOWED_ORIGINS="https://app.example.com"   # optional
gunicorn -w 4 -b 0.0.0.0:8000 'app:create_app("production")'
```

Put TLS in front (browsers only grant geolocation on secure origins), and set
`MAIL_SERVER` so verification and password-reset links are emailed instead of
surfaced in the UI. SMTP uses `MAIL_PORT` (587 by default), STARTTLS by default,
and optional `MAIL_USERNAME` / `MAIL_PASSWORD` credentials. Set `MAIL_USE_TLS=0`
only when your SMTP server does not support STARTTLS.

### Deliberate trade-offs
* **`style-src 'unsafe-inline'`** — templates use 60+ inline `style` attributes for small
  layout tweaks. Scripts are nonce-gated, which is where XSS risk actually lives; moving
  styles to classes would let this be locked down further.
* **In-memory session tokens / webhook replay window** — the cookie-less fallback and the
  300 s timestamp window assume a single process. A multi-worker deployment should back
  `session_tokens` with Redis and store webhook ids for replay rejection.
* **No rate limiting** — a 429 handler exists, but no limiter is wired (Flask-Limiter would
  be the drop-in). Login throttling is the first thing to add before public exposure.

---

### Location entry

Drivers describe where they are in words; the app turns that into coordinates.

* **Drivers never type raw coordinates.** Two ways in, both writing to hidden
  `latitude`/`longitude` inputs that post with the form:
  1. **Type an address** → suggestions from ``/api/places``. Keyboard: **↑/↓**,
     **Enter** to choose, **Esc** to dismiss.
  2. **"Use my current location"** → browser GPS (``GeoLocate`` in `static/js/app.js`)
     fills the coordinates, then ``/api/places/reverse`` names the place in the
     address box so it never reads blank.
* Stations and mechanics keep visible numeric Latitude/Longitude fields, since they
  set a fixed business location rather than standing somewhere.
* **Provider: [Photon](https://photon.komoot.io)** (komoot, OpenStreetMap data, no API
  key). Nominatim was rejected for this job — its public usage policy **forbids
  autocomplete**, capping traffic at 1 req/sec. Photon is built for search-as-you-type.
  Both are free; data is © OpenStreetMap contributors (attribution is shown in the UI).
* **Requests are proxied server-side** (`geo_search.py` → `/api/places`) rather than
  called from the browser, which keeps the CSP at `connect-src 'self'`, adds a
  25-lookups/10s per-IP throttle and a 2-minute cache, and means swapping provider is
  one config value (`PLACES_PROVIDER`).
* **Results are biased to Nigeria** (`DEFAULT_BBOX`) because a bare "Maitama" query
  otherwise ranks towns in Niger and Chad above the Abuja district. The bias is a
  preference, not a filter: if a biased search finds nothing it retries unbounded, so
  a user outside the box is never left with an empty list.
* **Fails soft.** Provider down, timeout, or throttled → no suggestions, an explanatory
  line, and the form still submits. Set `PLACES_AUTOCOMPLETE=0` to switch it off entirely.
* Stations and mechanics still have a **"Use my current position"** GPS button; drivers
  can also share GPS from the dashboard after signing in (`static/js/app.js`, `GeoLocate`).

## 5. Data model

```
users ──1:1── stations ──1:N── fuel_products
  │               └────1:N──── fuel_orders ──N:1── users (driver)
  └──1:1── mechanics ──1:N── service_requests ──N:1── users (driver)
```

| Table | Key columns |
|---|---|
| `users` | `email` (unique), `password_hash`, `full_name`, `phone`, `role`, `status`, `latitude`, `longitude` |
| `stations` | `owner_id` → users, `name`, `address`, `city`, `latitude`, `longitude`, `status`, `is_open`, `opening_hours` |
| `fuel_products` | `station_id`, `fuel_type`, `price_per_litre`, `in_stock` — unique per (station, fuel_type) |
| `mechanics` | `user_id` → users, `business_name`, `specialties`, `latitude`, `longitude`, `service_radius_km`, `available`, `hourly_rate`, `status` |
| `fuel_orders` | `reference`, `driver_id`, `station_id`, `fuel_type`, `quantity_litres`, `unit_price`, `total_price`, `status`, driver's lat/lng + `distance_km` snapshot |
| `service_requests` | `reference`, `driver_id`, `mechanic_id`, `issue`, `description`, `vehicle`, `latitude`, `longitude`, `distance_km`, `status` |

Status vocabularies are string constants in `models.py` (`ORDER_STATUSES`,
`REQUEST_STATUSES`, `ACCOUNT_STATUSES`) with `ORDER_TRANSITIONS` / `REQUEST_TRANSITIONS`
tables driving both the UI buttons and the server-side validation.

### Setup "migrations"
There is no Alembic step for this scope — the schema is created from the models:
`flask init-db` (tables only) or `flask seed-db [--reset]` (tables + demo data).
`--reset` drops everything first, which is the supported way to re-apply schema changes
during development. To use PostgreSQL instead, set `DATABASE_URL`, e.g.
`postgresql+psycopg://user:pass@localhost/fuelnow`, then run `flask init-db`.

---

## 6. Location handling (`geo.py`)

* **Input:** the browser's `navigator.geolocation.getCurrentPosition()` result is POSTed to
  `/driver/location` as JSON, stored on the user row, and the page reloads with
  `?lat=…&lng=…`. Every listing page also carries the coordinates in hidden fields, so a
  shared link reproduces the same results. If nothing is available, the last saved position
  on the user record is used as a fallback.
* **Maths:** haversine great-circle distance (`EARTH_RADIUS_KM = 6371.0088`). Accuracy is far
  better than any display precision at city scale, and no third-party API or key is needed.
* **Filtering:** stations must be `approved` **and** open; mechanics must be `approved` and
  (for driver search) `available`. A mechanic also appears when the driver is inside the
  mechanic's own `service_radius_km`, even if that is larger than the driver's search radius.
* **Sorting:** `sort_by_distance()` is the only sort used — everything is nearest-first.
* **Radius:** user-supplied radius is clamped to `MAX_SEARCH_RADIUS_KM` (200 km) so a stray
  query cannot dump the whole table.
* **Reuse:** `nearby_stations()`, `nearby_mechanics()`, `distance_km()`, `haversine_km()`,
  `parse_coords()`, `clamp_radius()` and `is_within_service_area()` are imported by the
  driver, mechanic and admin blueprints and by all the geo tests. No other module does
  trigonometry. `_matched_requests()` in `mechanic.py` re-uses
  `is_within_service_area()` and sorts by the returned distance.

---

## 7. Authentication & role-based access

* Passwords are hashed with Werkzeug (`generate_password_hash` / `check_password_hash`);
  sessions are signed with `SECRET_KEY` and use `HttpOnly`, `SameSite=Lax` cookies.
* Every protected view is wrapped in `@role_required(ROLE_…)` from `auth_utils.py`, which
  returns **redirect to login** for anonymous users, **403** for the wrong role, and
  **redirect to `/pending`** for accounts that are not yet approved. Templates additionally
  hide nav links per role, but that is cosmetic — the decorator is the real gate.
* Object-level checks are enforced too: a driver can only cancel *their own* orders, a
  station can only touch *its own* products/orders, and a mechanic can only update *their
  own* requests. Each of those is covered by a test.

---

## 8. Assumptions (deliberate defaults for unspecified details)

1. **Payment:** none. No card data is collected; orders carry a `unit_price` and computed
   `total_price`, and settlement happens at the pump. The DB has room for a payments table
   later.
2. **Fuel types:** free-text per station, seeded with Nigerian market names — Petrol (PMS),
   Diesel (AGO), Kerosene (DPK), CNG, LPG, EV charging. Each station sets its own price;
   prices are in **NGN per litre** (`DEFAULT_CURRENCY` in `config.py`).
3. **No live payment/map provider:** no Google Maps/Mapbox key. Distances are numeric
   ("2.2 km away") rather than a rendered map. Station/mechanic coordinates are editable in
   the UI, so a map library can be dropped in later against the same `latitude`/`longitude`.
4. **Delivery vs pickup:** an order is treated as an on-site/at-pump order handled by the
   station's own attendants; the driver's position is snapshotted on the order for context
   (and the `delivery_note` field carries instructions).
5. **Approval policy:** drivers self-activate immediately; station operators and mechanics
   register as `pending` and are invisible to drivers until an admin approves them.
6. **Order limits:** 1–500 litres per order; status changes are one-directional and
   terminal states (`completed`, `cancelled`) are final.
7. **Single station per operator, single mechanic profile per account** (enforced by unique
   FKs). Multi-branch operators would need a join table.
8. **Database:** SQLite file in `instance/` for zero-setup demo; swap `DATABASE_URL` for
   Postgres/MySQL in production.
9. **Search radius default 25 km, max 200 km**; a mechanic's default service radius is 15 km.

---

## 9. Other commands & notes

```bash
flask init-db                 # create tables only
flask seed-db --reset         # wipe + reseed demo data
flask list-users              # print every account
python -m pytest -v           # verbose test run
curl localhost:5000/healthz   # {"status":"ok"}
```

### Session cookies in embedded / preview environments

By default the session cookie is issued with `SameSite=Lax`, which is correct for a normal
top-level deployment. If the app is **embedded in a cross-site iframe** (hosted preview
panels, some CMS admin frames), browsers refuse to send Lax cookies and you will observe a
"broken login": `POST /login` returns 302, then `GET /driver/` bounces straight back to
`/login`, because the follow-up request arrives without a session.

Run with `None`/`Secure` in that case:

```bash
SESSION_COOKIE_SAMESITE=None SESSION_COOKIE_SECURE=1 flask run
```

Both are read from the environment in `config.py` (`SESSION_COOKIE_SAMESITE`,
`SESSION_COOKIE_SECURE=1`). `SameSite=None` requires HTTPS, which hosted previews provide;
do not use it for a plain-HTTP top-level deployment — keep the default `Lax` there.

### Cookie-less sessions (sandboxed preview iframes)

If the app runs inside a **sandboxed** iframe (`sandbox="allow-scripts"`, which gives the
document an opaque origin) or behind a proxy that strips `Set-Cookie`, the browser will not
store the session cookie at all — even with `SameSite=None`. The symptom is a login that
"does nothing": `POST /login` returns 302 to `/driver/`, and that request bounces straight
back to `/login`.

For that case the app has a cookie-less fallback (`session_tokens.py`): on login it also
mints an opaque random token, stores it server-side (12 h TTL) and carries it in the URL as
`?sid=…`. All template links and form actions include it, and an `after_request` hook adds
it to every internal redirect. So authentication survives with cookies fully blocked.

```bash
COOKIE_LESS_SESSIONS=1 SESSION_COOKIE_SAMESITE=None SESSION_COOKIE_SECURE=1 flask run
```

It is **off by default** and turns itself on automatically when
`SESSION_COOKIE_SAMESITE=None`. Keep it off in production: a token in the URL can leak via
`Referer` headers and shared links, which is why the default deployment uses cookies only.
`sid` is accepted from the query string, form body or JSON, so POSTs and the geolocation
`fetch()` work too.

**Production checklist:** export `SECRET_KEY`, export `DATABASE_URL`, export
`FLASK_CONFIG=production`, serve with a real WSGI server
(`gunicorn -w 4 'app:create_app("production")'`), and put HTTPS in front — browsers only
grant geolocation on secure origins or `localhost`.
