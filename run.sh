#!/usr/bin/env bash
# One-command setup: create a virtualenv, install deps, seed the database, run the app.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

export FLASK_APP=app
if [ ! -s instance/fuelnow.sqlite3 ]; then
  echo "Seeding demo data…"
  flask seed-db --reset
fi

echo "Starting Veyro on http://127.0.0.1:5000 (Ctrl+C to stop)"
flask run --debug
