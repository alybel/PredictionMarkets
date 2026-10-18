"""Index/EUI markets, domains, price histories and stock closes read from the raw data layer (1017-1426).

Per process: building from prepared ``raw_responses`` equals the old processing of the same answers; a refresh request
with a faked network lands in staging and ``raw_responses``; missing raw data give a clear message, never a live fetch.
Fake network and fake answers only: no network, no LLM.
"""

import dataclasses
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.backfill import sources
from backend.app.backfill.sources import CLOB_HISTORY_URL, fetch_history
from backend.app.clients import KalshiClient, PolymarketClient
from backend.app.clients.profiles import make_clients
from backend.app.domains.discovery import scan_sources
from backend.app.domains.models import DomainMarket
from backend.app.domains.service import load_domain
from backend.app.eui.models import MarketSnapshot
from backend.app.eui.snapshot import take_snapshot
from backend.app.rawdata.access import MissingRawData, RawReader
from backend.app.rawdata.models import DONE, RawFetchJob
from backend.app.rawdata.providers import RawCloses, RawHistory, RawMarkets, RawScanner
from backend.app.staging.models import RawLoadLog, RawResponse
from backend.app.stocks.models import StockPrice
from backend.app.stocks.prices import fetch_closes, refresh_prices
from backend.tests.raw_helpers import Capture, factory, file_factory, net, prepare_raw  # noqa: F401  (fixtures)
from backend.tests.test_clients import KALSHI_PAYLOAD, POLYMARKET_PAYLOAD, FakeSession
from backend.tests.test_domain_discovery import FakeGet
from backend.tests.test_domains import domain

TODAY = date(2026, 10, 17)
NOW = datetime(2026, 10, 17, 23, 0, tzinfo=timezone.utc)
TS = lambda d: int(datetime(d.year, d.month, d.day, 1, tzinfo=timezone.utc).timestamp())  # noqa: E731


def market_answer(url, params):
    return POLYMARKET_PAYLOAD if "polymarket" in url else KALSHI_PAYLOAD


def plain(markets):
    return [dataclasses.replace(m, fetched_at=NOW) for m in markets]


def raw_rows(factory, dataset):
    with factory() as s:
        return s.scalar(select(func.count()).select_from(RawResponse).where(RawResponse.dataset == dataset))


def staged(dataset):
    return sorted(p for p in (Path(os.environ["DATA_DIR"]) / "staging").glob(f"*/{dataset}/*/*.json") if not p.name.endswith(".meta.json"))


# ---- index population / EUI snapshot (clients) --------------------------------------------------------------

def test_index_markets_from_raw_equal_the_old_processing(factory, net):
    old = [m for c in (PolymarketClient(session=FakeSession(POLYMARKET_PAYLOAD)), KalshiClient(session=FakeSession(KALSHI_PAYLOAD)))
           for m in c.fetch_markets()]
    net.set(market_answer)
    new = [m for c in RawMarkets(factory, "test").clients("index") for m in c.fetch_markets()]
    assert plain(new) == plain(old) and len(new) == 2
    again = [m for c in RawMarkets(factory, "test").clients("index", refresh=False) for m in c.fetch_markets()]
    assert plain(again) == plain(old)  # read-only: the stored raw answers, no further request
    assert len(net.calls) == 2


def test_prepared_raw_answers_build_the_same_markets_without_any_request(factory, net):
    cap = Capture(market_answer)
    old = [m for c in make_clients("index", lambda s: cap) for m in c.fetch_markets()]
    prepare_raw(factory, "index_markets", cap.seen)
    new = [m for c in RawMarkets(factory, "test").clients("index", refresh=False) for m in c.fetch_markets()]
    assert plain(new) == plain(old) and net.calls == []


def test_market_refresh_lands_in_staging_and_raw_responses(factory, net):
    net.set(market_answer)
    provider = RawMarkets(factory, "test")
    provider.clients("snapshot")
    assert provider.job["status"] == DONE and provider.job["summary"]["markets"] == {"polymarket": 2, "kalshi": 2}
    assert len(staged("index_markets")) == raw_rows(factory, "index_markets") == 2
    with factory() as s:
        assert s.scalar(select(RawFetchJob.job)) == "markets:snapshot"
        assert s.scalar(select(func.count()).select_from(RawLoadLog)) == 2


def test_missing_market_raw_data_give_a_clear_message_not_a_live_fetch(factory, net):
    poly, _ = RawMarkets(factory, "test").clients("index", refresh=False)
    with pytest.raises(MissingRawData, match="Keine Rohdaten für index_markets"):
        poly.fetch_markets()
    assert net.calls == []


def snapshot_rows(session):
    return sorted(session.execute(select(MarketSnapshot.source, MarketSnapshot.external_id, MarketSnapshot.price, MarketSnapshot.eligible,
                                         MarketSnapshot.exclusion_reason, MarketSnapshot.liquidity_score)).all())


def test_eui_snapshot_from_raw_equals_the_old_snapshot(factory, net, tmp_path):
    with file_factory(tmp_path / "old.db")() as old_session:
        take_snapshot(old_session, make_clients("snapshot", lambda s: Capture(market_answer)), TODAY, NOW)
        old = snapshot_rows(old_session)
    net.set(market_answer)
    with factory() as session:
        run = take_snapshot(session, None, TODAY, NOW)
        assert run.status == "ok" and snapshot_rows(session) == old and len(old) == 2
    assert raw_rows(factory, "index_markets") == 2


# ---- domains ----------------------------------------------------------------------------------------------

def history_answer(url, params):
    """Domain scan answers plus histories: Gamma market -> token, CLOB history, Kalshi candles."""
    if "gamma-api.polymarket.com/markets/" in url:
        return {"clobTokenIds": '["tok"]'}
    if url == CLOB_HISTORY_URL:
        return {"history": [{"t": TS(TODAY - timedelta(days=3)), "p": 0.4}, {"t": TS(TODAY - timedelta(days=2)), "p": 0.45}]}
    if "/candlesticks" in url and "/historical/" not in url:
        day = TODAY - timedelta(days=5)
        return {"candlesticks": [{"end_period_ts": TS(day), "price": {"close_dollars": "0.3"}, "volume_fp": "10"}]}
    return FakeGet()(url, params)


def domain_rows(session):
    members = sorted(session.execute(select(DomainMarket.source, DomainMarket.external_id, DomainMarket.active, DomainMarket.loaded_at.is_(None))).all())
    return members, sorted(session.execute(select(MarketSnapshot.external_id, MarketSnapshot.snapshot_date, MarketSnapshot.price,
                                                  MarketSnapshot.volume_24h_usd)).all())


def test_domain_load_from_raw_equals_the_old_load_run(factory, net, tmp_path):
    with file_factory(tmp_path / "old.db")() as old_session:
        old_summary = load_domain(old_session, domain(old_session), lambda t, s, terms: scan_sources(t, s, terms, history_answer),
                                  lambda src, ext, start: fetch_history(src, ext, start, history_answer), TODAY)
        old = domain_rows(old_session)
    net.set(history_answer)
    with factory() as session:
        summary = load_domain(session, domain(session), today=TODAY)
        assert domain_rows(session) == old and len(old[0]) == 4 and len(old[1]) > 0
    assert {k: summary[k] for k in ("new_markets", "loaded", "rows_added")} == {k: old_summary[k] for k in ("new_markets", "loaded", "rows_added")}
    assert raw_rows(factory, "domain_scan") > 0 and raw_rows(factory, "price_history") > 0
    with factory() as s:
        kinds = {j.split(":")[0] for j in s.scalars(select(RawFetchJob.job))}
    assert kinds == {"domains", "history"}


def test_domain_scan_from_prepared_raw_and_missing_raw(factory, net, caplog):
    cap = Capture(FakeGet())
    old = scan_sources("Economics", "Fed", ["fed"], cap)
    prepare_raw(factory, "domain_scan", cap.seen)
    new = scan_sources("Economics", "Fed", ["fed"], RawReader(factory, "domain_scan"))
    assert {s: (plain(v["markets"]), v["errors"]) for s, v in new.items()} == {s: (plain(v["markets"]), v["errors"]) for s, v in old.items()}
    with caplog.at_level(logging.WARNING):
        empty = scan_sources("Politics", None, [], RawReader(factory, "domain_scan"))
    assert empty == {"polymarket": {"markets": [], "errors": 1}, "kalshi": {"markets": [], "errors": 1}}
    assert "Keine Rohdaten für domain_scan" in caplog.text and net.calls == []


def test_domain_refresh_lands_in_staging_and_raw_responses(factory, net):
    net.set(history_answer)
    out = RawScanner(factory, "test")("Economics", "Fed", ["fed"])
    assert {m.external_id for m in out["kalshi"]["markets"]} == {"KXFED-25DEC", "KXFED-27DEC"}
    assert len(staged("domain_scan")) == raw_rows(factory, "domain_scan") > 0


# ---- price histories --------------------------------------------------------------------------------------

def test_price_history_from_raw_equals_the_old_processing_and_lands_in_raw(factory, net):
    start = TODAY - timedelta(days=30)
    keys = [("polymarket", "pm-1"), ("kalshi", "KXFED-25DEC")]
    old = {k: fetch_history(*k, start, history_answer) for k in keys}
    net.set(history_answer)
    provider = RawHistory(factory, "test")
    provider.prepare(keys, start)
    assert {k: provider(*k, start) for k in keys} == old and all(old.values())
    assert len(staged("price_history")) == raw_rows(factory, "price_history") == 3  # gamma market, clob history, candles
    cap = Capture(history_answer)
    prepared = {k: fetch_history(*k, start, cap) for k in keys}
    assert {k: RawHistory(factory, "test")(*k, start) for k in keys} == prepared  # read-only from the stored raw rows


def test_missing_history_raw_data_give_a_clear_message_not_a_live_fetch(factory, net, caplog):
    with caplog.at_level(logging.WARNING):
        assert RawHistory(factory, "test")("polymarket", "pm-1", TODAY) == {}
    assert "Keine Rohdaten für price_history" in caplog.text and net.calls == []
    with pytest.raises(MissingRawData, match="Rohdatenschicht"):
        fetch_history("polymarket", "pm-1", TODAY)  # no getter: never a live request
    assert sources.FETCHERS["polymarket"] is sources.fetch_polymarket


# ---- stock closes -----------------------------------------------------------------------------------------

def chart_answer(url, params):
    days = [TODAY - timedelta(days=n) for n in (3, 2, 1)]
    return {"chart": {"result": [{"meta": {"gmtoffset": 0}, "timestamp": [TS(d) for d in days],
                                  "indicators": {"adjclose": [{"adjclose": [100.0, 101.5, 99.0]}]}}]}}


def prices(session):
    return sorted(session.execute(select(StockPrice.ticker, StockPrice.price_date, StockPrice.close)).all())


def test_stock_closes_from_raw_equal_the_old_processing(factory, net, tmp_path):
    with file_factory(tmp_path / "old.db")() as old_session:
        old_summary = refresh_prices(old_session, lambda t, s: fetch_closes(t, s, chart_answer), ["AAPL", "MSFT"], TODAY, NOW)
        old = prices(old_session)
    net.set(chart_answer)
    with factory() as session:
        summary = refresh_prices(session, None, ["AAPL", "MSFT"], TODAY, NOW, trigger="test")
        assert prices(session) == old and len(old) == 6 and summary == old_summary
    assert len(staged("stock_chart")) == raw_rows(factory, "stock_chart") == 2
    assert {u.rsplit("/", 1)[1] for u, _ in net.calls} == {"AAPL", "MSFT"}


def test_stock_closes_from_prepared_raw_and_missing_raw(factory, net):
    cap = Capture(chart_answer)
    old = fetch_closes("AAPL", TODAY - timedelta(days=10), cap)
    prepare_raw(factory, "stock_chart", cap.seen, RawCloses.ignore)
    assert RawCloses(factory, "test")("AAPL", TODAY - timedelta(days=4)) == old  # start/end are no part of the request identity
    with factory() as session:
        summary = refresh_prices(session, RawCloses(factory, "test"), ["MSFT"], TODAY, NOW)
    assert summary["failed"] == ["MSFT"] and net.calls == []  # no raw data: failed with a clear log, never fetched live
    with pytest.raises(MissingRawData, match="Keine Rohdaten für stock_chart"):
        RawCloses(factory, "test")("MSFT", TODAY)


def test_processes_contain_no_http_calls_of_their_own():
    app = Path(__file__).resolve().parents[1] / "app"
    for rel in ("domains/discovery.py", "backfill/sources.py", "clients/base.py", "clients/kalshi.py", "clients/polymarket.py",
                "stocks/prices.py", "eui/snapshot.py"):
        text = (app / rel).read_text()
        assert "import requests" not in text and "httpx" not in text and "urllib.request" not in text, rel
