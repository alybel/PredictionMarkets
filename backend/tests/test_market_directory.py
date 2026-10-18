"""Market directory (risk index 1/7, 1017-1107): all markets incl. expired/resolved, idempotence, inbox, coverage, errors.

Source answers are fixtures behind a fake getter: no network, no LLM.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.api import app, get_session
from backend.app.clients.kalshi import KALSHI_EVENTS_URL
from backend.app.clients.polymarket import GAMMA_EVENTS_URL
from backend.app.directory.api import get_directory_getter, get_directory_staging
from backend.app.directory.models import DirectoryMarket, DirectoryRun
from backend.app.directory.service import coverage, inbox, run_directory
from backend.app.rawdata.streams import GAMMA_KEYSET_URL, KALSHI_HISTORICAL_URL, KALSHI_MARKETS_URL
from backend.app.staging.models import RawResponse
from backend.tests.eui_helpers import memory_session

NOW = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)

PM_OPEN = {"id": "pm-1", "question": "S&P 500 down 20% by end of 2026?", "startDate": "2025-11-01T00:00:00Z",
           "endDate": "2026-12-31T00:00:00Z", "closed": False, "active": True, "outcomePrices": "[\"0.12\", \"0.88\"]"}
PM_RESOLVED = {"id": "pm-2", "question": "Stock market crash in 2024?", "startDate": "2023-06-01T00:00:00Z",
               "endDate": "2024-12-31T00:00:00Z", "closed": True, "umaResolutionStatus": "resolved", "outcomePrices": "[\"0\", \"1\"]"}
K_OPEN = {"ticker": "KXINXD-26DEC", "title": "S&P below 5000 on Dec 31?", "open_time": "2026-01-02T15:00:00Z",
          "close_time": "2026-12-31T21:00:00Z", "status": "active"}
K_CLOSED = {"ticker": "KXINXD-25JUN", "title": "S&P below 4000 on Jun 30?", "open_time": "2025-01-02T15:00:00Z",
            "close_time": "2025-06-30T21:00:00Z", "status": "closed", "result": ""}
K_HIST = {"ticker": "KXINX-22DEC", "title": "S&P below 3500 in 2022?", "open_time": "2022-01-03T15:00:00Z",
          "close_time": "2022-12-30T21:00:00Z", "status": "finalized", "result": "no"}


class FakeSources:
    """Answers of both sources by URL; ``broken`` maps a URL to a raw body (or None = failed request).

    ``extra_pages`` are further keyset pages of Polymarket (page n is reached with cursor "c<n>")."""

    def __init__(self, poly_open=(PM_OPEN,), poly_closed=(PM_RESOLVED,), kalshi=(K_OPEN, K_CLOSED), hist=(K_HIST,), broken=None,
                 extra_pages=()):
        self.open_event = {"id": "e1", "tags": [{"label": "Finance"}], "markets": list(poly_open)}
        self.closed_event = {"id": "e2", "tags": [{"label": "Finance"}, {"label": "Stocks"}], "markets": list(poly_closed)}
        self.pages = [[self.open_event, self.closed_event], *extra_pages]
        self.kalshi, self.hist, self.broken, self.calls = list(kalshi), list(hist), broken or {}, []
        self.closed_pages: dict[int, list] = {}  # offset -> events of the "latest closed" listing
        self.created: list = []                  # Kalshi /markets created since the last run

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if url in self.broken:
            return self.broken[url]
        if url == GAMMA_KEYSET_URL:
            n = int(params.get("after_cursor", "c0")[1:])
            data = {"events": self.pages[n], "next_cursor": f"c{n + 1}" if n + 1 < len(self.pages) else None}
        elif url == GAMMA_EVENTS_URL:
            if params["closed"] == "false":
                data = [self.open_event] if params["offset"] == 0 else []
            else:
                data = self.closed_pages.get(params["offset"], [self.closed_event] if params["offset"] == 0 else [])
        elif url == KALSHI_EVENTS_URL:
            data = {"events": [{"event_ticker": "KXINXD", "title": "S&P", "category": "Financials", "markets": self.kalshi}], "cursor": ""}
        elif url == KALSHI_MARKETS_URL:
            data = {"markets": self.created if "min_created_ts" in params else [], "cursor": ""}
        elif url == KALSHI_HISTORICAL_URL:
            data = {"markets": self.hist, "cursor": None}
        else:
            raise AssertionError(f"unexpected URL {url}")
        return json.dumps(data).encode()


@pytest.fixture()
def session():
    return memory_session()


def run(session, tmp_path, sources=None, now=NOW, **kw):
    return run_directory(session, sources or FakeSources(), tmp_path / "staging", now=lambda: now, **kw)


def rows(session) -> dict:
    return {r.external_id: r for r in session.scalars(select(DirectoryMarket))}


def snapshot(session) -> list[tuple]:
    return sorted(session.execute(select(DirectoryMarket.source, DirectoryMarket.external_id, DirectoryMarket.title, DirectoryMarket.status,
                                         DirectoryMarket.listed_at, DirectoryMarket.end_date, DirectoryMarket.updated_at,
                                         DirectoryMarket.first_seen_at)).all())


def test_active_and_resolved_markets_of_both_sources_with_dates_and_status(session, tmp_path):
    summary = run(session, tmp_path)
    r = rows(session)
    assert {k: (v.source, v.status) for k, v in r.items()} == {
        "pm-1": ("polymarket", "active"), "pm-2": ("polymarket", "resolved"), "KXINXD-26DEC": ("kalshi", "active"),
        "KXINXD-25JUN": ("kalshi", "expired"), "KXINX-22DEC": ("kalshi", "resolved")}
    assert r["pm-2"].listed_at.date().isoformat() == "2023-06-01" and r["pm-2"].end_date.date().isoformat() == "2024-12-31"
    assert r["KXINX-22DEC"].listed_at.year == 2022 and r["pm-2"].tags == ["Finance", "Stocks"] and r["KXINXD-26DEC"].tags == ["Financials"]
    assert summary["sources"]["polymarket"]["found"] == 4 and summary["sources"]["polymarket"]["duplicates"] == 2  # three listings
    assert summary["sources"]["polymarket"]["errors"] == summary["sources"]["kalshi"]["errors"] == 0
    assert all(not v.is_new for v in r.values())  # first run per source = baseline
    assert summary["raw"]["new"] == 7  # 3 Polymarket + 4 Kalshi answers moved staging -> raw
    assert session.scalar(select(func.count()).select_from(RawResponse)) == 7


def test_second_run_without_new_data_adds_and_changes_nothing(session, tmp_path):
    run(session, tmp_path)
    before = snapshot(session)
    summary = run(session, tmp_path, now=NOW + timedelta(hours=1))
    assert snapshot(session) == before
    assert summary["sources"]["polymarket"]["new"] == summary["sources"]["kalshi"]["changed"] == 0
    assert summary["sources"]["kalshi"]["unchanged"] == 3 and summary["raw"]["new"] == 0
    assert session.scalar(select(func.count()).select_from(RawResponse)) == 7


def test_database_rejects_a_direct_duplicate(session):
    session.add(DirectoryMarket(source="kalshi", external_id="X", title="a", tags=[], status="active", first_seen_at=NOW, is_new=False))
    session.commit()
    session.add(DirectoryMarket(source="kalshi", external_id="X", title="b", tags=[], status="active", first_seen_at=NOW, is_new=False))
    with pytest.raises(IntegrityError):
        session.commit()


def test_new_market_appears_once_in_inbox_known_market_does_not(session, tmp_path):
    run(session, tmp_path)
    new = {"ticker": "KXCRASH-27", "title": "Market crash in 2027?", "open_time": "2026-10-16T15:00:00Z", "status": "active"}
    later = NOW + timedelta(days=1)
    run(session, tmp_path, FakeSources(kalshi=(K_OPEN, K_CLOSED, new)), now=later)
    run(session, tmp_path, FakeSources(kalshi=(K_OPEN, K_CLOSED, new)), now=later + timedelta(days=1))
    box = inbox(session)
    assert [m["external_id"] for m in box["markets"]] == ["KXCRASH-27"]
    assert box["since"].startswith(later.date().isoformat())
    assert inbox(session, since=later + timedelta(hours=1))["count"] == 0


def test_status_change_updates_the_known_row_only(session, tmp_path):
    run(session, tmp_path)
    settled = {**K_CLOSED, "status": "settled", "result": "no"}
    summary = run(session, tmp_path, FakeSources(kalshi=(K_OPEN, settled)), now=NOW + timedelta(days=1))
    assert rows(session)["KXINXD-25JUN"].status == "resolved" and summary["sources"]["kalshi"]["changed"] == 1
    assert session.scalar(select(func.count()).select_from(DirectoryMarket)) == 5 and inbox(session)["count"] == 0


def test_coverage_counts_markets_per_year_from_start_and_end(session, tmp_path):
    run(session, tmp_path)
    cov = coverage(session, NOW)
    assert cov["years"] == [2022, 2023, 2024, 2025, 2026]
    assert cov["sources"]["polymarket"] == {"2022": 0, "2023": 1, "2024": 1, "2025": 1, "2026": 1}
    assert cov["sources"]["kalshi"] == {"2022": 1, "2023": 0, "2024": 0, "2025": 1, "2026": 1}
    assert cov["totals"] == {"polymarket": 2, "kalshi": 3} and cov["statuses"]["kalshi"]["resolved"] == 1


def test_broken_and_empty_answers_are_logged_and_the_rest_is_loaded(session, tmp_path, caplog):
    broken = {GAMMA_KEYSET_URL: b"<html>502</html>", GAMMA_EVENTS_URL: b"{}", KALSHI_HISTORICAL_URL: None}
    bad_market = {"title": "no ticker"}
    with caplog.at_level(logging.WARNING):
        summary = run(session, tmp_path, FakeSources(kalshi=(K_OPEN, bad_market, K_CLOSED), broken=broken))
    assert set(rows(session)) == {"KXINXD-26DEC", "KXINXD-25JUN"}
    assert summary["sources"]["polymarket"]["errors"] == 3 and summary["sources"]["kalshi"]["errors"] == 1
    assert summary["sources"]["kalshi"]["malformed"] == 1
    assert "answer is no JSON" in caplog.text and "unexpected answer" in caplog.text and "request failed" in caplog.text
    with caplog.at_level(logging.WARNING):
        empty = run(session, tmp_path, FakeSources(broken={GAMMA_KEYSET_URL: b'{"events": [], "next_cursor": null}'}), now=NOW + timedelta(days=1))
    assert "empty answer" in caplog.text and empty["sources"]["kalshi"]["new"] == 1
    assert empty["sources"]["polymarket"]["found"] == 2  # newest and latest closed listing


def test_one_failing_source_never_stops_the_other(session, tmp_path, monkeypatch):
    from backend.app.rawdata.streams import Crawler

    original = Crawler.pages

    def pages(self, stream, *a):
        if stream.source == "polymarket":
            raise RuntimeError("boom")
        return original(self, stream, *a)
    monkeypatch.setattr(Crawler, "pages", pages)
    summary = run(session, tmp_path)
    assert summary["sources"]["polymarket"]["errors"] == 3 and len(rows(session)) == 3


def test_full_listing_resumes_at_the_saved_cursor(session, tmp_path):
    old = {"id": "e0", "tags": [], "markets": [{"id": "pm-old", "question": "Dow below 30000 in 2021?", "startDate": "2020-12-01",
                                                "endDate": "2021-12-31", "closed": True, "outcomePrices": "[\"0\", \"1\"]"}]}
    sources = FakeSources(extra_pages=[[old]])
    first = run(session, tmp_path, sources, max_pages=1)
    assert first["positions"]["polymarket_all"] == "c1" and "pm-old" not in rows(session)
    second = run(session, tmp_path, sources, now=NOW + timedelta(days=1), max_pages=1)
    assert ("https://gamma-api.polymarket.com/events/keyset", {"limit": 100, "after_cursor": "c1"}) in sources.calls
    r = rows(session)
    assert r["pm-old"].status == "resolved" and not r["pm-old"].is_new  # history found later is not "new"
    assert second["positions"]["polymarket_all"] == "c1" and second["streams"]["polymarket_all"]["finished"]


def test_head_stream_stops_at_a_page_without_news(session, tmp_path):
    sources = FakeSources()
    closed = lambda i: {"id": f"c{i}", "tags": [], "markets": [{"id": f"pm-c{i}", "question": f"Closed {i}?", "closed": True}]}  # noqa: E731
    sources.closed_pages = {0: [closed(i) for i in range(100)], 100: [closed(100)]}
    run(session, tmp_path, sources)
    run(session, tmp_path, sources, now=NOW + timedelta(days=1))
    head = [p["offset"] for u, p in sources.calls if u == GAMMA_EVENTS_URL and p["closed"] == "true"]
    assert head == [0, 100, 0] and len(rows(session)) == 106  # second run: page 0 had nothing new, no further pages


# ---- API -------------------------------------------------------------------------------------

@pytest.fixture()
def api(session, tmp_path):
    sources = FakeSources()
    app.dependency_overrides.update({get_session: lambda: session, get_directory_getter: lambda: sources,
                                     get_directory_staging: lambda: tmp_path / "staging"})
    yield TestClient(app), session, sources
    app.dependency_overrides.clear()


def test_api_load_list_inbox_review_coverage(api, caplog):
    client, session, sources = api
    assert client.get("/directory/runs/last").json() is None
    assert client.post("/directory/load").json()["sources"]["kalshi"]["found"] == 3
    page = client.get("/directory/markets", params={"status": "resolved"}).json()
    assert page["total"] == 2 and {m["external_id"] for m in page["markets"]} == {"pm-2", "KXINX-22DEC"}
    assert set(page["markets"][0]) >= {"source", "external_id", "title", "tags", "listed_at", "end_date", "status", "first_seen_at"}
    assert client.get("/directory/markets", params={"q": "crash"}).json()["total"] == 1
    sources.kalshi.append({"ticker": "KXNEW", "title": "New crash market", "status": "active"})
    client.post("/directory/load")
    box = client.get("/directory/inbox").json()
    assert box["count"] == 1 and box["markets"][0]["external_id"] == "KXNEW"
    with caplog.at_level(logging.INFO):
        assert client.post("/directory/inbox/reviewed", json={"ids": [box["markets"][0]["id"]]}).json() == {"marked": 1}
    assert "1 markets marked as reviewed" in caplog.text and client.get("/directory/inbox").json()["count"] == 0
    assert client.get("/directory/inbox", params={"since": "kein-datum"}).status_code == 400
    assert client.get("/directory/coverage").json()["totals"]["kalshi"] == 4
    assert client.get("/directory/runs/last").json()["summary"]["sources"]["kalshi"]["new"] == 1
    assert session.scalar(select(func.count()).select_from(DirectoryRun)) == 2


def test_existing_catalog_and_domain_endpoints_keep_their_fields(api):
    client = api[0]
    client.post("/directory/load")
    assert client.get("/domains").json() == []
    summary = client.get("/catalog/summary").json()
    assert set(summary) == {"sources", "stocks"} and set(summary["stocks"]) == {"tickers", "with_prices", "rows"}
    assert set(client.get("/catalog/markets").json()) >= {"items", "total"}


def test_kalshi_market_created_since_last_run_is_new_and_gets_tags_later(session, tmp_path):
    run(session, tmp_path)
    created = {"ticker": "KXVIX-26OCT", "title": "VIX above 40 this month?", "open_time": "2026-10-17T12:00:00Z", "status": "active"}
    sources = FakeSources()
    sources.created = [created]
    run(session, tmp_path, sources, now=NOW + timedelta(days=1))
    calls = [p for u, p in sources.calls if u == KALSHI_MARKETS_URL]
    assert {"min_created_ts", "min_settled_ts"} == {k for p in calls for k in p if k.startswith("min_")}
    assert int(datetime(2026, 10, 16, tzinfo=timezone.utc).timestamp()) in {p.get("min_created_ts") for p in calls}
    assert rows(session)["KXVIX-26OCT"].tags == [] and [m["external_id"] for m in inbox(session)["markets"]] == ["KXVIX-26OCT"]
    sources.kalshi.append(created)
    summary = run(session, tmp_path, sources, now=NOW + timedelta(days=2))
    assert rows(session)["KXVIX-26OCT"].tags == ["Financials"] and summary["sources"]["kalshi"]["changed"] == 1
    assert inbox(session)["count"] == 1
