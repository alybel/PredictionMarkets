"""Plausibility check standing in for the backtest while the history is short.

On days with rate decisions, labour-market data or geopolitical events the
index should be visibly higher than on the surrounding days. Event dates
live in ``eui_events``; FOMC decision days and US jobs-report days of 2026
are seeded, geopolitical events are added by hand via the API.
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.eui.models import IndexEvent, IndexValue

MIN_HISTORY_DAYS_FOR_BACKTEST = 180
BASELINE_WINDOW_DAYS = 5

FOMC_2026 = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
             date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9)]
JOBS_REPORT_2026 = [date(2026, 1, 9), date(2026, 2, 6), date(2026, 3, 6), date(2026, 4, 3), date(2026, 5, 8),
                    date(2026, 6, 5), date(2026, 7, 2), date(2026, 8, 7), date(2026, 9, 4), date(2026, 10, 2),
                    date(2026, 11, 6), date(2026, 12, 4)]


def add_event(session: Session, event_date: date, label: str, kind: str) -> IndexEvent:
    existing = session.scalar(select(IndexEvent).where(IndexEvent.event_date == event_date, IndexEvent.label == label))
    if existing:
        return existing
    event = IndexEvent(event_date=event_date, label=label, kind=kind)
    session.add(event)
    session.commit()
    return event


def seed_events(session: Session) -> int:
    """Insert the 2026 FOMC and jobs-report dates once; returns the number added."""
    before = len(list_events(session))
    for d in FOMC_2026:
        add_event(session, d, "FOMC rate decision", "rates")
    for d in JOBS_REPORT_2026:
        add_event(session, d, "US jobs report", "labour")
    return len(list_events(session)) - before


def list_events(session: Session) -> list[IndexEvent]:
    return list(session.scalars(select(IndexEvent).order_by(IndexEvent.event_date)))


def check_plausibility(session: Session, window: int = BASELINE_WINDOW_DAYS) -> dict:
    """Per event: index value vs. the mean of surrounding non-event days."""
    values = {row.value_date: row.value for row in session.scalars(select(IndexValue)) if row.value is not None}
    events = list_events(session)
    event_days = {e.event_date for e in events}
    checks = []
    for event in events:
        value = values.get(event.event_date)
        if value is None:
            continue
        neighbours = [
            values[d] for k in range(-window, window + 1) if k != 0
            for d in [event.event_date + timedelta(days=k)] if d in values and d not in event_days
        ]
        baseline = sum(neighbours) / len(neighbours) if neighbours else None
        checks.append({
            "event_date": event.event_date.isoformat(), "label": event.label, "kind": event.kind,
            "value": value, "baseline": baseline,
            "rise": None if baseline is None else value > baseline,
        })
    judged = [c for c in checks if c["rise"] is not None]
    rises = sum(1 for c in judged if c["rise"])
    return {
        "history_days": len(values),
        "backtest_possible": len(values) >= MIN_HISTORY_DAYS_FOR_BACKTEST,
        "min_history_days_for_backtest": MIN_HISTORY_DAYS_FOR_BACKTEST,
        "events_checked": len(judged),
        "events_with_rise": rises,
        "share_with_rise": (rises / len(judged)) if judged else None,
        "verdict": _verdict(judged, rises),
        "events": checks,
    }


def _verdict(judged, rises) -> str:
    if not judged:
        return "insufficient_data"
    return "plausible" if rises / len(judged) >= 0.5 else "implausible"
