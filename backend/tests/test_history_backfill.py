"""Historical data (1015-0953): backfill past daily snapshots from the sources' price histories. No network."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.backfill import sources
from backend.app.backfill.api import get_history_fetcher
from backend.app.backfill.service import BACKFILL, backfill_markets, index_markets
from backend.app.backfill.sources import (DayPoint, candle_price, fetch_history, parse_kalshi_candles,
                                          parse_polymarket_history, yes_token)
from backend.app.engine import get_rule, upsert_rule
from backend.app.eui.models import MarketSnapshot
from backend.app.eui.snapshot import snapshot_rows
from backend.tests.eui_helpers import memory_session
from backend.tests.test_index_builder import snap

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TODAY = datetime.now(timezone.utc).date()  # the backfill dates by UTC
DAY = lambda n: TODAY - timedelta(days=n)  # noqa: E731
TS = lambda d: int(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).timestamp()) + 60  # noqa: E731


class FakeFetcher:
    def __init__(self, points: dict):
        self.points, self.calls = points, []

    def __call__(self, source, external_id, start):
        self.calls.append((source, external_id, start))
        return self.points.get(external_id, {})


# ---- source parsers -----------------------------------------------------------------

def test_polymarket_history_is_one_price_per_utc_day():
    payload = {"history": [{"t": TS(DAY(2)), "p": 0.4}, {"t": TS(DAY(1)), "p": 0.5}, {"t": TS(DAY(1)) + 3600, "p": 0.55},
                           {"t": TS(DAY(1)), "p": None}, "junk"]}
    assert parse_polymarket_history(payload) == {DAY(2): DayPoint(0.4), DAY(1): DayPoint(0.55)}
    assert parse_polymarket_history(None) == {}


def test_yes_token_reads_the_json_encoded_list():
    assert yes_token({"clobTokenIds": '["111", "222"]'}) == "111"
    assert yes_token({"clobTokenIds": ["9"]}) == "9"
    assert yes_token({"clobTokenIds": "not json"}) is None and yes_token([]) is None


def test_kalshi_candles_give_price_and_usd_volume():
    candles = {"candlesticks": [
        {"end_period_ts": TS(DAY(3)), "price": {"close_dollars": "0.40"}, "volume_fp": "1000"},
        {"end_period_ts": TS(DAY(2)), "price": {"previous_dollars": "0.40"}, "volume_fp": "0",
         "yes_bid": {"close_dollars": "0.30"}, "yes_ask": {"close_dollars": "0.50"}},
        {"end_period_ts": TS(DAY(1)), "price": {"previous_dollars": "0.45"}},
        {"end_period_ts": TS(DAY(0)), "price": {}},
    ]}
    out = parse_kalshi_candles(candles)
    assert out[DAY(3)] == DayPoint(0.4, pytest.approx(400.0))
    assert out[DAY(2)].price == pytest.approx(0.4) and out[DAY(2)].volume_usd == 0.0  # bid/ask midpoint
    assert out[DAY(1)].price == pytest.approx(0.45)  # previous close
    assert DAY(0) not in out
    assert candle_price({"price": None}) is None


def test_fetch_history_never_raises(monkeypatch):
    def boom(*_):
        raise ValueError("bad json")
    monkeypatch.setitem(sources.FETCHERS, "kalshi", boom)
    assert fetch_history("kalshi", "KX-1", DAY(5)) == {}
    assert fetch_history("unknown", "x", DAY(5)) == {}


# ---- backfill service ---------------------------------------------------------------

@pytest.fixture()
def session():
    s = memory_session()
    upsert_rule(s, "econ", categories=["economics"])
    snap(s, TODAY, "a", price=0.3, volume_24h=50_000.0)
    snap(s, TODAY, "b", price=0.6, volume_24h=20_000.0)
    snap(s, TODAY, "c", tags=("Sports",), price=0.5)
    snap(s, DAY(1), "a", price=0.31, volume_24h=40_000.0)  # real snapshot: must stay untouched
    return s


def test_index_markets_are_todays_constituents_by_volume(session):
    rows = index_markets(session, get_rule(session, "econ"))
    assert [r.external_id for r in rows] == ["a", "b"]
    assert [r.external_id for r in index_markets(session, get_rule(session, "econ"), limit=1)] == ["a"]
    assert index_markets(memory_session(), get_rule(session, "econ")) == []


def test_backfill_adds_missing_past_days_only(session):
    templates = index_markets(session, get_rule(session, "econ"))
    fetcher = FakeFetcher({
        "a": {DAY(3): DayPoint(0.2), DAY(1): DayPoint(0.9), TODAY: DayPoint(0.99), DAY(400): DayPoint(0.1)},
        "b": {DAY(3): DayPoint(0.5, 12_000.0)},
    })
    summary = backfill_markets(session, templates, days=30, fetcher=fetcher, today=TODAY)

    assert summary == {"markets": 2, "markets_with_history": 2, "rows_added": 2, "days_added": 1}
    assert {c[2] for c in fetcher.calls} == {DAY(30)}
    day3 = {r.external_id: r for r in snapshot_rows(session, DAY(3))}
    assert day3["a"].price == 0.2 and day3["a"].volume_24h_usd == 50_000.0  # Polymarket: current volume as proxy
    assert day3["b"].volume_24h_usd == 12_000.0 and day3["b"].title == "Fed cuts rates?"
    assert all(r.exclusion_reason == BACKFILL and not r.eligible for r in day3.values())
    assert snapshot_rows(session, DAY(1))[0].price == 0.31  # existing row kept
    assert len(snapshot_rows(session, TODAY)) == 3  # today belongs to the daily run

    again = backfill_markets(session, templates, days=30, fetcher=fetcher, today=TODAY)
    assert again["rows_added"] == 0  # idempotent


def test_templates_come_from_the_latest_real_snapshot_not_from_backfilled_days():
    s = memory_session()
    upsert_rule(s, "econ", categories=["economics"])
    snap(s, DAY(10), "a")
    snap(s, DAY(10), "b")
    backfill_markets(s, index_markets(s, get_rule(s, "econ"))[:1], days=30, fetcher=FakeFetcher({"a": {DAY(2): DayPoint(0.4)}}),
                     today=TODAY)
    assert [r.snapshot_date for r in index_markets(s, get_rule(s, "econ"))] == [DAY(10), DAY(10)]


def test_backfill_without_history_writes_nothing(session):
    summary = backfill_markets(session, index_markets(session, get_rule(session, "econ")), fetcher=FakeFetcher({}))
    assert summary["rows_added"] == 0
    assert session.query(MarketSnapshot).filter_by(exclusion_reason=BACKFILL).count() == 0


# ---- API + history -------------------------------------------------------------------

@pytest.fixture()
def client(session):
    fetcher = FakeFetcher({"a": {DAY(5): DayPoint(0.1), DAY(4): DayPoint(0.2)}, "b": {DAY(5): DayPoint(0.7, 30_000.0)}})
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    app.dependency_overrides[get_history_fetcher] = lambda: fetcher
    yield TestClient(app), fetcher
    app.dependency_overrides.clear()


def test_backfill_endpoint_extends_the_index_history(client):
    http, _ = client
    assert http.get("/indices/econ/history").json()["points"][0]["date"] == DAY(1).isoformat()

    body = http.post("/indices/econ/history/backfill?days=30").json()
    assert body["rows_added"] == 3 and body["days_added"] == 2
    assert body["status"]["first_date"] == DAY(5).isoformat() and body["status"]["backfilled_rows"] == 3

    points = http.get("/indices/econ/history").json()["points"]
    assert [p["date"] for p in points][:2] == [DAY(5).isoformat(), DAY(4).isoformat()]
    # day 5: (0.1 * 50k + 0.7 * 30k) / 80k
    assert points[0]["value"] == pytest.approx(0.325) and points[0]["market_count"] == 2
    assert http.get("/history/status").json()["snapshot_days"] == 4


def test_backfill_endpoint_uses_unsaved_rules_and_rejects_unknown_index(client):
    http, fetcher = client
    body = http.post("/indices/draft/history/backfill?limit=1", json={"categories": ["economics"]}).json()
    assert body["markets"] == 1 and [c[1] for c in fetcher.calls] == ["a"]
    assert http.post("/indices/nope/history/backfill").status_code == 404
    assert http.post("/indices/econ/history/backfill?days=0").status_code == 422


def test_frontend_offers_the_backfill_button():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "builder.js").read_text()
    assert 'id="ib-backfill"' in html
    assert "/history/backfill" in js and "builder.backfill.done" in js and "builder.backfill.failed" in js
