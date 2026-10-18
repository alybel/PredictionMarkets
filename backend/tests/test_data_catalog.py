"""Data catalog (1016-1410): every stored market and stock with period, history and quality grade. No network."""

import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.catalog.grading import grade
from backend.app.catalog.markets import catalog_summary, list_markets, market_detail
from backend.app.catalog.stocks import list_stocks, stock_detail, stock_entry
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.stocks.models import StockPrice
from backend.app.stocks.universe import TOP_US_STOCKS
from backend.tests.eui_helpers import memory_session

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
START = date(2026, 3, 2)
TODAY = date(2026, 4, 1)


def add(session, day, ext, *, source="polymarket", title="Fed cuts rates?", price=0.5, volume=50_000.0):
    run = session.query(SnapshotRun).filter_by(snapshot_date=day).first() or SnapshotRun(snapshot_date=day, status="ok")
    session.add(run)
    session.flush()
    session.add(MarketSnapshot(run_id=run.id, snapshot_date=day, source=source, external_id=ext, title=title, tags=[],
                               price=price, volume_24h_usd=volume, url=f"https://x/{ext}",
                               fetched_at=datetime.combine(day, time(8), tzinfo=timezone.utc)))


def add_close(session, ticker, day, close):
    session.add(StockPrice(ticker=ticker, price_date=day, close=close, fetched_at=datetime.combine(day, time(23), tzinfo=timezone.utc)))


@pytest.fixture()
def session():
    s = memory_session()
    for i in range(10):                                   # clean, liquid, 10 consecutive days, current
        add(s, START + timedelta(days=i), "clean", title="Fed cuts rates in March?", price=0.40 + i * 0.01)
    for i in (0, 1, 5, 6, 7, 8, 9):                       # gaps, one jump, one missing price
        add(s, START + timedelta(days=i), "messy", source="kalshi", title="Oil above 100?",
            price=None if i == 6 else (0.9 if i == 5 else 0.2), volume=500.0)
    add(s, START, "short", title="One-day Fed market", price=0.3)
    for i in range(30):
        add_close(s, "AAPL", START + timedelta(days=i), 100 + i)
    add_close(s, "MSFT", START, 300)
    add_close(s, "MSFT", START + timedelta(days=1), 450)  # +50 % jump, short history, stale
    s.commit()
    return s


def test_grade_counts_issues():
    assert (grade([]), grade(["a"]), grade(["a", "b"])) == ("good", "fair", "poor")


def test_summary_per_source(session):
    assert catalog_summary(session) == [
        {"source": "kalshi", "markets": 1, "rows": 7, "first_date": "2026-03-02", "last_date": "2026-03-11"},
        {"source": "polymarket", "markets": 2, "rows": 11, "first_date": "2026-03-02", "last_date": "2026-03-11"}]


def test_market_list_grades_quality_and_latest_state(session):
    page = list_markets(session)
    assert page["total"] == 3 and page["latest_date"] == "2026-03-11"
    by_id = {m["external_id"]: m for m in page["items"]}
    assert [m["external_id"] for m in page["items"]] == ["clean", "messy", "short"]  # most days first
    clean, messy, short = by_id["clean"], by_id["messy"], by_id["short"]
    assert clean["grade"] == "good" and clean["issues"] == [] and clean["current"] and clean["coverage"] == 1.0
    assert clean["last_price"] == pytest.approx(0.49) and clean["url"] == "https://x/clean"
    assert messy["grade"] == "poor" and messy["coverage"] == pytest.approx(0.7) and messy["missing_price"] == 1
    assert messy["jumps"] == 2 and "geringe Liquidität" in messy["issues"]
    assert short["grade"] == "fair" and not short["current"] and short["issues"] == ["kurze Historie (< 7 Tage)"]


def test_market_list_filters_sorts_and_pages(session):
    assert [m["external_id"] for m in list_markets(session, q="FED")["items"]] == ["clean", "short"]
    assert list_markets(session, source="kalshi")["total"] == 1
    assert list_markets(session, min_days=8)["total"] == 1
    assert list_markets(session, current=True)["total"] == 2
    assert list_markets(session, sort="title")["items"][0]["title"] == "Fed cuts rates in March?"
    assert [m["external_id"] for m in list_markets(session, sort="volume")["items"]][-1] == "messy"
    second = list_markets(session, limit=2, offset=2)
    assert second["total"] == 3 and [m["external_id"] for m in second["items"]] == ["short"]


def test_market_detail_has_history_and_summary(session):
    detail = market_detail(session, "kalshi", "messy", today=TODAY)
    assert len(detail["rows"]) == 7 and detail["rows"][0]["date"] == "2026-03-02"
    assert detail["summary"]["grade"] == "poor" and detail["summary"]["title"] == "Oil above 100?"
    assert market_detail(session, "kalshi", "unknown") is None


def test_stock_catalog_covers_universe_and_flags_issues(session):
    stocks = {s["ticker"]: s for s in list_stocks(session, today=START + timedelta(days=30))}
    assert set(TOP_US_STOCKS) <= set(stocks)
    aapl, msft = stocks["AAPL"], stocks["MSFT"]
    assert aapl["grade"] == "good" and aapl["days"] == 30 and aapl["last_close"] == 129
    assert aapl["return_total"] == pytest.approx(0.29) and aapl["return_1m"] == pytest.approx(129 / 108 - 1)
    assert msft["grade"] == "poor" and msft["jumps"] == 1 and "veraltet" in msft["issues"]
    assert stocks["NVDA"]["issues"] == ["keine Kurse"] and stocks["NVDA"]["grade"] == "fair"


def test_stock_entry_counts_provisional_closes():
    day = date(2026, 3, 2)
    rows = [(day, 10.0, datetime.combine(day, time(15), tzinfo=timezone.utc))]  # fetched before the close
    entry = stock_entry("AAPL", rows, day + timedelta(days=1))
    assert entry["days"] == 0 and "1 ungültige oder vorläufige Kurse" in entry["issues"]


def test_stock_detail_returns(session):
    detail = stock_detail(session, "AAPL", today=TODAY)
    assert detail["rows"][0]["return"] is None and detail["rows"][1]["return"] == pytest.approx(0.01)
    assert stock_detail(session, "ZZZZ") is None


@pytest.fixture()
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_catalog_endpoints(client):
    s = client.get("/catalog/summary").json()
    assert len(s["sources"]) == 2 and s["stocks"]["with_prices"] == 2
    page = client.get("/catalog/markets", params={"source": "polymarket", "sort": "volume", "limit": 1}).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    assert client.get("/catalog/markets", params={"sort": "bogus"}).status_code == 422
    assert client.get("/catalog/markets/kalshi/messy").json()["summary"]["days"] == 7
    assert client.get("/catalog/markets/kalshi/nope").status_code == 404
    assert len(client.get("/catalog/stocks").json()) >= len(TOP_US_STOCKS)
    assert client.get("/catalog/stocks/AAPL").json()["summary"]["days"] == 30
    assert client.get("/catalog/stocks/ZZZZ").status_code == 404


def test_catalog_tab_is_wired_into_the_dashboard(client):
    html = client.get("/").text
    assert 'data-view="catalog"' in html and 'data-view-panel="catalog"' in html
    assert html.index("/static/metrics.js") < html.index("/static/catalog.js")  # catalog reuses metricChart
    js = client.get("/static/catalog.js").text
    for endpoint in ("/catalog/summary", "/catalog/markets", "/catalog/stocks"):
        assert endpoint in js
    ids = set(re.findall(r'\$\("([\w-]+)"\)', js))
    assert ids and all(f'id="{i}"' in html for i in ids), "catalog.js nutzt IDs, die im HTML fehlen"
    assert "logEvent(" in js and "catalog.market.open" in js and "catalog.stock.open" in js
