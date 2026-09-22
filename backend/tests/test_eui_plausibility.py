"""Event calendar seeding and the event-day plausibility check."""

from datetime import date, timedelta

from backend.app.eui.models import IndexValue
from backend.app.eui.plausibility import add_event, check_plausibility, list_events, seed_events
from backend.tests.eui_helpers import memory_session


def test_seed_events_is_idempotent():
    session = memory_session()
    assert seed_events(session) == 20  # 8 FOMC + 12 jobs reports
    assert seed_events(session) == 0
    assert add_event(session, date(2026, 9, 16), "FOMC rate decision", "rates") is not None
    assert len(list_events(session)) == 20


def test_check_compares_event_days_with_surrounding_baseline():
    session = memory_session()
    event_day = date(2026, 9, 16)
    for offset in range(-3, 4):
        value = 60.0 if offset == 0 else 40.0
        session.add(IndexValue(value_date=event_day + timedelta(days=offset), value=value, constituent_count=5))
    add_event(session, event_day, "FOMC rate decision", "rates")
    add_event(session, date(2026, 9, 18), "Quiet day event", "other")  # neighbours exclude other event days
    session.commit()

    report = check_plausibility(session)
    assert report["history_days"] == 7 and report["backtest_possible"] is False
    fomc = next(e for e in report["events"] if e["label"] == "FOMC rate decision")
    assert fomc["value"] == 60.0 and fomc["baseline"] == 40.0 and fomc["rise"] is True
    quiet = next(e for e in report["events"] if e["label"] == "Quiet day event")
    assert quiet["rise"] is False
    assert report["events_checked"] == 2 and report["events_with_rise"] == 1 and report["verdict"] == "plausible"
