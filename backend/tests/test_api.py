"""REST API: consolidated index value plus per-market details, no network."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.api import app, get_markets, get_session
from backend.app.clients.base import NormalizedMarket
from backend.app.db import Base, make_session_factory
from backend.app.engine import upsert_rule


def make_market(**overrides) -> NormalizedMarket:
    defaults = dict(
        source="polymarket",
        external_id="pm-1",
        title="Will the president win the election?",
        category="politics",
        price=0.5,
        volume=100.0,
        liquidity=1_000.0,
    )
    defaults.update(overrides)
    return NormalizedMarket(**defaults)


MARKETS = [
    make_market(external_id="a", price=0.2, liquidity=10_000.0),
    make_market(external_id="b", price=0.8, liquidity=30_000.0),
    make_market(external_id="c", category="sports", price=0.9, liquidity=50_000.0),
    make_market(external_id="d", liquidity=5.0),
    # Near-decided (outside 0.03-0.97) and already ended: excluded despite high liquidity.
    make_market(external_id="e", price=0.99, liquidity=99_000.0),
    make_market(external_id="f", liquidity=99_000.0, end_date=datetime.now(timezone.utc) - timedelta(days=1)),
]


@pytest.fixture()
def client():
    # TestClient serves requests from a worker thread; share one connection.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        upsert_rule(session, "political", categories=["politics"], min_liquidity=100.0)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_markets] = lambda: MARKETS
        yield TestClient(app)
    app.dependency_overrides.clear()


def test_index_returns_consolidated_value_and_details(client):
    body = client.get("/indices/political").json()

    assert body["index_name"] == "political"
    assert body["value"] == pytest.approx(0.65)
    assert body["method"] == "liquidity_weighted"
    assert body["market_count"] == 2
    assert body["total_liquidity"] == 40_000.0

    details = body["markets"]
    assert [m["external_id"] for m in details] == ["a", "b"]
    assert details[0]["price"] == 0.2
    assert details[0]["source"] == "polymarket"
    assert details[0]["title"]


def test_unknown_index_returns_404(client):
    response = client.get("/indices/missing")
    assert response.status_code == 404
    assert "missing" in response.json()["detail"]


def test_indices_lists_stored_rules(client):
    body = client.get("/indices").json()
    assert body == [
        {
            "index_name": "political",
            "categories": ["politics"],
            "keywords": [],
            "min_liquidity": 10_000.0,  # requested 100, lifted to the global floor
            "min_price": 0.03,
            "max_price": 0.97,
        }
    ]
