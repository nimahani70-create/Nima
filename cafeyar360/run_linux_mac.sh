#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-lock.txt
export DEMO_MODE="${DEMO_MODE:-1}"
python manage.py init
python -m uvicorn app:app --host 0.0.0.0 --port 8000
