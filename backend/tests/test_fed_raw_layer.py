"""Fed rate chains, raw layer (1015-2043): parsers of both sources, append-only idempotent load, point in time,
fault tolerance, rate-limit retry, migration. Fixture payloads only, no network."""

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from backend.app.fed import models as fed_models  # noqa: F401  (register tables)
from backend.app.fed import sources
from backend.app.fed.loader import load_fed
from backend.app.fed.models import HISTORICAL, LIVE, FedMarket, FedPrice
from backend.app.fed.parsers import (FedPoint, kalshi_outcome_bp, parse_kalshi_candles, parse_kalshi_event,
                                     parse_polymarket_event, parse_polymarket_history, polymarket_outcome_bp, snap_session)
from backend.app.fed.sources import HttpFedSource
from backend.app.quality.pit import is_observable
from backend.tests.eui_helpers import memory_session
from backend.tests.fed_fixtures import (DAYS, KALSHI_EVENT, KALSHI_MARKETS, NOW, POLY_EVENT, SESSION, FakeApi,
                                        historical_candle, kalshi_candles, live_candle, poly_history, ts)

BACKEND_DIR = Path(__file__).resolve().parents[1]


# ---- parsers ---------------------------------------------------------------------------------

def test_kalshi_parser_splits_session_outcome_and_daily_prices():
    infos = parse_kalshi_event(KALSHI_EVENT, KALSHI_MARKETS)
    assert {i.external_id.rsplit("-", 1)[1]: i.outcome_bp for i in infos} == {"C26": -50, "C25": -25, "H0": 0, "H25": 25}
    assert {i.session_date for i in infos} == {SESSION} and {i.source for i in infos} == {"kalshi"}
    assert infos[0].url == "https://kalshi.com/markets/KXFEDDECISION-25DEC-C26" and infos[0].event_id == "KXFEDDECISION-25DEC"
    points = parse_kalshi_candles(kalshi_candles("C25"), NOW)
    assert [(p.day, p.price, p.volume_usd) for p in points] == [(DAYS[0], 0.25, 25.0), (DAYS[1], 0.30, 30.0)]
    # historical API format (close / volume), no trade: bid/ask midpoint
    hist = parse_kalshi_candles({"candlesticks": [historical_candle(DAYS[0], "0.97", "2"), historical_candle(DAYS[1], None, "0")]}, NOW)
    assert [(p.price, p.volume_usd) for p in hist] == [(0.97, 1.94), (pytest.approx(0.45), 0.0)]
    assert kalshi_outcome_bp({"ticker": "FEDDECISION-24SEP-C26"}) == -50 and kalshi_outcome_bp({"custom_strike": {"Hike": ">25"}}) == 50


def test_polymarket_parser_splits_event_into_outcomes_with_bp_and_daily_prices():
    infos = parse_polymarket_event(POLY_EVENT, [SESSION, date(2026, 1, 28)])
    assert [(i.outcome_label, i.outcome_bp) for i in infos] == [
        ("50+ bps decrease", -50), ("25 bps decrease", -25), ("No change", 0), ("25+ bps increase", 25)]  # "Other" skipped
    assert {i.session_date for i in infos} == {SESSION} and infos[0].history_key == "tok-m50"
    assert infos[0].url == "https://polymarket.com/event/fed-decision-in-december"
    points = parse_polymarket_history(poly_history("tok-m25"), NOW)
    assert [(p.day, p.price, p.volume_usd) for p in points] == [(DAYS[0], 0.30, None), (DAYS[1], 0.25, None)]
    assert polymarket_outcome_bp("75+ bps decrease") == -75 and polymarket_outcome_bp("Other") is None
    # end dates are off by up to a day: snapped to the known meeting, unknown meetings are skipped
    assert snap_session(date(2026, 10, 29), [date(2026, 10, 28)]) == date(2026, 10, 28)
    assert parse_polymarket_event(POLY_EVENT, [date(2026, 1, 28)]) == []


def test_points_after_the_us_close_or_in_the_future_are_dropped():
    d = DAYS[0]
    payload = {"candlesticks": [live_candle(d, "0.40", hour=5), live_candle(d + timedelta(1), "0.90", hour=21)]}
    assert [p.day for p in parse_kalshi_candles(payload, NOW)] == [d]  # 21:00 UTC is after the US close
    poly = {"history": [{"t": ts(d, 0), "p": 0.4}, {"t": ts(d, 20, 30), "p": 0.9}, {"t": ts(NOW.date(), 13), "p": 0.5}]}
    assert [(p.day, p.price) for p in parse_polymarket_history(poly, NOW)] == [(d, 0.4)]  # after close / after now


# ---- loader ----------------------------------------------------------------------------------

def all_rows(session):
    markets = session.execute(select(FedMarket.id, FedMarket.source, FedMarket.external_id, FedMarket.session_date,
                                     FedMarket.outcome_bp, FedMarket.created_at)).all()
    prices = session.execute(select(FedPrice.id, FedPrice.market_id, FedPrice.price_date, FedPrice.price, FedPrice.volume_usd,
                                    FedPrice.observed_at, FedPrice.fetched_at, FedPrice.origin)).all()
    return sorted(markets), sorted(prices)


def test_load_stores_both_sources_and_second_run_adds_and_changes_nothing():
    session = memory_session()
    first = load_fed(session, HttpFedSource(FakeApi()), now=NOW)
    assert first["markets_listed"] == 8 and first["markets_failed"] == 0 and first["rows_added"] == 16
    assert first["by_source"]["kalshi"]["rows_added"] == 8 and first["by_source"]["polymarket"]["rows_added"] == 8
    before = all_rows(session)
    second = load_fed(session, HttpFedSource(FakeApi()), now=NOW + timedelta(hours=1))
    assert second["rows_added"] == 0 and second["markets_new"] == 0
    assert all_rows(session) == before  # append-only: no row added or overwritten
    assert {r.origin for r in session.scalars(select(FedPrice))} == {HISTORICAL}


def test_new_days_are_appended_without_touching_old_rows():
    session = memory_session()
    api = FakeApi()
    load_fed(session, HttpFedSource(api), now=NOW)
    before = all_rows(session)[1]
    market = session.scalar(select(FedMarket).where(FedMarket.external_id == "KXFEDDECISION-25DEC-C25"))
    from backend.app.fed.loader import store_points
    today = NOW.date()
    points = [FedPoint(DAYS[0], 0.99, 1.0, datetime(2025, 12, 8, 5, tzinfo=timezone.utc)),  # already stored: ignored
              FedPoint(today, 0.5, None, datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc))]
    assert store_points(session, market, points, NOW) == 1
    session.commit()
    after = all_rows(session)[1]
    assert set(before) < set(after) and len(after) == len(before) + 1
    assert session.scalar(select(FedPrice.origin).where(FedPrice.price_date == today)) == LIVE


def test_no_stored_row_is_observed_after_the_us_close_of_its_day():
    session = memory_session()
    load_fed(session, HttpFedSource(FakeApi()), now=NOW)
    market = session.scalar(select(FedMarket))
    from backend.app.fed.loader import store_points
    late = FedPoint(DAYS[0] - timedelta(1), 0.5, None, datetime(2025, 12, 7, 20, 30, tzinfo=timezone.utc))
    assert store_points(session, market, [late], NOW) == 0
    assert all(is_observable(p.observed_at, p.price_date) for p in session.scalars(select(FedPrice)))


def test_a_broken_market_is_logged_and_skipped_the_rest_is_loaded(caplog):
    session = memory_session()
    with caplog.at_level(logging.WARNING):
        summary = load_fed(session, HttpFedSource(FakeApi(broken={"KXFEDDECISION-25DEC-H0", "tok-0"})), now=NOW)
    assert summary["markets_failed"] == 2 and summary["markets_loaded"] == 6 and summary["rows_added"] == 12
    assert {f["external_id"] for f in summary["failures"]} == {"KXFEDDECISION-25DEC-H0", "570362"}
    assert "fed load of kalshi/KXFEDDECISION-25DEC-H0 failed" in caplog.text
    assert session.scalar(select(FedMarket).where(FedMarket.external_id == "KXFEDDECISION-25DEC-H0")) is None
    # the next run loads the missing markets
    assert load_fed(session, HttpFedSource(FakeApi()), now=NOW)["rows_added"] == 4


def test_kalshi_falls_back_to_the_historical_api():
    class Historical(FakeApi):
        def __call__(self, url, params=None):
            if "/series/" in url:
                return {"candlesticks": []}
            if "/historical/markets/" in url:
                return {"candlesticks": [historical_candle(DAYS[0], "0.70", "3")]}
            return super().__call__(url, params)

    info = parse_kalshi_event(KALSHI_EVENT, KALSHI_MARKETS)[0]
    assert [p.price for p in HttpFedSource(Historical()).history(info, NOW)] == [0.70]


def test_rate_limit_is_retried(monkeypatch):
    class Resp:
        def __init__(self, code):
            self.status_code, self.headers = code, {"Retry-After": "0"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise sources.http.HTTPError(str(self.status_code))

        def json(self):
            return {"ok": True}

    answers = iter([Resp(429), Resp(429), Resp(200)])
    monkeypatch.setattr(sources.http.requests, "get", lambda *a, **k: next(answers))
    waits = []
    assert sources.http_get("https://x", sleep=waits.append) == {"ok": True}
    assert len(waits) == 2


def test_migration_creates_the_raw_tables(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'fed.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    inspector = inspect(create_engine(db_url))
    assert {c["name"] for c in inspector.get_columns("fed_markets")} >= {
        "source", "external_id", "event_id", "session_date", "outcome_bp", "outcome_label", "title", "url"}
    assert {c["name"] for c in inspector.get_columns("fed_prices")} >= {
        "market_id", "price_date", "price", "volume_usd", "observed_at", "fetched_at", "origin"}
    assert "eui_market_snapshots" in inspector.get_table_names()  # existing tables stay
