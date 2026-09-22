"""Regression: the market population was tiny (100 unsorted markets per source,
no tags, narrow filters). Covers pagination, Kalshi events, tags, the global
liquidity floor, the filter funnel and the market cache. No live API calls.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app import api
from backend.app.api import app, get_session
from backend.app.clients import kalshi, polymarket
from backend.app.clients.base import NormalizedMarket
from backend.app.db import Base, make_session_factory
from backend.app.engine import apply_funnel, market_matches, upsert_rule
from backend.app.market_cache import MarketCache
from backend.app.models import MIN_LIQUIDITY_USD, IndexRule
from backend.tests.test_clients import FakeResponse

# --- fakes ----------------------------------------------------------------


class PagedSession:
    """Serves one payload per call and records the query params."""

    def __init__(self, pages: list):
        self._pages = list(pages)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        return FakeResponse(self._pages.pop(0) if self._pages else None)


def poly_event(liquidity: float, *markets: dict, tags=("Politics",), slug="ev") -> dict:
    return {"liquidity": str(liquidity), "slug": slug, "tags": [{"label": t} for t in tags], "markets": list(markets)}


def poly_market(id_: str, liquidity: float, question: str = "Will X happen?") -> dict:
    return {"id": id_, "question": question, "liquidityNum": liquidity, "slug": f"m-{id_}"}


def kalshi_market(ticker: str, **fields) -> dict:
    return {"ticker": ticker, "title": f"{ticker}?", **fields}


def market(**overrides) -> NormalizedMarket:
    defaults = dict(
        source="polymarket", external_id="x", title="Will Trump win the election?",
        category=None, tags=["Politics", "US Election"], price=0.5, volume=1.0, liquidity=50_000.0,
    )
    defaults.update(overrides)
    return NormalizedMarket(**defaults)


def rule(**overrides) -> IndexRule:
    defaults = dict(index_name="i", categories=[], keywords=[], min_liquidity=MIN_LIQUIDITY_USD)
    defaults.update(overrides)
    return IndexRule(**defaults)


# --- Polymarket: pagination by liquidity, stop below the floor -------------


def test_polymarket_pages_until_liquidity_drops_below_floor(monkeypatch):
    monkeypatch.setattr(polymarket, "PAGE_SIZE", 2)
    session = PagedSession([
        [poly_event(90_000, poly_market("a", 60_000), poly_market("b", 5_000)), poly_event(50_000, poly_market("c", 50_000))],
        [poly_event(20_000, poly_market("d", 20_000)), poly_event(9_999, poly_market("e", 9_999))],
        [poly_event(15_000, poly_market("never", 15_000))],  # must not be requested
    ])
    markets = polymarket.PolymarketClient(session=session).fetch_markets()

    assert [m.external_id for m in markets] == ["a", "c", "d"]  # b: below floor, e: event below floor
    assert [c["offset"] for c in session.calls] == [0, 2]
    assert all(c["order"] == "liquidity" and c["ascending"] == "false" for c in session.calls)


def test_polymarket_stops_on_short_page_and_caps_pages(monkeypatch):
    monkeypatch.setattr(polymarket, "PAGE_SIZE", 2)
    short = PagedSession([[poly_event(50_000, poly_market("a", 50_000))]])
    assert len(polymarket.PolymarketClient(session=short).fetch_markets()) == 1
    assert len(short.calls) == 1

    endless = PagedSession([[poly_event(50_000, poly_market(str(i), 50_000)) for i in range(2)]] * 50)
    client = polymarket.PolymarketClient(session=endless)
    client.max_pages = 3
    assert len(client.fetch_markets()) == 6
    assert len(endless.calls) == 3


def test_polymarket_markets_inherit_event_tags():
    session = PagedSession([[poly_event(50_000, poly_market("a", 50_000), tags=("Politics", "Iran", "Politics"))]])
    (m,) = polymarket.PolymarketClient(session=session).fetch_markets()
    assert m.tags == ["Politics", "Iran"]  # deduplicated
    assert m.category == "Politics"
    assert m.url == "https://polymarket.com/market/m-a"


# --- Kalshi: /events with nested markets, cursor pagination, category tag ---


def test_kalshi_follows_cursor_and_tags_by_event_category():
    session = PagedSession([
        {"events": [{"category": "Politics", "markets": [kalshi_market("P1", liquidity_dollars="12000")]}], "cursor": "c2"},
        {"events": [{"category": "Economics", "markets": [kalshi_market("E1", liquidity_dollars="30000")]}], "cursor": ""},
    ])
    markets = kalshi.KalshiClient(session=session).fetch_markets()

    assert [(m.external_id, m.tags) for m in markets] == [("P1", ["Politics"]), ("E1", ["Economics"])]
    assert session.calls[0].get("cursor") is None
    assert session.calls[1]["cursor"] == "c2"
    assert all(c["with_nested_markets"] == "true" and c["status"] == "open" for c in session.calls)


def test_kalshi_page_cap_limits_requests():
    endless = PagedSession([{"events": [{"category": "World", "markets": [kalshi_market("W", liquidity_dollars="20000")]}], "cursor": "more"}] * 30)
    client = kalshi.KalshiClient(session=endless)
    client.max_pages = 4
    assert len(client.fetch_markets()) == 4
    assert len(endless.calls) == 4


def test_kalshi_liquidity_falls_back_to_open_interest():
    session = PagedSession([{"events": [{"category": "Economics", "markets": [
        kalshi_market("OI", liquidity_dollars="0.0000", open_interest_fp="25000.50", last_price_dollars="0.3100"),
        kalshi_market("LOW", liquidity_dollars="0.0000", open_interest_fp="100.00"),
        kalshi_market("REP", liquidity_dollars="15000.0000", open_interest_fp="1.00"),
    ]}]}])
    markets = {m.external_id: m for m in kalshi.KalshiClient(session=session).fetch_markets()}

    assert set(markets) == {"OI", "REP"}
    assert markets["OI"].liquidity == pytest.approx(25_000.5)
    assert markets["OI"].price == pytest.approx(0.31)
    assert markets["REP"].liquidity == pytest.approx(15_000.0)  # reported liquidity wins


# --- rule engine: tags, keywords, floor, funnel -----------------------------


def test_tag_filter_matches_any_tag_case_insensitive():
    r = rule(categories=["us election", "Economics"])
    assert market_matches(r, market(tags=["Politics", "US Election"]))
    assert market_matches(r, market(tags=[], category="ECONOMICS"))  # legacy category counts as tag
    assert not market_matches(r, market(tags=["Sports"]))
    assert not market_matches(r, market(tags=[], category=None))


def test_keywords_remain_additional_title_restriction():
    r = rule(categories=["Politics"], keywords=["ceasefire", "iran"])
    assert market_matches(r, market(title="Will the Iran ceasefire hold?"))
    assert not market_matches(r, market(title="Will Trump win?"))  # right tag, wrong title
    assert market_matches(rule(), market(title="anything", tags=[]))  # empty lists: no restriction


def test_rule_liquidity_cannot_undercut_global_floor():
    r = rule(min_liquidity=0.0)
    assert not market_matches(r, market(liquidity=9_999.0))
    assert market_matches(r, market(liquidity=MIN_LIQUIDITY_USD))
    assert not market_matches(rule(min_liquidity=20_000.0), market(liquidity=15_000.0))


def test_funnel_counts_survivors_per_stage_in_order():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    markets = [
        market(external_id="ok"),
        market(external_id="tag", tags=["Sports"]),
        market(external_id="kw", title="Bitcoin above 100k?"),
        market(external_id="liq", liquidity=100.0),
        market(external_id="price", price=0.99),
        market(external_id="ended", end_date=past),
    ]
    r = rule(categories=["Politics"], keywords=["trump"], min_price=0.03, max_price=0.97)
    selected, funnel = apply_funnel(r, markets)

    assert [m.external_id for m in selected] == ["ok"]
    assert list(funnel) == ["loaded", "tags", "keywords", "liquidity", "price", "end_date"]
    assert funnel == {"loaded": 6, "tags": 5, "keywords": 4, "liquidity": 3, "price": 2, "end_date": 1}


# --- cache -------------------------------------------------------------------


def test_cache_reuses_snapshot_within_ttl_and_refetches_on_force():
    clock = {"t": 0.0}
    calls = []
    cache = MarketCache(lambda: calls.append(1) or [len(calls)], ttl_seconds=60.0, clock=lambda: clock["t"])

    assert cache.get() == [1]
    clock["t"] = 30.0
    assert cache.get() == [1]  # still fresh
    assert cache.get(force=True) == [2]  # refresh button
    clock["t"] = 91.0
    assert cache.get() == [3]  # expired
    assert len(calls) == 3


# --- API + dashboard -----------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    fetches = []
    monkeypatch.setattr(api, "market_cache", MarketCache(lambda: fetches.append(1) or [market(), market(external_id="s", tags=["Sports"])]))
    with make_session_factory(engine)() as session:
        upsert_rule(session, "global-politics", categories=["Politics"], keywords=["trump"])
        app.dependency_overrides[get_session] = lambda: session
        yield TestClient(app), fetches
    app.dependency_overrides.clear()


def test_api_returns_funnel_and_tags_and_uses_cache(client):
    test_client, fetches = client
    body = test_client.get("/indices/global-politics").json()

    assert body["funnel"] == {"loaded": 2, "tags": 1, "keywords": 1, "liquidity": 1, "price": 1, "end_date": 1}
    assert body["min_liquidity_floor"] == MIN_LIQUIDITY_USD
    assert body["markets"][0]["tags"] == ["Politics", "US Election"]
    assert body["value"] == pytest.approx(0.5) and body["market_count"] == 1  # existing payload intact

    test_client.get("/indices/global-politics")
    assert len(fetches) == 1  # second call served from cache
    test_client.get("/indices/global-politics?refresh=1")
    assert len(fetches) == 2  # refresh forces a new fetch


def test_dashboard_shows_funnel_tags_and_forced_refresh(client):
    html = client[0].get("/").text
    assert 'id="funnel"' in html and "renderFunnel(data.funnel)" in html
    assert "nach Preis-Korridor" in html and "nach Enddatum" in html
    assert "marketTags(m).join" in html  # tags rendered in the category column
    assert '"?refresh=1"' in html  # refresh button bypasses the server cache
