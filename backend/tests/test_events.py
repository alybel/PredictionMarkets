"""Events: outcome markets of the same source event bundled; existing endpoints unchanged. No network."""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from backend.app.api import app, get_markets, get_session
from backend.app.clients import KalshiClient, PolymarketClient
from backend.app.engine import upsert_rule
from backend.app.events import models as event_models  # noqa: F401  (register tables)
from backend.app.events.service import group_markets, sync_events
from backend.app.eui.snapshot import take_snapshot
from tests.eui_helpers import FakeClient, market, memory_session
from tests.test_clients import FakeSession, kalshi_events, poly_events

BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
ELECTION = "Who will win the 2028 election?"
CANDIDATES = [market(external_id=f"pm-{i}", title=f"Will candidate {i} win the 2028 election?", price=p, event_key="ev-2028",
                     event_title=ELECTION, outcome_label=f"Candidate {i}") for i, p in ((1, 0.5), (2, 0.3), (3, 0.2))]
SOLO = market(external_id="solo", title="Will the Fed cut rates in December?")
# Field set of an index market before events existed (GET /indices/{name}).
INDEX_MARKET_FIELDS = {"source", "external_id", "title", "category", "price", "volume", "liquidity", "end_date", "url", "tags",
                       "fetched_at", "volume_24h", "best_bid", "best_ask", "active", "description", "liquidity_basis", "depth"}


def test_three_candidates_form_one_event_and_solo_market_stays_standalone():
    groups, standalone = group_markets([*CANDIDATES, SOLO, CANDIDATES[0]])
    assert list(groups) == [("polymarket", "ev-2028")]
    assert [m.external_id for m in groups[("polymarket", "ev-2028")]] == ["pm-1", "pm-2", "pm-3"]
    assert standalone == [SOLO]


@pytest.fixture()
def client():
    session = memory_session()
    upsert_rule(session, "fed", categories=["Economics"], min_liquidity=100.0)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: [*CANDIDATES, SOLO]
    yield TestClient(app), session
    app.dependency_overrides.clear()


def test_sync_endpoint_and_events_api_deliver_one_event_with_three_outcomes(client):
    api, session = client
    assert api.post("/events/sync").json() == {"events": 1, "created": 1, "outcomes": 3, "standalone": 1}
    assert api.post("/events/sync").json()["created"] == 0  # idempotent
    body = api.get("/events").json()
    assert body["total"] == 1
    event = body["items"][0]
    assert (event["title"], event["outcome_count"]) == (ELECTION, 3)
    assert [o["outcome_label"] for o in event["outcomes"]] == ["Candidate 1", "Candidate 2", "Candidate 3"]
    assert "solo" not in {o["external_id"] for o in event["outcomes"]}
    assert api.get(f"/events/{event['id']}").json() == event
    assert api.get("/events/999").status_code == 404
    assert api.get("/events", params={"min_outcomes": 4}).json()["total"] == 0


def test_existing_index_endpoint_keeps_its_market_fields(client):
    api, _ = client
    markets = api.get("/indices/fed").json()["markets"]
    assert markets and all(set(m) == INDEX_MARKET_FIELDS for m in markets)


def test_daily_snapshot_bundles_events():
    session = memory_session()
    take_snapshot(session, clients=[FakeClient("polymarket", [*CANDIDATES, SOLO])], snapshot_date=date(2026, 10, 16), now=NOW)
    rows = session.execute(text("SELECT e.title, count(*) FROM events e JOIN event_markets m ON m.event_id = e.id GROUP BY e.id")).all()
    assert rows == [(ELECTION, 3)]


def test_outcome_moves_when_source_reassigns_it():
    session = memory_session()
    sync_events(session, CANDIDATES, NOW)
    moved = market(external_id="pm-3", title="Will candidate 3 win?", event_key="ev-other", event_title="Other")
    sync_events(session, [moved], NOW)
    assert session.execute(text("SELECT e.event_key FROM event_markets m JOIN events e ON e.id = m.event_id "
                                "WHERE m.external_id = 'pm-3'")).scalar() == "ev-other"


def test_clients_deliver_event_key_title_and_outcome_label():
    poly = PolymarketClient(session=FakeSession(poly_events(
        [{"id": "1", "question": "Will A win?", "groupItemTitle": "A", "outcomePrices": '["0.6","0.4"]', "volume24hr": 20000}],
        id="77", title=ELECTION)), liquidity_floor=None)
    pm = poly.fetch_markets()[0]
    assert (pm.event_key, pm.event_title, pm.outcome_label) == ("77", ELECTION, "A")
    kalshi = KalshiClient(session=FakeSession(kalshi_events([{"ticker": "EV-A", "title": "A?", "yes_sub_title": "A"}])), liquidity_floor=None)
    km = kalshi.fetch_markets()[0]
    assert (km.event_key, km.outcome_label) == ("EV", "A")


def _alembic(tmp_path, monkeypatch) -> tuple[Config, str]:
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    return Config(str(BACKEND_DIR / "alembic.ini")), db_url


def test_migration_up_and_down_on_fresh_db(tmp_path, monkeypatch):
    cfg, db_url = _alembic(tmp_path, monkeypatch)
    command.upgrade(cfg, "head")
    assert {"events", "event_markets"} <= set(inspect(create_engine(db_url)).get_table_names())
    command.downgrade(cfg, "0012")
    assert not {"events", "event_markets"} & set(inspect(create_engine(db_url)).get_table_names())
    command.upgrade(cfg, "head")
    assert {"events", "event_markets"} <= set(inspect(create_engine(db_url)).get_table_names())


def test_migration_keeps_rows_of_existing_db(tmp_path, monkeypatch):
    cfg, db_url = _alembic(tmp_path, monkeypatch)
    command.upgrade(cfg, "0012")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO index_rules (index_name, categories, keywords, min_liquidity, min_price, max_price, created_at) "
                          "VALUES ('legacy', '[]', '[]', 10000, 0.03, 0.97, '2026-10-01 00:00:00')"))
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT index_name FROM index_rules")).scalars().all() == ["legacy"]
        assert conn.execute(text("SELECT count(*) FROM events")).scalar() == 0
