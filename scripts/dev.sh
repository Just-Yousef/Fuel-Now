#!/usr/bin/env bash
# Start FuelNow with the settings required by the hosted preview environment.
#
# Why these flags:
#   SESSION_COOKIE_SAMESITE=None + SECURE=1  the preview renders the app inside a
#       cross-site (and often sandboxed) iframe, where SameSite=Lax cookies are
#       never sent back.
#   COOKIE_LESS_SESSIONS=1                   sandboxed iframes have an opaque
#       origin and may refuse cookies outright, so the session is also carried
#       in the URL as ?sid=... (see session_tokens.py).
# A normal deployment should NOT use these; plain `flask run` keeps the safe
# cookie-only defaults.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-5000}"
DB="instance/fuelnow.sqlite3"

[ -s "$DB" ] || { echo "No initialized database found - seeding demo data..."; FLASK_APP=app flask seed-db --reset; }

export FLASK_APP=app
export SESSION_COOKIE_SAMESITE=None
export SESSION_COOKIE_SECURE=1
export COOKIE_LESS_SESSIONS=1

exec flask run --host 0.0.0.0 --port "$PORT"
