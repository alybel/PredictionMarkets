"""Domains with full raw history (1016-2013): CRUD, membership, expired markets, idempotence, errors, point-in-time.

Sources and histories are fakes: no network, no LLM.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api import app, get_session
from backend.app.backfill.api import get_history_fetcher
from backend.app.backfill.sources import DayPoint
from backend.app.domains.api import get_domain_scanner, get_topic_source
from backend.app.domains.models import Domain, DomainMarket
from backend.app.domains.service import clean_definition, load_domain, matches, refresh_domains
from backend.app.eui.models import MarketSnapshot
from backend.app.quality.pit import BACKFILL, snapshot_violation
from backend.tests.eui_helpers import market, memory_session

TODAY = datetime.now(timezone.utc).date()
DAY = lambda n: TODAY - timedelta(days=n)  # noqa: E731
TREE = [{"name": "Economics", "events": 0, "markets": 3, "subtopics": [{"name": "Fed", "events": 0, "markets": 2},
                                                                     {"name": "Allgemein", "events": 0, "markets": 1}]}]
OPEN = market(source="kalshi", external_id="KXFED-27DEC", title="Fed rate cut in December 2027?", tags=["Economics", "Fed"], active=True)
SETTLED = market(source="kalshi", external_id="KXFED-25DEC", title="Fed rate cut in December 2025?", tags=["Economics", "Fed"],
                 active=False, end_date=datetime(2025, 12, 10, tzinfo=timezone.utc))
CLOSED_PM = market(source="polymarket", external_id="pm-9", title="Fed cuts rates in March 2025?", tags=["Economics", "Fed"], active=False)
OTHER = market(source="polymarket", external_id="pm-2", title="US GDP growth above 2%?", tags=["Economics", "Fed"])
HISTORY = {"KXFED-27DEC": {DAY(3): DayPoint(0.4, 100.0), DAY(2): DayPoint(0.45, 90.0)},
           "KXFED-25DEC": {DAY(400): DayPoint(0.2, 50.0), DAY(399): DayPoint(0.3, 60.0), DAY(397): DayPoint(0.9, 10.0)},
           "pm-9": {DAY(600): DayPoint(0.6), DAY(599): DayPoint(0.7)}}


class FakeScanner:
    def __init__(self, polymarket=(), kalshi=(), errors=None):
        self.result = {"polymarket": list(polymarket), "kalshi": list(kalshi)}
        self.errors, self.calls = errors or {}, []

    def __call__(self, topic, subtopic, terms):
        self.calls.append((topic, subtopic, terms))
        return {s: {"markets": list(m), "errors": self.errors.get(s, 0)} for s, m in self.result.items()}


class FakeFetcher:
    def __init__(self, points):
        self.points, self.calls = points, []

    def __call__(self, source, external_id, start):
        self.calls.append(external_id)
        return dict(self.points.get(external_id, {}))


def domain(session, **kw) -> Domain:
    d = Domain(**{**clean_definition("Fed", "Economics", "Fed", ["fed"]), **kw})
    session.add(d)
    session.commit()
    return d


def rows(session) -> list[tuple]:
    return sorted(session.execute(select(MarketSnapshot.external_id, MarketSnapshot.snapshot_date, MarketSnapshot.price,
                                         MarketSnapshot.exclusion_reason, MarketSnapshot.id)).all())


@pytest.fixture()
def api():
    session = memory_session()
    scanner, fetcher = FakeScanner(polymarket=[CLOSED_PM, OTHER], kalshi=[OPEN, SETTLED]), FakeFetcher(HISTORY)
    app.dependency_overrides.update({get_session: lambda: session, get_topic_source: lambda: (lambda: TREE),
                                     get_domain_scanner: lambda: scanner, get_history_fetcher: lambda: fetcher})
    yield TestClient(app), session
    app.dependency_overrides.clear()


# ---- CRUD via API -------------------------------------------------------------------------

def test_create_read_update_delete_domain(api, caplog):
    client, session = api
    caplog.set_level(logging.INFO)
    created = client.post("/domains", json={"name": " US-Geldpolitik ", "topic": "Economics", "subtopic": "Fed", "terms": ["fed", " ", "FED", "rate"]})
    assert created.status_code == 201
    body = created.json()
    assert (body["name"], body["topic"], body["subtopic"], body["terms"]) == ("US-Geldpolitik", "Economics", "Fed", ["fed", "rate"])
    assert (body["markets_active"], body["markets_expired"], body["first_date"], body["last_run_at"]) == (0, 0, None, None)
    assert client.get(f"/domains/{body['id']}").json()["name"] == "US-Geldpolitik"

    changed = client.put(f"/domains/{body['id']}", json={"name": "Fed", "topic": "Economics", "subtopic": "Allgemein", "terms": []})
    assert changed.status_code == 200 and (changed.json()["name"], changed.json()["subtopic"], changed.json()["terms"]) == ("Fed", None, [])
    assert [d["name"] for d in client.get("/domains").json()] == ["Fed"]

    assert client.delete(f"/domains/{body['id']}").status_code == 204
    assert client.get("/domains").json() == [] and client.get(f"/domains/{body['id']}").status_code == 404
    log = caplog.text
    assert "created" in log and "updated" in log and "deleted" in log


@pytest.mark.parametrize("payload, status", [
    ({"name": "  ", "topic": "Economics"}, 400),
    ({"name": "X", "topic": "Astrology"}, 400),
    ({"name": "X", "topic": "Economics", "subtopic": "Mars"}, 400),
    ({"name": "X", "topic": ""}, 400),
])
def test_invalid_definitions_are_rejected_with_400(api, payload, status):
    client, _ = api
    assert client.post("/domains", json=payload).status_code == status
    assert client.get("/domains").json() == []


def test_duplicate_name_is_409_and_unknown_domain_404(api):
    client, _ = api
    assert client.post("/domains", json={"name": "Fed", "topic": "Economics"}).status_code == 201
    assert client.post("/domains", json={"name": "fed", "topic": "Economics"}).status_code == 409
    assert client.put("/domains/99", json={"name": "Y", "topic": "Economics"}).status_code == 404
    assert client.post("/domains/99/load").status_code == 404


# ---- membership ---------------------------------------------------------------------------

def test_market_with_topic_and_term_belongs_market_without_term_hit_does_not():
    d = Domain(**clean_definition("Fed", "Economics", "Fed", ["fed"]))
    assert matches(d, OPEN) and matches(d, CLOSED_PM)
    assert not matches(d, OTHER)  # right topic, no term hit
    assert matches(d, market(tags=["Economy", "Fed"], title="Fed chair confirmed?"))  # subtopic tag under another topic label
    assert not matches(d, market(tags=["Economics"], title="Fed hike?"))  # subtopic missing
    whole = Domain(**clean_definition("All", "economics", None, []))
    assert matches(whole, OTHER) and not matches(whole, market(tags=["Politics"], title="Fed?"))  # no subtopic: topic tag


# ---- load run -----------------------------------------------------------------------------

def test_expired_markets_of_both_sources_are_loaded_with_history(api, caplog):
    client, session = api
    caplog.set_level(logging.INFO)
    d = client.post("/domains", json={"name": "Fed", "topic": "Economics", "subtopic": "Fed", "terms": ["fed"]}).json()
    res = client.post(f"/domains/{d['id']}/load").json()
    assert res["run"]["new_markets"] == 3 and res["run"]["loaded"] == 3 and res["run"]["rows_added"] == 7
    assert res["run"]["sources"]["kalshi"]["rows_added"] == 5 and res["run"]["sources"]["polymarket"]["rows_added"] == 2
    assert (res["markets_active"], res["markets_expired"]) == (1, 2)
    assert (res["first_date"], res["last_date"]) == (DAY(600).isoformat(), DAY(2).isoformat())
    assert (res["gap_days"], res["markets_with_gaps"]) == (1, 1)  # KXFED-25DEC misses DAY(398)
    stored = {(r[0], r[1]): r[2] for r in rows(session)}
    assert stored[("KXFED-25DEC", DAY(400))] == 0.2 and stored[("pm-9", DAY(599))] == 0.7
    assert "pm-2" not in {r[0] for r in rows(session)}
    assert "load run finished" in caplog.text and "'rows_added': 7" in caplog.text


def test_second_run_without_new_data_adds_and_changes_nothing():
    session = memory_session()
    d = domain(session)
    scanner, fetcher = FakeScanner(polymarket=[CLOSED_PM], kalshi=[OPEN, SETTLED]), FakeFetcher(HISTORY)
    load_domain(session, d, scanner, fetcher, TODAY)
    before = rows(session)
    second = load_domain(session, d, scanner, fetcher, TODAY)
    assert second["rows_added"] == 0 and second["new_markets"] == 0 and rows(session) == before
    assert len(fetcher.calls) == 3  # loaded members are not asked again


def test_existing_rows_are_never_overwritten():
    session = memory_session()
    d = domain(session)
    load_domain(session, d, FakeScanner(kalshi=[OPEN]), FakeFetcher(HISTORY), TODAY)
    session.query(DomainMarket).update({DomainMarket.loaded_at: None})  # force a reload with different source values
    session.commit()
    before = rows(session)
    changed = {"KXFED-27DEC": {DAY(3): DayPoint(0.99, 1.0), DAY(2): DayPoint(0.99, 1.0), DAY(1): DayPoint(0.5, 1.0)}}
    load_domain(session, d, FakeScanner(kalshi=[OPEN]), FakeFetcher(changed), TODAY)
    after = rows(session)
    assert [r for r in after if r[1] != DAY(1)] == before and len(after) == len(before) + 1


def test_new_market_gets_full_history_on_next_daily_run():
    session = memory_session()
    d = domain(session)
    scanner = FakeScanner(kalshi=[OPEN])
    refresh_domains(session, scanner, FakeFetcher(HISTORY), TODAY)
    scanner.result["kalshi"].append(SETTLED)  # appears at the source the next day
    out = refresh_domains(session, scanner, FakeFetcher(HISTORY), TODAY)
    assert out["Fed"]["new_markets"] == 1 and out["Fed"]["rows_added"] == 3 and out["Fed"]["trigger"] == "daily"
    days = session.scalars(select(MarketSnapshot.snapshot_date).where(MarketSnapshot.external_id == "KXFED-25DEC")).all()
    assert sorted(days) == [DAY(400), DAY(399), DAY(397)]
    assert d.last_run["new_markets"] == 1


def test_failing_or_empty_source_does_not_stop_the_run(caplog):
    session = memory_session()
    d = domain(session)
    caplog.set_level(logging.INFO)
    scanner = FakeScanner(polymarket=[], kalshi=[OPEN, SETTLED], errors={"polymarket": 2})
    res = load_domain(session, d, scanner, FakeFetcher({"KXFED-27DEC": HISTORY["KXFED-27DEC"]}), TODAY)  # settled: empty history
    assert res["sources"]["polymarket"]["errors"] == 2 and res["sources"]["kalshi"]["without_history"] == 1
    assert res["rows_added"] == 2 and res["loaded"] == 1
    member = session.scalar(select(DomainMarket).where(DomainMarket.external_id == "KXFED-25DEC"))
    assert member.loaded_at is None and member.attempts == 1  # retried on the next run
    assert "'errors': 2" in caplog.text


def test_crashing_domain_never_stops_the_daily_run():
    session = memory_session()
    domain(session, name="A")
    domain(session, name="B")

    def scanner(topic, subtopic, terms):
        if not hasattr(scanner, "done"):
            scanner.done = True
            raise RuntimeError("source down")
        return {"kalshi": {"markets": [OPEN], "errors": 0}}

    out = refresh_domains(session, scanner, FakeFetcher(HISTORY), TODAY)
    assert "source down" in out["A"]["error"] and out["B"]["rows_added"] == 2


# ---- point in time ------------------------------------------------------------------------

def test_loaded_rows_carry_backfill_mark_and_pass_lookahead_check():
    session = memory_session()
    load_domain(session, domain(session), FakeScanner(polymarket=[CLOSED_PM], kalshi=[OPEN, SETTLED]), FakeFetcher(HISTORY), TODAY)
    stored = session.scalars(select(MarketSnapshot)).all()
    assert stored and all(r.exclusion_reason == BACKFILL and r.eligible is False for r in stored)
    assert all(snapshot_violation(r.snapshot_date, r.fetched_at, r.exclusion_reason, TODAY) is None for r in stored)
    assert session.scalar(select(func.max(MarketSnapshot.snapshot_date))) < TODAY  # today stays with the daily snapshot


# ---- schema and frontend ------------------------------------------------------------------

def test_migration_creates_domain_tables(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    db_url = f"sqlite:///{tmp_path / 'd.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    inspector = inspect(create_engine(db_url))
    assert {"domains", "domain_markets"} <= set(inspector.get_table_names())
    assert {"topic", "subtopic", "terms", "last_run"} <= {c["name"] for c in inspector.get_columns("domains")}


def test_dashboard_serves_domain_tab_with_logged_actions(api):
    client, _ = api
    html = client.get("/").text
    assert 'data-view="domains"' in html and 'data-view-panel="domains"' in html and "/static/domains.js" in html
    js = client.get("/static/domains.js").text
    for event in ("domains.create", "domains.update", "domains.delete", "domains.load", "domains.edit"):
        assert f'"{event}"' in js
