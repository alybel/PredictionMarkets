"""Unit conversion, order-book fields, liquidity score and eligibility filter."""

from datetime import datetime, timezone

import pytest

from backend.app.clients.kalshi import KalshiClient
from backend.app.clients.polymarket import PolymarketClient
from backend.app.eui.harmonize import SPREAD_LIMIT, exclusion_reason, harmonize, liquidity_score, spread_of
from backend.tests.eui_helpers import FUTURE, market


def test_kalshi_converts_cents_and_contracts_to_dollars():
    item = {"ticker": "FED-DEC", "title": "Fed cuts in December?", "status": "active", "category": "Economics",
            "last_price": 55, "yes_bid": 54, "yes_ask": 57, "volume_24h": 1000, "liquidity": 250000,
            "close_time": "2027-01-01T00:00:00Z"}
    m = KalshiClient()._normalize(item)
    assert m.price == 0.55
    assert m.best_bid == 0.54 and m.best_ask == 0.57
    assert m.volume_24h == pytest.approx(1000 * 0.55)  # contracts x price
    assert m.liquidity == 2500.0  # cents / 100, open interest not used
    assert m.active is True


def test_kalshi_prefers_dollar_fields_and_reports_inactive_status():
    item = {"ticker": "T", "title": "x", "status": "closed", "last_price_dollars": "0.30", "yes_bid_dollars": "0.29",
            "yes_ask_dollars": "0.31", "volume_24h_fp": "200.0", "liquidity_dollars": "12.5"}
    m = KalshiClient()._normalize(item)
    assert (m.best_bid, m.best_ask, m.volume_24h, m.liquidity, m.active) == (0.29, 0.31, 60.0, 12.5, False)


def test_polymarket_reads_order_book_and_activity_flags():
    item = {"id": "1", "question": "Q?", "outcomePrices": '["0.4","0.6"]', "liquidityNum": 500.0,
            "volume24hr": 120.0, "bestBid": 0.39, "bestAsk": 0.42, "active": True, "closed": False}
    m = PolymarketClient()._normalize(item)
    assert (m.volume_24h, m.best_bid, m.best_ask, m.active) == (120.0, 0.39, 0.42, True)
    assert PolymarketClient()._normalize({**item, "closed": True}).active is False
    assert PolymarketClient()._normalize({"id": "2", "question": "Q?"}).active is None


def test_spread_and_score_penalty():
    assert spread_of(0.4, 0.45) == pytest.approx(0.05)
    assert spread_of(None, 0.45) is None
    assert liquidity_score(1000.0, 500.0, 0.02) == 1500.0
    assert liquidity_score(1000.0, 500.0, SPREAD_LIMIT) == 1500.0  # limit itself is not penalized
    assert liquidity_score(1000.0, 500.0, 0.10) == pytest.approx(750.0)  # twice the limit halves the score
    assert liquidity_score(1000.0, 500.0, None) == 1500.0


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        (dict(), None),
        (dict(active=False), "inactive"),
        (dict(price=None), "no_price"),
        (dict(price=0.02), "price_outside_corridor"),
        (dict(price=0.98), "price_outside_corridor"),
        (dict(price=0.03), None),
        (dict(end_date=None), "no_end_date"),
        (dict(end_date=datetime(2020, 1, 1, tzinfo=timezone.utc)), "ended"),
        (dict(end_date=datetime(2020, 1, 1)), "ended"),  # naive datetimes count as UTC
    ],
)
def test_exclusion_reasons(kwargs, reason):
    m = market(**kwargs)
    assert exclusion_reason(m.price, m.end_date, m.active) == reason


def test_harmonize_carries_fields_and_eligibility():
    h = harmonize(market(liquidity=1000.0, volume_24h=200.0, best_bid=0.40, best_ask=0.50))
    assert h.key == ("polymarket", "pm-1")
    assert h.depth_usd == 1000.0 and h.volume_24h_usd == 200.0
    assert h.spread == pytest.approx(0.10)
    assert h.liquidity_score == pytest.approx(600.0)
    assert h.eligible and h.exclusion_reason is None and h.end_date == FUTURE
    ineligible = harmonize(market(price=0.99, liquidity=None, volume_24h=None))
    assert not ineligible.eligible and ineligible.exclusion_reason == "price_outside_corridor"
    assert ineligible.liquidity_score == 0.0
