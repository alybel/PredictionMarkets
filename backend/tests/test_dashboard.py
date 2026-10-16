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


def page_with_script(client) -> str:
    """Dashboard HTML plus the index view script it loads (moved out of the page in 1015-0912)."""
    html = client.get("/").text
    assert '<script src="/static/indices.js"></script>' in html
    return html + client.get("/static/indices.js").text


def test_dashboard_fetches_indices_on_demand(client):
    """The page pulls its data from the same two endpoints it renders."""
    html = page_with_script(client)
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
    html = page_with_script(client)

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


# --- shared design system (feature 0923-0740: look of FactorInvestingFund) ----------


def test_dashboard_uses_shared_dark_design_system(client):
    """Same colour tokens, topbar, tabs and card structure as FactorInvestingFund."""
    css = client.get("/static/style.css").text
    for token in ("--bg: #0f1419", "--card: #171d25", "--accent: #3b82f6", "--ok: #22c55e", "--bad: #ef4444"):
        assert token in css
    assert ".tabs {" in css and ".tab.active" in css and ".card-head" in css and ".dot.ok" in css

    html = client.get("/").text
    assert '<header class="topbar">' in html and 'id="health"' in html
    assert 'class="tab" data-view="indices"' in html and 'data-view-panel="indices"' in html
    assert "/static/shell.js" in html
    assert 'id="banner"' in html


def test_shell_script_wires_tabs_health_and_logging(client):
    js = client.get("/static/shell.js")
    assert js.status_code == 200
    assert 'fetch("/health")' in js.text and 'fetch("/client-log"' in js.text
    assert "function selectView" in js.text and "function logEvent" in js.text
    html = page_with_script(client)
    assert 'logEvent("info", "button.click", { id: "refresh"' in html  # clicks are logged


def test_health_reads_cache_without_fetching_markets(client, monkeypatch):
    from backend.app import api
    from backend.app.market_cache import MarketCache

    fetches = []
    monkeypatch.setattr(api, "market_cache", MarketCache(lambda: fetches.append(1) or MARKETS))

    body = client.get("/health").json()
    assert body["version"] == api.APP_VERSION
    assert body["indices"] == 1
    assert body["markets_cached"] is None and body["cache_age_s"] is None  # nothing cached yet
    assert body["eui_value"] is None and body["eui_value_date"] is None
    assert fetches == []  # health never triggers a market fetch

    api.market_cache.get()
    body = client.get("/health").json()
    assert body["markets_cached"] == 2 and body["cache_age_s"] >= 0


def test_client_log_lands_in_server_log(client, caplog):
    with caplog.at_level("INFO", logger="backend.app.client"):
        res = client.post("/client-log", json={"level": "info", "event": "button.click", "detail": {"id": "refresh"}})
    assert res.status_code == 204
    assert any("button.click" in r.getMessage() and "refresh" in r.getMessage() for r in caplog.records)


def test_client_log_rejects_empty_event(client):
    assert client.post("/client-log", json={"level": "info", "event": ""}).status_code == 422


def test_legacy_tabs_eui_and_manufacture_are_gone(client):
    """1015-2232: the legacy tabs and their scripts are removed; the backend endpoints stay."""
    html = client.get("/").text
    for gone in ('data-view="eui"', 'data-view-panel="eui"', "Economic Uncertainty Index", 'data-view="manufacture"',
                 'data-view-panel="manufacture"', "Index Manufacture", 'id="mf-', 'id="eui-', "/static/eui.js", "/static/manufacture.js"):
        assert gone not in html
    assert client.get("/static/eui.js").status_code == 404
    assert client.get("/static/manufacture.js").status_code == 404
    assert "eui_value" not in client.get("/static/shell.js").text
    assert "mfRenderCatalog" not in client.get("/static/fields.js").text


def test_constituents_are_visible_right_below_the_index_value(client):
    """1015-1111: the constituents table follows the value card, before history and correlation."""
    html = client.get("/").text

    card = html.index('id="constituents-card"')
    assert html.index('id="funnel"') < card < html.index("<h2>Wertentwicklung</h2>") < html.index('id="cr-card"')
    assert 'href="#constituents-card"' in html  # KPI jumps to the list
    assert 'id="constituents-summary"' in html
    assert 'data-key="rank"' in html  # rank by weight column


def test_constituents_sorted_by_weight_with_summary(client):
    """Largest weight first by default; summary per source and concentration; jump is logged."""
    js = client.get("/static/indices.js").text

    assert 'sortKey: "weight", sortDir: -1' in js
    assert "m.rank = i + 1" in js
    assert "renderConstituentSummary(data.markets)" in js
    assert '"kpi-constituents"' in js and "logEvent(" in js
    css = client.get("/static/style.css").text
    assert ".constituents-scroll" in css and "position: sticky" in css


def test_frontend_is_always_revalidated(client):
    """Regression bug-1015-2222: stale cached JS hid newly built metric indices; HTML and static files must revalidate."""
    for path in ("/", "/static/indices.js", "/static/metricindex.js", "/static/style.css"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    assert "cache-control" not in client.get("/health").headers
