"""Consolidated aggregation: liquidity-weighted value per index."""

import pytest

from backend.app.aggregation import (
    METHOD_LIQUIDITY_WEIGHTED,
    METHOD_NO_DATA,
    METHOD_SIMPLE_AVERAGE,
    aggregate,
)
from backend.app.clients.base import NormalizedMarket


def make_market(**overrides) -> NormalizedMarket:
    defaults = dict(
        source="polymarket",
        external_id="pm-1",
        title="Will the president win the election?",
        category="politics",
        price=0.5,
        volume=100.0,
        liquidity=1_000.0,
    )
    defaults.update(overrides)
    return NormalizedMarket(**defaults)


def test_liquidity_weighted_average():
    markets = [
        make_market(external_id="a", price=0.2, liquidity=1_000.0),
        make_market(external_id="b", price=0.8, liquidity=3_000.0),
    ]
    result = aggregate("political", markets)
    assert result.value == pytest.approx(0.65)  # (0.2*1000 + 0.8*3000) / 4000
    assert result.method == METHOD_LIQUIDITY_WEIGHTED
    assert result.market_count == 2
    assert result.total_liquidity == 4_000.0
    assert result.total_volume == 200.0
    assert result.markets == markets


def test_unknown_liquidity_counts_as_zero_weight():
    markets = [
        make_market(external_id="a", price=0.2, liquidity=None),
        make_market(external_id="b", price=0.8, liquidity=2_000.0),
    ]
    result = aggregate("political", markets)
    assert result.value == pytest.approx(0.8)
    assert result.total_liquidity == 2_000.0


def test_fallback_to_simple_average_without_liquidity():
    markets = [
        make_market(external_id="a", price=0.2, liquidity=None),
        make_market(external_id="b", price=0.6, liquidity=0.0),
    ]
    result = aggregate("political", markets)
    assert result.value == pytest.approx(0.4)
    assert result.method == METHOD_SIMPLE_AVERAGE


def test_markets_without_price_are_listed_but_not_valued():
    markets = [
        make_market(external_id="a", price=None, liquidity=9_999.0),
        make_market(external_id="b", price=0.7, liquidity=1_000.0),
    ]
    result = aggregate("political", markets)
    assert result.value == pytest.approx(0.7)
    assert result.market_count == 2
    assert result.total_liquidity == 10_999.0


def test_no_priced_markets_yields_no_value():
    result = aggregate("political", [make_market(price=None)])
    assert result.value is None
    assert result.method == METHOD_NO_DATA


def test_empty_index():
    result = aggregate("political", [])
    assert result.value is None
    assert result.method == METHOD_NO_DATA
    assert result.market_count == 0
    assert result.total_liquidity == 0.0
    assert result.total_volume == 0.0
    assert result.markets == []
