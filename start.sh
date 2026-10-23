#!/usr/bin/env bash
# Starts Cooper AI on http://localhost:8000 (macOS / Linux).
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r backend/requirements.txt
if [ ! -d frontend/dist ]; then
  (cd frontend && npm install && npm run build)
fi
[ -f .env ] || cp .env.example .env
echo "Cooper AI: open http://localhost:8000"
exec uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
