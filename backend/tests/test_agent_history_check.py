"""Agent history check (1016-1951): short stored histories are extended from the source. Fake fetcher, no network, no LLM."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app.agent import api as agent_api
from backend.app.agent import history_check
from backend.app.agent.assistant import RuleAgent
from backend.app.agent.history_check import SHORT_DAYS, extend_history, short_candidates
from backend.app.agent.pool import gather
from backend.app.api import app
from backend.app.backfill.sources import DayPoint
from backend.app.deps import get_session
from backend.app.eui.models import MarketSnapshot
from backend.app.quality.pit import BACKFILL
from backend.tests.eui_helpers import market, memory_session
from backend.tests.test_index_agent import add, cand
from backend.tests.topic_helpers import review_list

TODAY = date(2026, 10, 16)


class FakeFetcher:
    """Daily points over ``days`` days for the known keys, nothing for the others."""

    def __init__(self, histories: dict[str, int]):
        self.histories, self.calls = histories, []

    def __call__(self, source, external_id, start):
        self.calls.append(f"{source}:{external_id}")
        n = self.histories.get(f"{source}:{external_id}", 0)
        return {TODAY - timedelta(days=k): DayPoint(0.5) for k in range(1, n + 1)}


@pytest.fixture(autouse=True)
def fresh_cache():
    history_check._exhausted.clear()
    yield
    history_check._exhausted.clear()


@pytest.fixture()
def session():
    s = memory_session()
    for k in (2, 1):  # two stored days, like after the first daily runs
        add(s, TODAY - timedelta(days=k), "fed", "Fed cut in December 2026?")
    for k in range(1, SHORT_DAYS + 3):
        add(s, TODAY - timedelta(days=k), "long", "Fed cut in January 2027?")
    s.commit()
    return s


def test_only_relevant_short_histories_are_checked():
    pool = [cand("short", "Fed cut?", days=2), cand("long", "Fed cut 2?", days=30), cand("off", "Bitcoin up?", days=1),
            cand("pin", "Bitcoin down?", days=0)]
    assert [c.external_id for c in short_candidates(pool, ["fed"], ["polymarket:pin"], today=TODAY)] == ["short", "pin"]
    assert short_candidates(pool, ["fed"], limit=0, today=TODAY) == []


def test_short_history_is_extended_and_pool_sees_it(session):
    fetcher = FakeFetcher({"polymarket:fed": 300})
    pool = gather(session, [], ["fed"])
    summary = extend_history(session, pool, [], ["fed"], fetcher, today=TODAY)
    assert fetcher.calls == ["polymarket:fed"]  # the long history is not asked again
    assert summary["checked"] == summary["extended"] == 1 and summary["rows_added"] == 298
    assert summary["markets"][0]["first_date"] == (TODAY - timedelta(days=300)).isoformat()
    after = {c.external_id: c for c in gather(session, [], ["fed"])}
    assert after["fed"].days == 300 and after["fed"].first_date == TODAY - timedelta(days=300)
    rows = session.query(MarketSnapshot).filter_by(external_id="fed", exclusion_reason=BACKFILL).all()
    assert len(rows) == 298 and not any(r.eligible for r in rows)  # stored days stay untouched, never eligible


def test_live_only_market_gets_history_and_misses_are_not_asked_twice(session):
    live = [market(external_id="new", title="Fed cut in March 2027?"), market(external_id="none", title="Fed cut in June 2027?")]
    fetcher = FakeFetcher({"polymarket:new": 40})
    pool = gather(session, live, ["fed"])
    summary = extend_history(session, pool, live, ["fed"], fetcher, today=TODAY)
    assert sorted(fetcher.calls) == ["polymarket:fed", "polymarket:new", "polymarket:none"]
    assert summary["checked"] == 3 and summary["extended"] == 1 and summary["rows_added"] == 40
    assert session.query(MarketSnapshot).filter_by(external_id="new").count() == 40
    fetcher.calls.clear()
    extend_history(session, gather(session, live, ["fed"]), live, ["fed"], fetcher, today=TODAY)
    assert "polymarket:none" not in fetcher.calls and "polymarket:new" not in fetcher.calls


def test_failing_source_keeps_the_turn_alive(session):
    summary = extend_history(session, gather(session, [], ["fed"]), [], ["fed"], FakeFetcher({}), today=TODAY)
    assert summary == {"checked": 1, "extended": 0, "rows_added": 0, "markets": []}


def test_endpoint_reports_the_check_and_can_skip_it(session):
    fetcher = FakeFetcher({"polymarket:fed": 60})
    tid = review_list(session, [("polymarket", "fed", "Fed cut in December 2026?", "high", "accepted"),
                                ("polymarket", "long", "Fed cut in January 2027?", "high", "accepted")]).id
    app.dependency_overrides.update({get_session: lambda: session, agent_api.get_agent_markets: lambda: [],
                                     agent_api.get_agent: RuleAgent, agent_api.get_history_fetcher: lambda: fetcher})
    try:
        client = TestClient(app)
        skipped = client.post("/agent/blueprint", json={"topic_id": tid, "messages": [{"role": "user", "content": "Fed cut"}], "check_history": False}).json()
        assert skipped["history_check"] is None and fetcher.calls == []
        body = client.post("/agent/blueprint", json={"topic_id": tid, "messages": [{"role": "user", "content": "Fed cut"}]}).json()
        assert body["history_check"]["extended"] == 1 and body["history_check"]["rows_added"] > 0
        assert body["blueprint"]["history"]["start"] < (TODAY - timedelta(days=SHORT_DAYS + 2)).isoformat()
    finally:
        app.dependency_overrides.clear()
