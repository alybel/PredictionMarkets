"""Market links: every market carries a URL to its page on the source platform.

No live API calls: HTTP sessions are replaced by fakes.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.api import app, get_markets, get_session
from backend.app.clients.base import NormalizedMarket
from backend.app.clients.kalshi import KalshiClient
from backend.app.clients.polymarket import PolymarketClient
from backend.app.db import Base, make_session_factory
from backend.app.engine import upsert_rule
from backend.tests.test_clients import FakeSession, kalshi_events, poly_events


def polymarket_item(**overrides) -> dict:
    item = {"id": "1", "question": "Will X happen?", "slug": "will-x-happen", "liquidityNum": 20000}
    item.update(overrides)
    return item


def test_polymarket_url_from_slug():
    (market,) = PolymarketClient(session=FakeSession(poly_events([polymarket_item()]))).fetch_markets()
    assert market.url == "https://polymarket.com/market/will-x-happen"


def test_polymarket_url_falls_back_to_event_slug():
    payload = poly_events([polymarket_item(slug=None)], slug="x-event")
    (market,) = PolymarketClient(session=FakeSession(payload)).fetch_markets()
    assert market.url == "https://polymarket.com/event/x-event"


@pytest.mark.parametrize("event_slug", [None, ""])
def test_polymarket_url_none_without_slug(event_slug):
    payload = poly_events([polymarket_item(slug=None)], slug=event_slug)
    (market,) = PolymarketClient(session=FakeSession(payload)).fetch_markets()
    assert market.url is None


def test_kalshi_url_from_ticker():
    payload = kalshi_events([{"ticker": "FED-25DEC", "title": "Fed cuts rates?", "liquidity_dollars": "20000"}])
    (market,) = KalshiClient(session=FakeSession(payload)).fetch_markets()
    assert market.url == "https://kalshi.com/markets/FED-25DEC"


@pytest.fixture()
def client():
    # TestClient serves requests from a worker thread; share one connection.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    markets = [
        NormalizedMarket(
            source="polymarket",
            external_id="pm-1",
            title="Will the president win the election?",
            category="politics",
            price=0.6,
            volume=100.0,
            liquidity=20_000.0,
            url="https://polymarket.com/market/president-election",
        )
    ]
    with make_session_factory(engine)() as session:
        upsert_rule(session, "political", categories=["politics"], min_liquidity=100.0)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_markets] = lambda: markets
        yield TestClient(app)
    app.dependency_overrides.clear()


def test_api_delivers_market_url(client):
    (detail,) = client.get("/indices/political").json()["markets"]
    assert detail["url"] == "https://polymarket.com/market/president-election"


def test_dashboard_links_market_titles(client):
    html = client.get("/").text
    assert "marketTitle(m)" in html  # title cell renders the market link
    assert "m.url" in html
    assert 'rel = "noopener noreferrer"' in html
