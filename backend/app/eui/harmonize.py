"""Harmonize normalized markets into one dollar-based schema and score their liquidity.

Both clients already quote USD (Kalshi cents / contracts are converted in
the client), so this module derives spread, order-book depth, 24h turnover,
the liquidity score and the eligibility filter of the uncertainty index:
price inside the corridor, end date in the future, source status active.
Open interest is deliberately not used (only Kalshi reports it).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.app.models import DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE, utcnow

SPREAD_LIMIT = 0.05  # spreads above five cents make the price unreliable


@dataclass(frozen=True)
class HarmonizedMarket:
    source: str
    external_id: str
    title: str
    price: float | None
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    depth_usd: float
    volume_24h_usd: float
    liquidity_score: float
    end_date: datetime | None
    active: bool | None
    eligible: bool
    exclusion_reason: str | None
    tags: list[str] = field(default_factory=list)
    url: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)


def spread_of(best_bid: float | None, best_ask: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    return max(best_ask - best_bid, 0.0)


def liquidity_score(depth_usd: float, volume_24h_usd: float, spread: float | None) -> float:
    """Depth plus 24h turnover (USD); a spread above the limit scales the score down proportionally."""
    score = max(depth_usd, 0.0) + max(volume_24h_usd, 0.0)
    if spread is not None and spread > SPREAD_LIMIT:
        score *= SPREAD_LIMIT / spread
    return score


def exclusion_reason(price, end_date, active, now: datetime | None = None) -> str | None:
    """None if the market qualifies for the index, else the first failed criterion."""
    if active is False:
        return "inactive"
    if price is None:
        return "no_price"
    if not DEFAULT_MIN_PRICE <= price <= DEFAULT_MAX_PRICE:
        return "price_outside_corridor"
    if end_date is None:
        return "no_end_date"
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    if end_date <= (now or utcnow()):
        return "ended"
    return None


def harmonize(market, now: datetime | None = None) -> HarmonizedMarket:
    """Derive spread, score and eligibility from a NormalizedMarket."""
    spread = spread_of(market.best_bid, market.best_ask)
    depth = market.liquidity or 0.0
    volume_24h = market.volume_24h or 0.0
    reason = exclusion_reason(market.price, market.end_date, market.active, now)
    return HarmonizedMarket(
        source=market.source,
        external_id=market.external_id,
        title=market.title,
        price=market.price,
        best_bid=market.best_bid,
        best_ask=market.best_ask,
        spread=spread,
        depth_usd=depth,
        volume_24h_usd=volume_24h,
        liquidity_score=liquidity_score(depth, volume_24h, spread),
        end_date=market.end_date,
        active=market.active,
        eligible=reason is None,
        exclusion_reason=reason,
        tags=list(market.tags or []),
        url=market.url,
    )
