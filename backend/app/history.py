"""Index history (backtest): replay an index rule over the stored daily market snapshots.

The daily EUI run stores every active market of the day (``eui_market_snapshots``).
Only point-in-time rows enter (``quality.pit``: observed on their day before the US close, never a future
date), so no value of a day carries information from a later moment. For each snapshot day the rule's funnel runs against that day's markets — the
end-date check uses the start of that day, not today — and the selected markets are
aggregated exactly like the live index (liquidity-weighted, liquidity = 24h turnover).

GET  /indices/{name}/history           -> history of the stored rule (?days, default 90)
POST /indices/{name}/history/preview   -> history of the rule in the body; DB untouched
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.aggregation import aggregate
from backend.app.deps import get_session
from backend.app.engine import apply_funnel, build_rule, effective_min_liquidity, get_rule
from backend.app.eui.models import MarketSnapshot
from backend.app.models import IndexRule, utcnow
from backend.app.quality.pit import snapshot_violation
from backend.app.schemas import RuleSpec

logger = logging.getLogger(__name__)
router = APIRouter(tags=["history"])

DEFAULT_DAYS = 90
MAX_DAYS = 1825  # 5 years: the Fed metrics reach back to 2023 (1015-2258)


@dataclass(frozen=True)
class SnapshotMarket:
    """A stored snapshot row in the shape the rule engine and the aggregation expect."""

    source: str
    external_id: str
    title: str
    price: float | None
    liquidity: float | None
    end_date: datetime | None
    url: str | None = None
    tags: list[str] = field(default_factory=list)
    category: str | None = None
    volume: float | None = None


def _as_market(row: MarketSnapshot) -> SnapshotMarket:
    return SnapshotMarket(source=row.source, external_id=row.external_id, title=row.title, price=row.price,
                          liquidity=row.volume_24h_usd, end_date=row.end_date, url=row.url, tags=list(row.tags or []))


def _snapshot_dates(session: Session, days: int, today: date) -> list[date]:
    stmt = select(MarketSnapshot.snapshot_date).distinct().where(MarketSnapshot.snapshot_date <= today).order_by(MarketSnapshot.snapshot_date.desc()).limit(days)
    return sorted(session.scalars(stmt))


def _rows_by_day(session: Session, rule: IndexRule, days: list[date]) -> dict[date, list[MarketSnapshot]]:
    """Point-in-time snapshot rows of the given days, pre-filtered in SQL by the rule's liquidity floor and price corridor."""
    stmt = select(MarketSnapshot).where(
        MarketSnapshot.snapshot_date.in_(days),
        MarketSnapshot.volume_24h_usd >= effective_min_liquidity(rule.min_liquidity),
        or_(MarketSnapshot.price.is_(None), MarketSnapshot.price.between(rule.min_price, rule.max_price)),
    )
    grouped: dict[date, list[MarketSnapshot]] = {d: [] for d in days}
    today, blocked = utcnow().date(), 0
    for row in session.scalars(stmt):
        if snapshot_violation(row.snapshot_date, row.fetched_at, row.exclusion_reason, today):
            blocked += 1
            continue
        grouped[row.snapshot_date].append(row)
    if blocked:
        logger.warning("point-in-time guard: %d snapshot rows of %r blocked (observed outside their day or after the US close)",
                       blocked, rule.index_name)
    return grouped


def _summary(points: list[dict]) -> dict:
    values = [p["value"] for p in points if p["value"] is not None]
    if not values:
        return {"first": None, "last": None, "change": None, "min": None, "max": None}
    return {"first": values[0], "last": values[-1], "change": values[-1] - values[0], "min": min(values), "max": max(values)}


def daily_selection(session: Session, rule: IndexRule, days: int = DEFAULT_DAYS) -> list[tuple[date, list[SnapshotMarket]]]:
    """The rule's selected markets per snapshot day (oldest first) — the input of every backtest value."""
    dates = _snapshot_dates(session, days, utcnow().date())
    grouped = _rows_by_day(session, rule, dates) if dates else {}
    out = []
    for day in dates:
        start_of_day = datetime.combine(day, time.min, tzinfo=timezone.utc)
        selected, _ = apply_funnel(rule, [_as_market(r) for r in grouped[day]], now=start_of_day)
        out.append((day, selected))
    return out


def index_history(session: Session, rule: IndexRule, days: int = DEFAULT_DAYS) -> dict:
    """One aggregated value per snapshot day (oldest first) plus a short summary."""
    points = []
    for day, selected in daily_selection(session, rule, days):
        result = aggregate(rule.index_name, selected)
        points.append({"date": day.isoformat(), "value": result.value, "market_count": result.market_count,
                       "total_liquidity": result.total_liquidity})
    logger.info("history of %r over %d snapshot days (requested %d)", rule.index_name, len(points), days)
    return {"index_name": rule.index_name, "days": days, "points": points, "summary": _summary(points)}


@router.get("/indices/{index_name}/history")
def stored_history(index_name: str, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS), session=Depends(get_session)) -> dict:
    rule = get_rule(session, index_name)
    if rule is None:
        raise HTTPException(404, f"unknown index {index_name!r}")
    return index_history(session, rule, days)


@router.post("/indices/{index_name}/history/preview")
def preview_history(index_name: str, body: RuleSpec, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
                    session=Depends(get_session)) -> dict:
    """History of an unsaved rule (parameter panel preview); nothing is stored."""
    return index_history(session, build_rule(index_name, **body.validated()), days)
