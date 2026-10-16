#!/bin/bash
# Startet die PredictionMarketIndex REST-API mit Hot Reload im Vordergrund (Port 8000).
cd "$(dirname "$0")"
PORT=8000
UVICORN=.venv/bin/uvicorn
[ -x "$UVICORN" ] || UVICORN=uvicorn
echo "http://localhost:$PORT"
exec "$UVICORN" backend.app.api:app --port "$PORT" --reload
