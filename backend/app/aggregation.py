"""Consolidated aggregation: one value per index from its selected markets.

The consolidated value is the liquidity-weighted average of the market
prices. Markets without a price appear in the details but are excluded
from the value; unknown liquidity counts as weight 0. If no priced
market carries liquidity, the value falls back to the simple average.
"""

from dataclasses import dataclass, field

METHOD_LIQUIDITY_WEIGHTED = "liquidity_weighted"
METHOD_SIMPLE_AVERAGE = "simple_average"
METHOD_NO_DATA = "no_data"


@dataclass(frozen=True)
class IndexAggregate:
    """Consolidated value of an index plus the markets behind it."""

    index_name: str
    value: float | None
    method: str
    market_count: int
    total_liquidity: float
    total_volume: float
    markets: list = field(default_factory=list)


def aggregate(index_name: str, markets) -> IndexAggregate:
    """Consolidate the markets already selected for an index.

    Works on any normalized market object exposing ``price``, ``volume``
    and ``liquidity`` — both NormalizedMarket and the Market ORM row.
    """
    markets = list(markets)
    value, method = _consolidated_value([m for m in markets if m.price is not None])
    return IndexAggregate(
        index_name=index_name,
        value=value,
        method=method,
        market_count=len(markets),
        total_liquidity=sum(m.liquidity or 0.0 for m in markets),
        total_volume=sum(m.volume or 0.0 for m in markets),
        markets=markets,
    )


def _consolidated_value(priced: list) -> tuple[float | None, str]:
    if not priced:
        return None, METHOD_NO_DATA
    total_weight = sum(m.liquidity or 0.0 for m in priced)
    if total_weight > 0:
        weighted = sum(m.price * (m.liquidity or 0.0) for m in priced)
        return weighted / total_weight, METHOD_LIQUIDITY_WEIGHTED
    return sum(m.price for m in priced) / len(priced), METHOD_SIMPLE_AVERAGE
