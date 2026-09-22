#!/bin/bash
# Täglicher Lauf des Economic Uncertainty Index: Snapshot -> Zuordnung -> Indexwert.
# Cron-Beispiel (täglich 06:00 UTC): 0 6 * * * /Users/alex/TechProjects/PredictionMarketIndex/eui_daily.sh
# Umgebung: DATABASE_URL (PostgreSQL; ohne Angabe SQLite backend/dev.db), ANTHROPIC_API_KEY (sonst Keyword-Fallback).
cd "$(dirname "$0")"
PYTHON=.venv/bin/python
[ -x "$PYTHON" ] || PYTHON=python3
exec "$PYTHON" -m backend.app.eui.daily "$@"
