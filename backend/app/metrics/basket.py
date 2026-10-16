"""Daily series of a market-basket metric from the stored daily market snapshots.

Only point-in-time rows enter (``quality.pit``); rows that violate it are returned separately so the
lookahead check can block the metric. Value of a day = liquidity-weighted price (24h turnover) of the
basket's markets, simple average if none carries turnover. Liquidity of a day = summed 24h turnover of
the basket's markets (None if none carries turnover).
"""

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.eui.models import MarketSnapshot
from backend.app.metrics.catalog import MetricDef, MetricPoint
from backend.app.quality.pit import snapshot_violation


def basket_points(session: Session, defn: MetricDef, today: date) -> tuple[list[MetricPoint], list[dict]]:
    """(points oldest first, point-in-time violations)."""
    wanted = set(defn.markets)
    stmt = select(MarketSnapshot).where(MarketSnapshot.external_id.in_({e for _, e in wanted}),
                                        MarketSnapshot.price.is_not(None))
    by_day, violations = defaultdict(list), []
    for row in session.scalars(stmt):
        if (row.source, row.external_id) not in wanted:
            continue
        reason = snapshot_violation(row.snapshot_date, row.fetched_at, row.exclusion_reason, today)
        if reason:
            violations.append({"quelle": row.source, "markt": row.external_id, "tag": row.snapshot_date.isoformat(), "grund": reason})
        else:
            by_day[row.snapshot_date].append(row)
    points = []
    for day in sorted(by_day):
        rows = by_day[day]
        total = sum(r.volume_24h_usd or 0.0 for r in rows)
        value = (sum(r.price * (r.volume_24h_usd or 0.0) for r in rows) / total if total > 0
                 else sum(r.price for r in rows) / len(rows))
        volumes = [r.volume_24h_usd for r in rows if r.volume_24h_usd is not None]
        points.append(MetricPoint(day, value, day, None, False, sum(volumes) if volumes else None))
    return points, violations
