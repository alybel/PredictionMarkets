"""Client tests: normalization to the common schema and error resilience.

No live API calls: HTTP sessions are replaced by fakes.
"""

import dataclasses
from datetime import datetime, timezone

import pytest
import requests

from backend.app.clients import KalshiClient, NormalizedMarket, PolymarketClient


class FakeResponse:
    def __init__(self, payload=None, error: Exception | None = None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if isinstance(self._error, requests.HTTPError):
            raise self._error

    def json(self):
        if isinstance(self._error, ValueError):
            raise self._error
        return self._payload


class FakeSession:
    def __init__(self, payload=None, error: Exception | None = None):
        self._payload = payload
        self._error = error

    def get(self, url, params=None, timeout=None):
        if isinstance(self._error, requests.ConnectionError):
            raise self._error
        return FakeResponse(self._payload, self._error)


def poly_events(markets: list, **event) -> list:
    """Gamma /events page: one liquid event wrapping the given markets."""
    return [{"liquidity": "40000", "slug": "x-event", "tags": [{"label": "Politics"}, {"label": "Elections"}],
             "markets": markets, **event}]


def kalshi_events(markets: list, category: str = "Economics") -> dict:
    """Kalshi /events page with nested markets and no further cursor."""
    return {"events": [{"event_ticker": "EV", "category": category, "markets": markets}], "cursor": ""}


POLYMARKET_PAYLOAD = poly_events(
    [
        {
            "id": "12345",
            "question": "Will X win the election?",
            "outcomePrices": '["0.62", "0.38"]',
            "volumeNum": 150000.5,
            "liquidityNum": 32000.0,
            "endDate": "2028-11-07T12:00:00Z",
        },
        {"id": "999", "question": None},  # malformed: no title -> skipped
    ]
)

KALSHI_PAYLOAD = kalshi_events(
    [
        {
            "ticker": "FED-25DEC",
            "title": "Fed cuts rates in December?",
            "last_price_dollars": "0.6200",
            "volume_fp": "88000.00",
            "liquidity_dollars": "45000.0000",
            "close_time": "2026-12-31T23:00:00Z",
        },
        {"title": "no ticker"},  # malformed -> skipped
    ]
)


def test_clients_return_identical_schema():
    poly = PolymarketClient(session=FakeSession(POLYMARKET_PAYLOAD)).fetch_markets()
    kalshi = KalshiClient(session=FakeSession(KALSHI_PAYLOAD)).fetch_markets()

    assert len(poly) == 1 and len(kalshi) == 1
    expected_fields = {f.name for f in dataclasses.fields(NormalizedMarket)}
    for market in (poly[0], kalshi[0]):
        assert isinstance(market, NormalizedMarket)
        assert {f.name for f in dataclasses.fields(market)} == expected_fields


def test_polymarket_normalization():
    (market,) = PolymarketClient(session=FakeSession(POLYMARKET_PAYLOAD)).fetch_markets()
    assert market.source == "polymarket"
    assert market.external_id == "12345"
    assert market.title == "Will X win the election?"
    assert market.category == "Politics"
    assert market.tags == ["Politics", "Elections"]
    assert market.price == pytest.approx(0.62)
    assert market.volume == pytest.approx(150000.5)
    assert market.liquidity == pytest.approx(32000.0)
    assert market.end_date == datetime(2028, 11, 7, 12, 0, tzinfo=timezone.utc)
    assert market.fetched_at is not None


def test_kalshi_normalization_uses_dollar_fields():
    (market,) = KalshiClient(session=FakeSession(KALSHI_PAYLOAD)).fetch_markets()
    assert market.source == "kalshi"
    assert market.external_id == "FED-25DEC"
    assert market.category == "Economics"
    assert market.tags == ["Economics"]
    assert market.price == pytest.approx(0.62)
    assert market.volume == pytest.approx(88000)
    assert market.liquidity == pytest.approx(45000.0)
    assert market.end_date == datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc)


def test_kalshi_legacy_cent_fields_are_normalized():
    item = {"ticker": "T", "title": "Q?", "last_price": 62, "volume": 88000, "liquidity": 4500000}
    (market,) = KalshiClient(session=FakeSession(kalshi_events([item]))).fetch_markets()
    assert market.price == pytest.approx(0.62)  # 62 cents -> 0.62
    assert market.liquidity == pytest.approx(45000.0)  # cents -> dollars


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("network down"),
        requests.HTTPError("500 Server Error"),
        ValueError("invalid JSON"),
    ],
)
def test_failed_responses_yield_empty_list(error):
    assert PolymarketClient(session=FakeSession(error=error)).fetch_markets() == []
    assert KalshiClient(session=FakeSession(error=error)).fetch_markets() == []


@pytest.mark.parametrize("payload", [None, {}, [], {"markets": None}, "not json-shaped"])
def test_empty_or_unexpected_payloads_yield_empty_list(payload):
    assert PolymarketClient(session=FakeSession(payload)).fetch_markets() == []
    assert KalshiClient(session=FakeSession(payload)).fetch_markets() == []


def test_missing_numeric_fields_become_none():
    item = {"id": "1", "question": "Q?", "outcomePrices": "broken", "volumeNum": "n/a"}
    market = PolymarketClient()._normalize(item)
    assert market.price is None
    assert market.volume is None
    assert market.liquidity is None
    assert market.end_date is None
    # Unknown liquidity counts as 0 and keeps the market out of the population.
    assert PolymarketClient(session=FakeSession(poly_events([item]))).fetch_markets() == []


def test_malformed_end_date_becomes_none():
    payload = poly_events([{"id": "1", "question": "Q?", "endDate": "not-a-date", "liquidityNum": 20000}])
    (market,) = PolymarketClient(session=FakeSession(payload)).fetch_markets()
    assert market.end_date is None
