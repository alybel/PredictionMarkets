"""More history (1015-2258): 5-year window, range switch of the metric index view, backfill of older stock closes.
No network, no LLM."""

from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.backfill.service import BACKFILL_DAYS
from backend.app.history import MAX_DAYS
from backend.app.models import utcnow
from backend.app.stocks.api import get_fetcher
from backend.app.stocks.models import StockPrice
from backend.app.stocks.prices import GAP_TOLERANCE_DAYS, INITIAL_DAYS, price_status, refresh_prices
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_regression import env  # noqa: F401  (pytest fixture)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TODAY = date(2026, 3, 10)  # Tuesday
TARGET = TODAY - timedelta(days=INITIAL_DAYS)


def test_window_reaches_five_years_while_backfill_default_stays_one_year():
    assert MAX_DAYS >= 1825 and INITIAL_DAYS > MAX_DAYS
    assert BACKFILL_DAYS == 365


def _stored(session, ticker, days):
    session.add_all(StockPrice(ticker=ticker, price_date=d, close=100.0) for d in days)
    session.commit()


def test_backfill_loads_older_closes_only_for_short_histories():
    session = memory_session()
    _stored(session, "AAPL", [TODAY - timedelta(days=3), TODAY - timedelta(days=1)])           # short history
    _stored(session, "MSFT", [TARGET + timedelta(days=GAP_TOLERANCE_DAYS), TODAY - timedelta(days=1)])  # complete
    calls = {}

    def fetcher(ticker, start):
        calls[ticker] = start
        return {TARGET + timedelta(days=2): 50.0, TODAY - timedelta(days=3): 999.0}

    summary = refresh_prices(session, fetcher, tickers=["AAPL", "MSFT"], today=TODAY, backfill=True)
    assert calls == {"AAPL": TARGET, "MSFT": TODAY - timedelta(days=1)}  # MSFT stays incremental
    assert summary["backfilled"] == ["AAPL"] and summary["rows_added"] == 1
    closes = {r.price_date: r.close for r in session.query(StockPrice).filter_by(ticker="AAPL")}
    assert closes[TARGET + timedelta(days=2)] == 50.0 and closes[TODAY - timedelta(days=3)] == 100.0  # stored rows untouched


def test_without_backfill_the_refresh_stays_incremental():
    session = memory_session()
    _stored(session, "AAPL", [TODAY - timedelta(days=1)])
    calls = []
    summary = refresh_prices(session, lambda t, s: calls.append(s) or {}, tickers=["AAPL"], today=TODAY)
    assert calls == [TODAY - timedelta(days=1)] and summary["backfilled"] == []


def test_refresh_endpoint_passes_the_backfill_flag():
    session = memory_session()
    _stored(session, "AAPL", [date.today() - timedelta(days=5)])
    starts = []

    def fetcher(ticker, start):
        starts.append(start)
        return {}

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    app.dependency_overrides[get_fetcher] = lambda: fetcher
    try:
        client = TestClient(app)
        assert client.post("/stocks/refresh").json()["backfilled"] == []
        body = client.post("/stocks/refresh?backfill=true").json()
    finally:
        app.dependency_overrides.clear()
    assert "AAPL" in body["backfilled"] and min(starts) == utcnow().date() - timedelta(days=INITIAL_DAYS)
    assert body["status"] == price_status(session)


def test_metric_correlation_accepts_five_years_and_reports_coverage(env):  # noqa: F811
    client, _, inputs = env
    body = client.get(f"/builder/indices/Fed Rates/correlation?days={MAX_DAYS}").json()
    assert body["days"] == MAX_DAYS
    assert body["first_index_date"] == min(inputs.values).isoformat()
    assert body["first_price_date"] is not None and date.fromisoformat(body["first_price_date"]) <= max(inputs.values)
    assert client.get(f"/builder/indices/Fed Rates/correlation?days={MAX_DAYS + 1}").status_code == 422


def test_frontend_offers_the_range_switch_and_loads_older_closes():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js_mi = (STATIC / "metricindex.js").read_text(encoding="utf-8")
    js_corr = (STATIC / "correlation.js").read_text(encoding="utf-8")
    assert 'id="mi-range"' in html and all(f'data-days="{d}"' in html for d in (90, 365, 1095, 1825))
    assert '"?days=" + mi.days' in js_mi and '"mi-range"' in js_mi  # chart window + click logging
    assert "mi.days : builder.days" in js_corr  # stock evaluation follows the metric range switch
    assert "/stocks/refresh?backfill=true" in js_corr
    assert "priceCoverageNote" in (STATIC / "regression.js").read_text(encoding="utf-8")
