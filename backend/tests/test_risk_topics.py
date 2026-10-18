"""Risk topics (risk index 2/7, 1017-1107): classification once per topic, discarded answers, fallback, review via API,
hand-over, timeline (start date, < 3 years note), auto-classification of inbox markets. Fake classifiers only: no LLM, no network."""

import json
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api import app
from backend.app.deps import get_session
from backend.app.directory.models import DirectoryMarket
from backend.app.eui.models import MarketClassification
from backend.app.risk.api import get_risk_classifier
from backend.app.risk.classify import FALLBACK, HaikuClassifier, KeywordClassifier, classifier_from_env, validate
from backend.app.risk.models import RiskAssessment
from backend.app.risk.service import accepted_markets, classify_new_markets, classify_topic, ensure_pilot, timeline
from backend.tests.eui_helpers import memory_session

TODAY = date(2026, 10, 17)


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


class FakeHaiku:
    """Records every market id sent; answers relevant/high, plus optional bad answers."""

    name = "claude-haiku-5-5"

    def __init__(self, extra=()):
        self.sent: list[int] = []
        self.extra = list(extra)

    def classify(self, topic, items):
        self.sent += [i.market_id for i in items]
        raw = [{"id": str(i.market_id), "relevant": True, "rationale": "crash question", "reading": "high"} for i in items]
        return validate(raw + self.extra, {str(i.market_id): i for i in items}, self.name)


def add_market(session, ext, title, listed=None, end=None, tags=(), is_new=False):
    m = DirectoryMarket(source="polymarket", external_id=ext, title=title, tags=list(tags), listed_at=listed, end_date=end,
                        status="active", first_seen_at=dt(2026, 10, 1), is_new=is_new)
    session.add(m)
    session.commit()
    return m


@pytest.fixture
def session():
    s = memory_session()
    add_market(s, "a", "S&P 500 crash in 2026?", dt(2025, 1, 10), dt(2026, 12, 31))
    add_market(s, "b", "Nasdaq above 20000 at year end?", dt(2025, 6, 1), dt(2025, 12, 31))
    add_market(s, "c", "Who wins the Super Bowl?", dt(2025, 1, 1), tags=["Sports"])
    add_market(s, "d", "Big drop this year?", dt(2026, 3, 1), tags=["Stocks"])
    return s


def test_unknown_id_and_invalid_reading_are_discarded_and_logged(session, caplog):
    topic = ensure_pilot(session)
    bad = [{"id": "999999", "relevant": True, "rationale": "x", "reading": "high"}]
    clf = FakeHaiku(extra=bad)
    with caplog.at_level(logging.WARNING):
        summary = classify_topic(session, topic, clf)
    assert summary["discarded"] == 1 and summary["stored"] == 3 and summary["candidates"] == 3  # sports market not pre-filtered
    assert "unknown market id '999999' discarded" in caplog.text
    items = {"7": SimpleNamespace(market_id=7)}
    with caplog.at_level(logging.WARNING):
        res = validate([{"id": "7", "relevant": True, "rationale": "", "reading": "sideways"}], items, "m")
    assert res.discarded == 1 and not res.decisions and "invalid reading 'sideways'" in caplog.text


def test_second_run_sends_no_market_twice(session, caplog):
    topic, clf = ensure_pilot(session), FakeHaiku()
    classify_topic(session, topic, clf)
    first = list(clf.sent)
    with caplog.at_level(logging.INFO):
        second = classify_topic(session, topic, clf)
    assert second["sent"] == 0 and second["already"] == 3 and clf.sent == first
    assert len(set(clf.sent)) == len(clf.sent) == 3
    assert "3 already classified, 0 sent" in caplog.text


def test_fallback_without_key_is_marked_and_upgraded_once_by_model(session, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    clf = classifier_from_env()
    assert clf.name == FALLBACK
    topic = ensure_pilot(session)
    summary = classify_topic(session, topic, clf)
    assert summary["fallback"] and summary["stored"] == 3
    rows = {r.market_id: r for r in session.scalars(select(RiskAssessment))}
    assert {r.model for r in rows.values()} == {FALLBACK}
    assert KeywordClassifier.reading("Nasdaq above 20000?") == "low" and KeywordClassifier.reading("S&P crash?") == "high"
    haiku = FakeHaiku()
    assert classify_topic(session, topic, haiku)["upgraded"] == 3
    assert classify_topic(session, topic, haiku)["sent"] == 0 and len(haiku.sent) == 3


def test_haiku_request_and_unparsable_answer_without_network():
    class Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            text = json.dumps({"decisions": [{"id": "1", "relevant": True, "rationale": "r", "reading": "uncertainty"}]})
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])

    msgs = Messages()
    from backend.app.risk.classify import Item
    res = HaikuClassifier(client=SimpleNamespace(messages=msgs)).classify({"name": "T", "question": "Q"}, [Item(1, "x")])
    assert res.decisions[0].reading == "uncertainty" and msgs.calls[0]["model"] == "claude-haiku-5-5"
    assert json.loads(msgs.calls[0]["messages"][0]["content"])["topic"] == {"name": "T", "question": "Q"}


def test_api_list_accept_reject_change_reading_and_handover(session, caplog):
    clf = FakeHaiku()
    app.dependency_overrides.update({get_session: lambda: session, get_risk_classifier: lambda: clf})
    try:
        client = TestClient(app)
        topic = next(t for t in client.get("/risk/topics").json() if t["name"] == "Marktcrash-Risiko")
        r = client.post(f"/risk/topics/{topic['id']}/classify").json()
        assert r["stored"] == 3
        items = client.get(f"/risk/topics/{topic['id']}/assessments").json()["assessments"]
        assert all(i["rationale"] and i["proposed_reading"] == "high" for i in items)
        a, b, _ = items
        with caplog.at_level(logging.INFO):
            assert client.patch(f"/risk/assessments/{a['id']}", json={"status": "accepted", "reading": "low"}).json()["status"] == "accepted"
            assert client.patch(f"/risk/assessments/{b['id']}", json={"status": "rejected"}).json()["status"] == "rejected"
        assert "status proposed -> accepted, reading high -> low" in caplog.text
        assert client.patch(f"/risk/assessments/{a['id']}", json={"reading": "bogus"}).status_code == 422
        handover = client.get(f"/risk/topics/{topic['id']}/accepted").json()
        assert [h["market_id"] for h in handover] == [a["market_id"]] and handover[0]["reading"] == "low"
        assert b["market_id"] not in [h["market_id"] for h in handover]
        counts = client.get(f"/risk/topics/{topic['id']}/assessments").json()["counts"]
        assert counts == {"proposed": 1, "accepted": 1, "rejected": 1}
    finally:
        app.dependency_overrides.clear()


def test_timeline_counts_market_only_from_start_and_flags_short_history(session):
    topic = ensure_pilot(session)
    classify_topic(session, topic, FakeHaiku())
    tl = timeline(session, topic, TODAY)
    months = {m["month"]: m["count"] for m in tl["months"]}
    assert tl["window_start"] <= "2023-10-17" and "2023-10" in months  # at least three years shown
    assert months["2024-12"] == 0 and months["2025-01"] == 1  # "a" starts 2025-01-10, never earlier
    assert months["2025-06"] == 2 and months["2026-01"] == 1 and months["2026-03"] == 2
    assert tl["short_history"] and "kürzer als 3 Jahre" in tl["note"] and tl["history_start"] == "2025-01-10"
    add_market(session, "old", "Stock market crash 2022?", dt(2022, 1, 5), dt(2022, 12, 31))
    classify_topic(session, topic, FakeHaiku())
    tl = timeline(session, topic, TODAY)
    assert not tl["short_history"] and tl["note"] is None and tl["window_start"] == "2022-01-05"


def test_rejected_market_leaves_timeline(session):
    topic = ensure_pilot(session)
    classify_topic(session, topic, FakeHaiku())
    row = session.scalar(select(RiskAssessment).join(DirectoryMarket).where(DirectoryMarket.external_id == "b"))
    row.status = "rejected"
    session.commit()
    assert timeline(session, topic, TODAY)["total_markets"] == 2
    assert accepted_markets(session, topic) == []


def test_new_inbox_market_is_classified_automatically(session):
    topic, clf = ensure_pilot(session), FakeHaiku()
    classify_topic(session, topic, clf)
    new = add_market(session, "n", "Stock market crash next week?", dt(2026, 10, 16), is_new=True)
    summary = classify_new_markets(session, clf)
    assert summary[0]["stored"] == 1 and clf.sent[-1] == new.id
    assert session.scalar(select(func.count()).select_from(RiskAssessment)) == 4


def test_eui_classification_untouched(session):
    classify_topic(session, ensure_pilot(session), FakeHaiku())
    assert session.scalar(select(func.count()).select_from(MarketClassification)) == 0
