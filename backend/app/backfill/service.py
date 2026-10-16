"""Backfill of past daily snapshots, so index histories reach back before the first daily run.

The markets of an index (the rule's funnel on the latest snapshot day, largest 24h
volume first) get one ``eui_market_snapshots`` row per past day from the source's
price history. Title, tags, link and end date come from the latest snapshot; the 24h
volume is the day's turnover where the source publishes it (Kalshi), else the current
one (Polymarket). Rows are marked ``exclusion_reason = "backfill"`` and never eligible,
so the Economic Uncertainty Index ignores them. Existing rows are never touched and
today is left to the daily run. Only markets of the latest real snapshot can be backfilled.

CLI: ``python -m backend.app.backfill.service`` [--index NAME] [--days 365] [--limit 50]
"""

import argparse
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.backfill.sources import DayPoint, fetch_history
from backend.app.engine import apply_funnel, list_rules
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.history import _as_market
from backend.app.logging_setup import configure_logging
from backend.app.models import IndexRule, utcnow
from backend.app.quality.pit import BACKFILL

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
BACKFILL_DAYS = 365  # default reach of a backfill run (the history window itself allows more)
MAX_LIMIT = 200

HistoryFetcher = Callable[[str, str, date], dict[date, DayPoint]]


def latest_live_date(session: Session) -> date | None:
    """Latest day of a real daily snapshot (backfilled rows never serve as template)."""
    return session.scalar(select(func.max(MarketSnapshot.snapshot_date)).where(
        func.coalesce(MarketSnapshot.exclusion_reason, "") != BACKFILL))


def index_markets(session: Session, rule: IndexRule, limit: int = DEFAULT_LIMIT) -> list[MarketSnapshot]:
    """Current constituents of the rule on the latest real snapshot day, largest 24h volume first."""
    day = latest_live_date(session)
    if day is None:
        return []
    rows = list(session.scalars(select(MarketSnapshot).where(MarketSnapshot.snapshot_date == day)))
    by_key = {(r.source, r.external_id): r for r in rows}
    selected, _ = apply_funnel(rule, [_as_market(r) for r in rows], now=datetime.combine(day, time.min, tzinfo=timezone.utc))
    picked = [by_key[(m.source, m.external_id)] for m in selected]
    return sorted(picked, key=lambda r: r.volume_24h_usd or 0.0, reverse=True)[:limit]


def _row(template: MarketSnapshot, run_id: int, day: date, point: DayPoint, now: datetime) -> MarketSnapshot:
    volume = point.volume_usd if point.volume_usd is not None else template.volume_24h_usd
    return MarketSnapshot(run_id=run_id, snapshot_date=day, source=template.source, external_id=template.external_id,
                          title=template.title, tags=list(template.tags or []), url=template.url, price=point.price,
                          volume_24h_usd=volume or 0.0, depth_usd=0.0, liquidity_score=0.0, end_date=template.end_date,
                          active=True, eligible=False, exclusion_reason=BACKFILL, fetched_at=now)


def backfill_markets(session: Session, templates: list[MarketSnapshot], days: int = BACKFILL_DAYS,
                     fetcher: HistoryFetcher = fetch_history, today: date | None = None) -> dict:
    """Add the missing past days of the given markets; one failing market never stops the run."""
    now = utcnow()
    today = today or now.date()
    start = today - timedelta(days=days)
    summary = {"markets": len(templates), "markets_with_history": 0, "rows_added": 0, "days_added": 0}
    run: SnapshotRun | None = None
    new_days: set[date] = set()
    known_days = set(session.scalars(select(MarketSnapshot.snapshot_date).distinct()))
    for template in templates:
        points = {d: p for d, p in fetcher(template.source, template.external_id, start).items() if start <= d < today}
        if not points:
            continue
        summary["markets_with_history"] += 1
        stored = set(session.scalars(select(MarketSnapshot.snapshot_date).where(
            MarketSnapshot.source == template.source, MarketSnapshot.external_id == template.external_id,
            MarketSnapshot.snapshot_date >= start)))
        missing = sorted(d for d in points if d not in stored)
        if not missing:
            continue
        if run is None:
            run = SnapshotRun(snapshot_date=today, started_at=now, status=BACKFILL, note=f"backfill from {start.isoformat()}")
            session.add(run)
            session.flush()
        session.add_all(_row(template, run.id, d, points[d], now) for d in missing)
        session.commit()
        summary["rows_added"] += len(missing)
        new_days.update(d for d in missing if d not in known_days)
    summary["days_added"] = len(new_days)
    if run is not None:
        run.finished_at, run.eligible_count = utcnow(), 0
        run.source_counts = {"rows": summary["rows_added"], "markets": summary["markets_with_history"]}
        session.commit()
    logger.info("backfill from %s: %d markets, %d with history, %d rows added, %d new snapshot days",
                start, summary["markets"], summary["markets_with_history"], summary["rows_added"], summary["days_added"])
    return summary


def backfill_index(session: Session, rule: IndexRule, days: int = BACKFILL_DAYS, limit: int = DEFAULT_LIMIT,
                   fetcher: HistoryFetcher = fetch_history, today: date | None = None) -> dict:
    templates = index_markets(session, rule, limit)
    logger.info("backfill of %r: %d markets over %d days", rule.index_name, len(templates), days)
    return {"index_name": rule.index_name, "days": days, **backfill_markets(session, templates, days, fetcher, today),
            "status": history_status(session)}


def history_status(session: Session) -> dict:
    """How far the stored daily snapshots reach back and how many rows were backfilled."""
    first, last, count = session.execute(select(func.min(MarketSnapshot.snapshot_date), func.max(MarketSnapshot.snapshot_date),
                                                func.count(func.distinct(MarketSnapshot.snapshot_date)))).one()
    backfilled = session.scalar(select(func.count(MarketSnapshot.id)).where(MarketSnapshot.exclusion_reason == BACKFILL))
    return {"first_date": first.isoformat() if first else None, "last_date": last.isoformat() if last else None,
            "snapshot_days": count, "backfilled_rows": backfilled}


def main() -> None:
    from backend.app.db import make_engine, make_session_factory

    parser = argparse.ArgumentParser(description="Backfill past daily snapshots of the index markets")
    parser.add_argument("--index", help="one index (default: all saved indices)")
    parser.add_argument("--days", type=int, default=BACKFILL_DAYS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="markets per index, largest 24h volume first")
    args = parser.parse_args()
    configure_logging()
    with make_session_factory(make_engine())() as session:
        for rule in list_rules(session):
            if args.index in (None, rule.index_name):
                print(backfill_index(session, rule, args.days, args.limit))


if __name__ == "__main__":
    main()
