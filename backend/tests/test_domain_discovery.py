"""Domain scan at the sources (1016-2013): open and settled/closed markets of both sources, broken answers counted.

All requests go to a fake ``get_json``; no network.
"""

from datetime import date, datetime, timezone

from backend.app.backfill import sources
from backend.app.clients.kalshi import KALSHI_EVENTS_URL
from backend.app.clients.polymarket import GAMMA_EVENTS_URL
from backend.app.domains.discovery import KALSHI_SERIES_URL, scan_sources, slug

POLY_OPEN = {"id": "pm-1", "question": "Fed cut in 2027?", "outcomePrices": '["0.4","0.6"]', "active": True, "closed": False}
POLY_CLOSED = {"id": "pm-9", "question": "Fed cut in March 2025?", "outcomePrices": '["1","0"]', "active": True, "closed": True}
KALSHI_SETTLED = {"ticker": "KXFED-25DEC", "title": "Fed cut in Dec 2025?", "status": "settled", "last_price_dollars": "0.99"}
KALSHI_OPEN = {"ticker": "KXFED-27DEC", "title": "Fed cut in Dec 2027?", "status": "active", "last_price_dollars": "0.3"}


def poly_event(market):
    return [{"id": "e1", "title": "Fed", "volume24hr": "0", "tags": [{"label": "Economics"}, {"label": "Fed"}], "markets": [market]}]


class FakeGet:
    def __init__(self, broken=()):
        self.calls, self.broken = [], set(broken)

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if url in self.broken:
            return None
        if url == GAMMA_EVENTS_URL:
            if params["offset"]:
                return []
            return poly_event(POLY_CLOSED if params["closed"] == "true" else POLY_OPEN)
        if url == KALSHI_SERIES_URL:
            return {"series": [{"ticker": "KXGDP", "title": "GDP", "tags": None}, {"ticker": "KXFED", "title": "Fed rate", "tags": ["Fed"]}]}
        if url == KALSHI_EVENTS_URL:
            if params["series_ticker"] != "KXFED":
                return "garbage"  # malformed answer
            return {"events": [{"event_ticker": "KXFED-X", "title": "Fed", "markets": [KALSHI_SETTLED, KALSHI_OPEN, "junk"]}], "cursor": ""}
        return None


def test_scan_finds_open_and_closed_markets_of_both_sources():
    get = FakeGet()
    out = scan_sources("Economics", "Fed", ["fed"], get)
    poly = {m.external_id: m.active for m in out["polymarket"]["markets"]}
    kalshi = {m.external_id: m for m in out["kalshi"]["markets"]}
    assert poly == {"pm-1": True, "pm-9": False}
    assert kalshi["KXFED-25DEC"].active is False and kalshi["KXFED-27DEC"].active is True
    assert kalshi["KXFED-25DEC"].tags == ["Economics", "Fed"]  # series tags refine the topic
    gamma = [p for u, p in get.calls if u == GAMMA_EVENTS_URL]
    assert {p["closed"] for p in gamma} == {"false", "true"} and {p["tag_slug"] for p in gamma} == {"fed"}
    kalshi_events = [p for u, p in get.calls if u == KALSHI_EVENTS_URL]
    assert kalshi_events[0]["series_ticker"] == "KXFED" and "status" not in kalshi_events[0]  # term hit first, all statuses
    assert out["kalshi"]["errors"] == 1 and out["polymarket"]["errors"] == 0  # the garbage answer is counted


def test_failed_source_is_counted_and_other_source_still_scanned():
    out = scan_sources("Economics", None, [], FakeGet(broken={GAMMA_EVENTS_URL}))
    assert out["polymarket"] == {"markets": [], "errors": 2}
    assert len(out["kalshi"]["markets"]) == 2


def test_crashing_get_json_yields_empty_source():
    def boom(url, params):
        raise KeyError("x")

    out = scan_sources("Economics", None, [], boom)
    assert out == {"polymarket": {"markets": [], "errors": 1}, "kalshi": {"markets": [], "errors": 1}}


def test_slug_of_topic_labels():
    assert slug(" Fed Rates & Policy ") == "fed-rates-policy"


def test_resolved_market_histories_use_fallback_endpoints(monkeypatch):
    ts = int(datetime(2025, 3, 1, 1, tzinfo=timezone.utc).timestamp())
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params or {}))
        if "gamma" in url:
            return {"clobTokenIds": '["tok"]'}
        if "prices-history" in url:
            return {"history": [] if params.get("interval") == "max" else [{"t": ts, "p": 0.7}]}
        if "/historical/" in url:
            return {"candlesticks": [{"end_period_ts": ts, "price": {"close_dollars": "0.9"}, "volume_fp": "10"}]}
        return None  # failed request (getter contract of the raw data layer)

    monkeypatch.setattr(sources, "_get", fake_get)
    assert sources.fetch_history("polymarket", "pm-9", date(2025, 1, 1)) == {date(2025, 3, 1): sources.DayPoint(0.7)}
    assert sources.fetch_history("kalshi", "KXFED-25MAR", date(2025, 1, 1)) == {date(2025, 3, 1): sources.DayPoint(0.9, 9.0)}
    assert any("startTs" in p for u, p in calls if "prices-history" in u)
