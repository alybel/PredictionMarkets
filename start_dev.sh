#!/bin/bash
# Startet die PredictionMarketIndex REST-API mit Hot Reload im Vordergrund (Port 8000).
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
UVICORN=.venv/bin/uvicorn
[ -x "$UVICORN" ] || UVICORN=uvicorn
PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
# Schema vor dem Serverstart auf head migrieren (bestehende Daten bleiben erhalten).
"$PYTHON" -m backend.app.schema_state || { echo "Schema-Migration fehlgeschlagen (siehe logs/app.log)"; exit 1; }
echo "http://localhost:$PORT"
exec "$UVICORN" backend.app.api:app --port "$PORT" --reload
