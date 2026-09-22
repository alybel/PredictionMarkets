"""On-demand dashboard: static frontend plus timestamp in the API, no network."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.api import app, get_markets, get_session
from backend.app.clients.base import NormalizedMarket
from backend.app.db import Base, make_session_factory
from backend.app.engine import upsert_rule

MARKETS = [
    NormalizedMarket(
        source="polymarket",
        external_id="pm-1",
        title="Will the president win the election?",
        category="politics",
        price=0.6,
        volume=100.0,
        liquidity=10_000.0,
    ),
    NormalizedMarket(
        source="kalshi",
        external_id="ks-1",
        title="Recession declared this year?",
        category="politics",
        price=0.2,
        volume=50.0,
        liquidity=30_000.0,
    ),
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


def test_root_serves_dashboard_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "PredictionMarketIndex" in html
    assert 'id="index-buttons"' in html  # index selection triggers the fetch
    assert 'id="markets"' in html  # per-market details table


def test_dashboard_fetches_indices_on_demand(client):
    """The page pulls its data from the same two endpoints it renders."""
    html = client.get("/").text
    assert 'fetch("/indices")' in html
    assert 'fetch("/indices/"' in html


def test_dashboard_has_interactive_controls(client):
    """Filter input, source select, refresh button and sortable headers are served."""
    html = client.get("/").text

    assert 'id="market-filter"' in html
    assert 'id="source-filter"' in html
    assert 'id="refresh"' in html
    for key in ("source", "title", "category", "price", "volume", "liquidity"):
        assert f'data-key="{key}"' in html  # every column header is sortable


def test_dashboard_wires_interactivity_client_side(client):
    """Sorting/filtering re-render locally; refresh re-fetches the active index."""
    html = client.get("/").text

    assert "renderTable()" in html
    assert 'addEventListener("input"' in html  # text filter
    assert 'addEventListener("change"' in html  # source filter
    assert "loadIndex(state.name" in html  # refresh re-fetches on demand


def test_index_response_carries_timestamp(client):
    body = client.get("/indices/political").json()

    as_of = datetime.fromisoformat(body["as_of"])
    assert as_of.tzinfo is not None
    # Existing consolidated payload stays intact next to the timestamp.
    assert body["value"] == pytest.approx(0.3)
    assert body["market_count"] == 2


def test_dashboard_uses_full_screen_width(client):
    """The main column is not capped to a fixed max-width (feature 0919-1720)."""
    css = client.get("/static/style.css").text
    main_rule = css[css.index("main {"):]
    main_rule = main_rule[: main_rule.index("}")]

    assert "max-width" not in main_rule
    assert "width: 100%" in main_rule
