"""Data quality tab and point-in-time guard (1015-1948): no value reaches a day other than the one it was observed on,
nothing observed after the US close of its day, no provisional stock close. No network."""

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.backfill.sources import DayPoint, parse_kalshi_candles, parse_polymarket_history
from backend.app.engine import get_rule, upsert_rule
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.history import index_history
from backend.app.quality.checks import calendar_gaps, quality_report
from backend.app.quality.pit import (AFTER_CLOSE, BACKFILL, FUTURE_DATE, OUTSIDE_DAY, PROVISIONAL_CLOSE, is_observable,
                                     last_final_day, price_violation, snapshot_violation)
from backend.app.stocks.correlation import closes_by_ticker
from backend.app.stocks.models import StockPrice
from backend.app.stocks.prices import refresh_prices
from backend.tests.eui_helpers import memory_session
from backend.tests.test_index_builder import snap

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
D1, D2, D3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
TODAY = date(2026, 3, 10)
UTC = lambda d, h, m=0: datetime.combine(d, time(h, m), tzinfo=timezone.utc)  # noqa: E731


def add_row(session, day, ext, *, fetched, price=0.5, backfill=False, volume=20_000.0):
    run = session.query(SnapshotRun).filter_by(snapshot_date=day).first() or SnapshotRun(snapshot_date=day, status="ok")
    session.add(run)
    session.flush()
    session.add(MarketSnapshot(run_id=run.id, snapshot_date=day, source="polymarket", external_id=ext, title="Fed cuts rates?",
                               tags=["Economics"], price=price, volume_24h_usd=volume, fetched_at=fetched,
                               exclusion_reason=BACKFILL if backfill else None, end_date=UTC(date(2027, 1, 1), 0)))
    session.commit()


# ---- point-in-time rules -----------------------------------------------------------------

def test_snapshot_rows_count_only_on_their_day_before_the_us_close():
    assert snapshot_violation(D2, UTC(D2, 8), None, TODAY) is None
    assert snapshot_violation(D2, UTC(D2, 19, 59), None, TODAY) is None
    assert snapshot_violation(D2, UTC(D2, 20), None, TODAY) == AFTER_CLOSE
    assert snapshot_violation(D2, UTC(D3, 1), None, TODAY) == OUTSIDE_DAY      # day 3 information dated day 2
    assert snapshot_violation(D2, UTC(D1, 23), None, TODAY) == OUTSIDE_DAY     # day 1 information dated day 2
    assert snapshot_violation(D2, datetime(2026, 3, 3, 8), None, TODAY) is None  # SQLite: naive = UTC
    assert snapshot_violation(TODAY + timedelta(days=1), UTC(TODAY, 8), None, TODAY) == FUTURE_DATE
    assert snapshot_violation(D1, UTC(TODAY, 9), BACKFILL, TODAY) is None      # observation time guaranteed by the parser


def test_stock_close_is_final_only_after_the_close():
    assert price_violation(D2, UTC(D2, 15), TODAY) == PROVISIONAL_CLOSE
    assert price_violation(D2, UTC(D2, 22), TODAY) is None
    assert price_violation(D2, UTC(D3, 6), TODAY) is None
    assert price_violation(TODAY + timedelta(days=1), UTC(TODAY, 23), TODAY) == FUTURE_DATE
    assert last_final_day(UTC(D2, 21, 59)) == D1 and last_final_day(UTC(D2, 22)) == D2


def test_backfill_parsers_drop_points_observed_after_the_cutoff():
    ts = lambda d, h: int(UTC(d, h).timestamp())  # noqa: E731
    poly = parse_polymarket_history({"history": [{"t": ts(D2, 21), "p": 0.9}, {"t": ts(D2, 0), "p": 0.4}, {"t": ts(D3, 4), "p": 0.5}]})
    assert poly == {D2: DayPoint(0.4), D3: DayPoint(0.5)}  # the 21:00 point is after the close; order of the payload irrelevant
    assert is_observable(UTC(D2, 4), D2) and not is_observable(UTC(D2, 4), D1)
    kalshi = parse_kalshi_candles({"candlesticks": [
        {"end_period_ts": ts(D2, 4), "price": {"close_dollars": "0.40"}, "volume_fp": "10"},
        {"end_period_ts": ts(D3, 22), "price": {"close_dollars": "0.70"}, "volume_fp": "10"}]})
    assert list(kalshi) == [D2]


# ---- the guard in every analysis -----------------------------------------------------------

@pytest.fixture()
def session():
    s = memory_session()
    upsert_rule(s, "econ", categories=["economics"])
    return s


def test_history_ignores_rows_observed_after_the_close_or_on_another_day(session):
    add_row(session, D1, "a", fetched=UTC(D1, 8), price=0.4)
    add_row(session, D2, "a", fetched=UTC(D2, 8), price=0.5)
    add_row(session, D2, "late", fetched=UTC(D2, 21), price=0.9)      # after the close of D2
    add_row(session, D3, "a", fetched=UTC(D2, 8), price=0.6, backfill=True)
    add_row(session, D3, "moved", fetched=UTC(date(2026, 3, 5), 8), price=0.95)  # information of D5 dated D3
    points = {p["date"]: p for p in index_history(session, get_rule(session, "econ"))["points"]}
    assert points[D2.isoformat()]["value"] == pytest.approx(0.5) and points[D2.isoformat()]["market_count"] == 1
    assert points[D3.isoformat()]["value"] == pytest.approx(0.6) and points[D3.isoformat()]["market_count"] == 1


def test_provisional_and_future_closes_never_enter_the_correlation(session):
    session.add_all([StockPrice(ticker="AAPL", price_date=D1, close=100.0, fetched_at=UTC(D2, 6)),
                     StockPrice(ticker="AAPL", price_date=D2, close=101.0, fetched_at=UTC(D2, 15)),
                     StockPrice(ticker="AAPL", price_date=date(2099, 1, 1), close=1.0, fetched_at=UTC(D2, 23))])
    session.commit()
    assert closes_by_ticker(session, D1) == {"AAPL": {D1: 100.0}}


def test_refresh_skips_unfinished_closes_and_repairs_provisional_rows(session):
    session.add(StockPrice(ticker="AAPL", price_date=D2, close=101.0, fetched_at=UTC(D2, 15)))  # intraday price
    session.add(StockPrice(ticker="AAPL", price_date=D3, close=102.0, fetched_at=UTC(D3, 23)))
    session.commit()
    calls = []

    def fetcher(ticker, start):
        calls.append(start)
        return {D2: 99.0, D3: 102.0, date(2026, 3, 5): 103.0}

    summary = refresh_prices(session, fetcher, tickers=["AAPL"], now=UTC(date(2026, 3, 5), 18))
    assert calls == [D2]  # starts at the first provisional day, not the last stored one
    assert summary["rows_repaired"] == 1 and summary["rows_added"] == 0  # 5 March is not closed yet
    rows = {r.price_date: r.close for r in session.query(StockPrice)}
    assert rows == {D2: 99.0, D3: 102.0}


# ---- report and API ---------------------------------------------------------------------------

def test_calendar_gaps_name_the_missing_days():
    assert calendar_gaps([D1, D2, date(2026, 3, 6)]) == [{"after": "2026-03-03", "before": "2026-03-06", "missing_days": 2}]


def test_report_counts_blocked_rows_and_cleanliness_issues(session):
    add_row(session, D1, "a", fetched=UTC(D1, 8), price=0.1)
    add_row(session, D2, "a", fetched=UTC(D2, 8), price=0.9)            # jump of 80 points
    add_row(session, D2, "late", fetched=UTC(D2, 21))
    add_row(session, D2, "bad", fetched=UTC(D2, 8), price=1.4)
    add_row(session, date(2026, 3, 6), "a", fetched=UTC(D2, 9), price=0.8, backfill=True)
    session.add(StockPrice(ticker="AAPL", price_date=D2, close=101.0, fetched_at=UTC(D2, 15)))
    session.add(SnapshotRun(snapshot_date=D1, status="partial", note="no markets from: kalshi"))
    session.commit()
    report = quality_report(session, today=TODAY)
    checks = {c["key"]: c for c in report["lookahead"] + report["cleanliness"]}
    assert checks["after_close"]["count"] == 1 and checks["after_close"]["status"] == "blocked"
    assert checks["outside_day"]["status"] == "ok" and checks["provisional_close"]["count"] == 1
    assert checks["price_range"]["count"] == 1 and checks["price_jump"]["count"] >= 1
    assert checks["runs"]["count"] == 1 and checks["calendar_gap"]["count"] == 2
    assert checks["stock_missing"]["count"] == 99 and checks["backfill"]["count"] == 1
    assert report["snapshots"] == {"first_date": "2026-03-02", "last_date": "2026-03-06", "days": 3, "live_days": 2,
                                   "rows": 5, "backfill_rows": 1, "blocked_rows": 1}
    day2 = next(d for d in report["coverage"] if d["date"] == "2026-03-03")
    assert day2 == {"date": "2026-03-03", "live_rows": 3, "backfill_rows": 0, "blocked_rows": 1, "stock_closes": 0}


@pytest.fixture()
def client(session):
    snap(session, D1, "m1", title="Fed cuts rates in March?", price=0.3)
    snap(session, D2, "m1", title="Fed cuts rates in March?", price=0.35)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_quality_endpoints(client):
    report = client.get("/quality/report").json()
    assert {"snapshots", "stocks", "lookahead", "cleanliness", "coverage"} <= set(report)
    hits = client.get("/quality/markets", params={"q": "FED CUTS"}).json()
    assert hits == [{"source": "polymarket", "external_id": "m1", "title": "Fed cuts rates in March?", "days": 2,
                     "first_date": "2026-03-02", "last_date": "2026-03-03"}]
    rows = client.get("/quality/markets/polymarket/m1").json()["rows"]
    assert [(r["date"], r["origin"], r["violation"]) for r in rows] == [("2026-03-02", "live", None), ("2026-03-03", "live", None)]
    assert client.get("/quality/markets/polymarket/unknown").status_code == 404
    assert client.get("/quality/markets", params={"q": "x"}).status_code == 422


def test_dashboard_has_the_quality_tab():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-view="quality"' in html and 'data-view-panel="quality"' in html and "/static/quality.js" in html
    js = (STATIC / "quality.js").read_text(encoding="utf-8")
    assert 'fetch("/quality/report")' in js and 'logEvent("info", "quality.load"' in js
