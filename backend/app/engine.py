"""Index rule engine: selects markets for an index by configurable rules.

A rule (see backend.app.models.IndexRule) combines tag filters, keyword
filters, a liquidity threshold, a price corridor and an end-date-in-the-
future requirement. Rules are stored per index in the database, never
hardcoded. Matching works on any normalized market object exposing
``tags``/``category``, ``title``, ``liquidity``, ``price`` and ``end_date``
— both NormalizedMarket and the Market ORM row qualify.

The filters run as a funnel in a fixed order (tags -> keywords ->
liquidity -> price corridor -> end date); ``apply_funnel`` reports how
many markets survive each stage.
"""

from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE, MIN_LIQUIDITY_USD, IndexRule, utcnow


def _tags_ok(rule: IndexRule, market) -> bool:
    """Empty list: no restriction. Otherwise any rule tag must appear in the market's tags."""
    if not rule.categories:
        return True
    wanted = {c.lower() for c in rule.categories}
    return bool(wanted & market_tags(market))


def _keywords_ok(rule: IndexRule, market) -> bool:
    """Empty list: no restriction. Otherwise any keyword must occur in the title."""
    if not rule.keywords:
        return True
    title = (market.title or "").lower()
    return any(k.lower() in title for k in rule.keywords)


def _liquidity_ok(rule: IndexRule, market) -> bool:
    """Unknown liquidity counts as 0; the rule can never go below the global floor."""
    return (market.liquidity or 0.0) >= effective_min_liquidity(rule.min_liquidity)


def _price_ok(rule: IndexRule, market) -> bool:
    """Unknown price passes; a missing bound means no restriction."""
    price = market.price
    if price is None:
        return True
    lower = rule.min_price if rule.min_price is not None else 0.0
    upper = rule.max_price if rule.max_price is not None else 1.0
    return lower <= price <= upper


def _end_date_ok(rule: IndexRule, market) -> bool:
    """Markets whose end date lies in the past are excluded; naive datetimes count as UTC."""
    end_date = market.end_date
    if end_date is None:
        return True
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    return end_date > utcnow()


FUNNEL_STAGES = (
    ("tags", _tags_ok),
    ("keywords", _keywords_ok),
    ("liquidity", _liquidity_ok),
    ("price", _price_ok),
    ("end_date", _end_date_ok),
)


def market_tags(market) -> set[str]:
    """Lower-cased tags of a market; the legacy ``category`` counts as a tag."""
    tags = {t.lower() for t in (getattr(market, "tags", None) or []) if isinstance(t, str)}
    category = getattr(market, "category", None)
    if category:
        tags.add(category.lower())
    return tags


def effective_min_liquidity(min_liquidity) -> float:
    return max(min_liquidity or 0.0, MIN_LIQUIDITY_USD)


def market_matches(rule: IndexRule, market) -> bool:
    """True if the market passes all filters of the rule."""
    return all(check(rule, market) for _, check in FUNNEL_STAGES)


def apply_funnel(rule: IndexRule, markets) -> tuple[list, dict[str, int]]:
    """Selected markets plus the survivor count after each funnel stage."""
    remaining = list(markets)
    funnel = {"loaded": len(remaining)}
    for name, check in FUNNEL_STAGES:
        remaining = [m for m in remaining if check(rule, m)]
        funnel[name] = len(remaining)
    return remaining, funnel


def select_markets(rule: IndexRule, markets) -> list:
    """Markets from the given normalized data that belong to the rule's index."""
    return apply_funnel(rule, markets)[0]


def build_rule(
    index_name: str,
    categories: list[str] | None = None,
    keywords: list[str] | None = None,
    min_liquidity: float = MIN_LIQUIDITY_USD,
    min_price: float = DEFAULT_MIN_PRICE,
    max_price: float = DEFAULT_MAX_PRICE,
) -> IndexRule:
    """Transient rule (not attached to any session), e.g. for a preview."""
    return IndexRule(
        index_name=index_name,
        categories=categories or [],
        keywords=keywords or [],
        min_liquidity=effective_min_liquidity(min_liquidity),
        min_price=min_price,
        max_price=max_price,
    )


def upsert_rule(
    session: Session,
    index_name: str,
    categories: list[str] | None = None,
    keywords: list[str] | None = None,
    min_liquidity: float = MIN_LIQUIDITY_USD,
    min_price: float = DEFAULT_MIN_PRICE,
    max_price: float = DEFAULT_MAX_PRICE,
) -> IndexRule:
    """Create or update the rule stored for an index (liquidity lifted to the global floor)."""
    rule = get_rule(session, index_name)
    if rule is None:
        rule = IndexRule(index_name=index_name)
        session.add(rule)
    rule.categories = categories or []
    rule.keywords = keywords or []
    rule.min_liquidity = effective_min_liquidity(min_liquidity)
    rule.min_price = min_price
    rule.max_price = max_price
    session.commit()
    return rule


def delete_rule(session: Session, index_name: str) -> bool:
    """Remove the stored rule of an index; False if no such index exists."""
    rule = get_rule(session, index_name)
    if rule is None:
        return False
    session.delete(rule)
    session.commit()
    return True


def tag_counts(markets) -> list[dict]:
    """Tags actually delivered by the population with their market counts.

    Grouped case-insensitively (first spelling wins), most frequent first.
    """
    counts: dict[str, dict] = {}
    for market in markets:
        for tag in {t for t in (getattr(market, "tags", None) or []) if isinstance(t, str) and t.strip()}:
            entry = counts.setdefault(tag.lower(), {"tag": tag, "count": 0})
            entry["count"] += 1
    return sorted(counts.values(), key=lambda e: (-e["count"], e["tag"].lower()))


def get_rule(session: Session, index_name: str) -> IndexRule | None:
    return session.scalars(select(IndexRule).where(IndexRule.index_name == index_name)).first()


def list_rules(session: Session) -> list[IndexRule]:
    return list(session.scalars(select(IndexRule).order_by(IndexRule.index_name)))


def markets_for_index(session: Session, index_name: str, markets) -> list:
    """Apply the stored rule of an index to normalized market data.

    Raises LookupError if no rule is stored for the index.
    """
    rule = get_rule(session, index_name)
    if rule is None:
        raise LookupError(f"no rule stored for index {index_name!r}")
    return select_markets(rule, markets)
