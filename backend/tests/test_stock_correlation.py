"""Stock correlation (1015-0943): index change on day T vs. next-day return of the top-100 US stocks. No network."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.engine import upsert_rule
from backend.app.stocks.api import get_fetcher
from backend.app.stocks.correlation import signal_pairs, stock_stats
from backend.app.stocks.models import StockPrice
from backend.app.stocks.prices import parse_chart, price_status, refresh_prices
from backend.app.stocks.stats import correlation_p_value, pearson
from backend.app.stocks.trades import trade_series, trade_summary
from backend.app.stocks.universe import TOP_US_STOCKS
from backend.tests.eui_helpers import memory_session
from backend.tests.test_index_builder import snap

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FRI, SAT, SUN, MON, TUE = (date(2026, 3, 6) + timedelta(days=i) for i in range(5))
THU = FRI - timedelta(days=1)


# ---- statistics -----------------------------------------------------------------------

def test_universe_has_100_unique_us_tickers():
    assert len(TOP_US_STOCKS) == 100 and {"AAPL", "MSFT", "NVDA", "BRK-B"} <= set(TOP_US_STOCKS)


def test_pearson_and_p_value_match_reference_values():
    r, slope = pearson([1, 2, 3, 4], [2, 4, 6, 8])
    assert r == pytest.approx(1.0) and slope == pytest.approx(2.0)
    assert pearson([1, 1, 1], [1, 2, 3]) == (None, None)
    # scipy.stats: r=0.3, n=30 -> p=0.1072; r=0.5, n=10 -> p=0.1411
    assert correlation_p_value(0.3, 30) == pytest.approx(0.1072, abs=1e-4)
    assert correlation_p_value(0.5, 10) == pytest.approx(0.1411, abs=1e-4)
    assert correlation_p_value(0.0, 50) == pytest.approx(1.0)


# ---- alignment ------------------------------------------------------------------------

def test_signal_pairs_use_next_trading_day_and_carry_weekend_changes():
    index = {FRI: 0.40, SAT: 0.45, SUN: 0.50, MON: 0.55}
    closes = {THU: 90.0, FRI: 100.0, MON: 110.0, TUE: 121.0}
    # Fri has no predecessor; Mon pairs the Fri->Mon change with the Mon->Tue return (not Fri->Mon: no look-ahead)
    [(change, ret)] = signal_pairs(index, closes)
    assert change == pytest.approx(0.15) and ret == pytest.approx(0.10)


def test_signal_pairs_skip_gaps_and_missing_next_day():
    index = {date(2026, 3, 2): 0.4, date(2026, 3, 20): 0.5, date(2026, 3, 23): 0.6}
    closes = {date(2026, 3, 2): 1.0, date(2026, 3, 20): 1.0, date(2026, 3, 23): 1.0}
    assert signal_pairs(index, closes) == []  # 18-day hole, and 23 March has no following close


def test_stock_stats_hit_rate_and_conditional_means():
    pairs = [(0.02, 0.01), (-0.02, -0.01), (0.01, -0.005), (0.001, 0.5)] * 3
    stats = stock_stats(pairs, min_change=0.005)
    assert stats["n"] == 12 and stats["events_up"] == 6 and stats["events_down"] == 3  # 0.001 is below the threshold
    assert stats["hit_rate"] == pytest.approx(6 / 9)
    assert stats["mean_return_after_up"] == pytest.approx(0.0025)
    assert stats["mean_return_after_down"] == pytest.approx(-0.01)
    assert stock_stats(pairs[:4])["correlation"] is None  # below MIN_OBSERVATIONS


# ---- prices ---------------------------------------------------------------------------

def test_parse_chart_prefers_adjusted_close_in_exchange_time():
    payload = {"chart": {"result": [{"meta": {"gmtoffset": -14400}, "timestamp": [1772803800, 1772890200],
                                     "indicators": {"quote": [{"close": [10.0, 11.0]}], "adjclose": [{"adjclose": [9.5, None]}]}}]}}
    assert parse_chart(payload) == {date(2026, 3, 6): 9.5}
    assert parse_chart({"chart": {"result": None}}) == {}


def test_refresh_prices_is_incremental_and_survives_failures():
    session = memory_session()
    calls = []

    def fetcher(ticker, start):
        calls.append((ticker, start))
        if ticker == "MSFT":
            raise RuntimeError("HTTP 500")
        return {MON: 1.0, TUE: 2.0}

    first = refresh_prices(session, fetcher, tickers=["AAPL", "MSFT"], today=TUE)
    assert first["rows_added"] == 2 and first["failed"] == ["MSFT"]
    second = refresh_prices(session, fetcher, tickers=["AAPL"], today=TUE)
    assert second["rows_added"] == 0 and calls[-1] == ("AAPL", TUE)  # continues from the last stored day
    assert price_status(session)["rows"] == 2 and price_status(session)["last_date"] == TUE.isoformat()


# ---- API ------------------------------------------------------------------------------

def _weekdays(start: date, count: int) -> list[date]:
    days, d = [], start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


@pytest.fixture()
def env():
    session = memory_session()
    upsert_rule(session, "econ", categories=["economics"])
    days = _weekdays(date(2026, 3, 2), 16)
    values = [0.5 + 0.02 * ((i * 7) % 5 - 2) for i in range(len(days))]  # zig-zag index between 0.46 and 0.54
    for day, value in zip(days, values):
        snap(session, day, "m", price=value)
    # AAPL's next-day return is exactly 0.5 x the index change -> correlation +1; MSFT mirrors it -> -1
    aapl, msft = [100.0], [100.0]
    for i in range(1, len(days)):
        change = values[i - 1] - values[i - 2] if i >= 2 else 0.0
        aapl.append(aapl[-1] * (1 + 0.5 * change))
        msft.append(msft[-1] * (1 - 0.5 * change))
    for day, a, m in zip(days, aapl, msft):
        session.add_all([StockPrice(ticker="AAPL", price_date=day, close=a), StockPrice(ticker="MSFT", price_date=day, close=m)])
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    app.dependency_overrides[get_fetcher] = lambda: (lambda ticker, start: {TUE: 50.0})
    yield TestClient(app), session
    app.dependency_overrides.clear()


def test_correlation_endpoint_finds_the_planted_patterns(env):
    client, _ = env
    body = client.get("/indices/econ/correlation").json()
    assert body["index_days"] == 16 and body["stocks_with_prices"] == 2 and body["stocks_tested"] == 2
    top = {row["ticker"]: row for row in body["stocks"][:2]}
    assert top["AAPL"]["correlation"] == pytest.approx(1.0) and top["AAPL"]["significant"]
    assert top["AAPL"]["hit_rate"] == pytest.approx(1.0) and top["AAPL"]["slope_per_point"] == pytest.approx(0.005)
    assert top["MSFT"]["correlation"] == pytest.approx(-1.0) and top["MSFT"]["hit_rate"] == pytest.approx(0.0)
    assert body["significant"] == 2 and body["expected_false_positives"] == pytest.approx(0.1)
    assert len(body["stocks"]) == 100 and body["stocks"][2]["n"] == 0  # stocks without prices are listed, untested


def test_correlation_preview_and_unknown_index(env):
    client, _ = env
    preview = client.post("/indices/econ/correlation/preview?min_change=0.01", json={"categories": ["sports"]}).json()
    assert preview["index_days"] == 0 and preview["stocks_tested"] == 0 and preview["min_change"] == 0.01
    assert client.get("/indices/nope/correlation").status_code == 404
    assert client.get("/indices/econ/correlation?min_change=2").status_code == 422


def test_stock_refresh_and_status_endpoints(env):
    client, _ = env
    body = client.post("/stocks/refresh").json()
    # AAPL/MSFT already hold data up to 23 March, so the fake close of 10 March is older and ignored
    assert body["tickers"] == 100 and body["rows_added"] == 98 and body["failed"] == []
    assert client.get("/stocks/status").json()["tickers"] == 100


def test_frontend_has_correlation_card():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "correlation.js").read_text()
    assert 'id="cr-card"' in html and "/static/correlation.js" in html
    assert "/correlation" in js and "/stocks/refresh" in js and "logEvent" in js


# ---- trade view (1015-1110) -----------------------------------------------------------

def test_trade_series_positions_threshold_and_compounding():
    days = [(MON, TUE, 0.02, 0.10), (TUE, TUE + timedelta(days=1), -0.001, 0.50), (THU, FRI, -0.03, 0.10)]
    rows = trade_series(days, min_change=0.005, direction=-1)
    assert [r["position"] for r in rows] == [-1, 0, 1]  # negative correlation: index up -> short
    assert rows[0]["trade_return"] == pytest.approx(-0.10) and rows[1]["trade_return"] is None
    assert rows[-1]["strategy"] == pytest.approx(0.9 * 1.1 - 1) and rows[-1]["buy_hold"] == pytest.approx(1.1 * 1.5 * 1.1 - 1)
    summary = trade_summary(rows)
    assert summary["trades"] == 2 and summary["wins"] == 1 and summary["longs"] == 1 and summary["shorts"] == 1
    assert trade_summary([])["strategy_return"] is None
    assert trade_series([(MON, TUE, 0.495 - 0.5, 0.1)], 0.005, 1)[0]["position"] == 0  # float noise at the threshold
    assert stock_stats([(0.495 - 0.5, 0.1)], 0.005)["events_down"] == 0


def test_trades_endpoint_follows_the_correlation_direction(env):
    client, _ = env
    aapl = client.get("/indices/econ/correlation/AAPL").json()
    assert aapl["direction"] == 1 and aapl["in_sample"] and aapl["name"] == TOP_US_STOCKS["AAPL"]
    assert len(aapl["series"]) == aapl["n"] and aapl["summary"]["win_rate"] == pytest.approx(1.0)
    assert aapl["summary"]["strategy_return"] > 0
    msft = client.get("/indices/econ/correlation/MSFT").json()
    assert msft["direction"] == -1 and msft["summary"]["win_rate"] == pytest.approx(1.0)  # traded short on rises
    first = aapl["series"][0]
    assert set(first) >= {"date", "exit_date", "index_change", "stock_return", "position", "trade_return", "strategy", "buy_hold"}


def test_trades_endpoint_preview_and_errors(env):
    client, _ = env
    preview = client.post("/indices/econ/correlation/AAPL/preview", json={"categories": ["sports"]}).json()
    assert preview["series"] == [] and preview["summary"]["trades"] == 0
    assert client.get("/indices/econ/correlation/XXXX").status_code == 404
    assert client.get("/indices/nope/correlation/AAPL").status_code == 404
    assert client.get("/indices/econ/correlation/AAPL?min_change=2").status_code == 422


def test_frontend_has_trade_view():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "trades.js").read_text()
    corr_js = (STATIC / "correlation.js").read_text()
    assert all(f'id="{i}"' in html for i in ("cr-detail", "cr-perf", "cr-returns", "cr-trade-rows", "cr-detail-close"))
    assert "/static/trades.js" in html and html.index("correlation.js") < html.index("trades.js")
    assert "openTrades(" in corr_js and "correlation-loaded" in corr_js and "correlation-loaded" in js
    assert "/correlation/" in js and "logEvent" in js


# ---- archive indices unchanged by the metric evaluation (1015-2231) --------------------------------------------


def test_archive_endpoints_return_unchanged_results(env):
    """Golden values captured before 1015-2231 (refactoring of the pairing, new metric endpoints)."""
    client, _ = env
    body = client.get("/indices/econ/correlation?min_change=0.005").json()
    assert {k: body[k] for k in ("stocks_tested", "significant", "bonferroni_alpha", "total_signals")} == \
        {"stocks_tested": 2, "significant": 2, "bonferroni_alpha": 0.025, "total_signals": 14}
    assert "market" not in body and "kind" not in body
    aapl = body["stocks"][0]
    assert (aapl["ticker"], aapl["n"], aapl["signals"], aapl["precision"]) == ("AAPL", 14, 7, None)
    assert aapl["slope_per_point"] == pytest.approx(0.005) and aapl["base_rate"] == pytest.approx(0.5102040816326531)
    attribution = client.get("/indices/econ/correlation/AAPL/attribution").json()
    assert attribution["n"] == 14 and attribution["total"]["corr_total"] == pytest.approx(1.0)
    assert attribution["total"]["beta_total"] == pytest.approx(0.005)
    trades = client.get("/indices/econ/correlation/MSFT?min_change=0.005").json()
    assert (trades["summary"]["trades"], trades["summary"]["longs"], trades["summary"]["shorts"]) == (14, 5, 9)
    assert trades["summary"]["strategy_return"] == pytest.approx(0.38543983119243874)
    assert [c["min_change"] for c in trades["robustness"][::3]] == [0.005, 0.01, 0.02]
