"""Daily market snapshot: fetch both sources in one pass and persist the harmonized state.

Every run stores all active markets of the day (eligible or not, with the
exclusion reason), so future backtests can replay any filter. A second run
on the same day replaces that day's rows. Sources are fetched without the
dashboard's liquidity floor and with a small pause between pages.
"""

import logging
from datetime import date, datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from backend.app.clients.kalshi import KalshiClient
from backend.app.clients.polymarket import PolymarketClient
from backend.app.eui.harmonize import harmonize
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.models import utcnow

logger = logging.getLogger(__name__)

REQUEST_DELAY_SECONDS = 0.2
POLYMARKET_MAX_PAGES = 50
KALSHI_MAX_PAGES = 30


def default_clients() -> list:
    polymarket = PolymarketClient(liquidity_floor=None, request_delay=REQUEST_DELAY_SECONDS)
    polymarket.max_pages = POLYMARKET_MAX_PAGES
    kalshi = KalshiClient(liquidity_floor=None, request_delay=REQUEST_DELAY_SECONDS)
    kalshi.max_pages = KALSHI_MAX_PAGES
    return [polymarket, kalshi]


def take_snapshot(session: Session, clients=None, snapshot_date: date | None = None, now: datetime | None = None) -> SnapshotRun:
    """Fetch, harmonize and store today's market state; never raises on source failures."""
    now = now or utcnow()
    snapshot_date = snapshot_date or now.date()
    run = SnapshotRun(snapshot_date=snapshot_date, started_at=now)
    session.add(run)
    session.flush()
    logger.info("snapshot run %d for %s started", run.id, snapshot_date)

    counts: dict[str, int] = {}
    unique: dict[tuple[str, str], object] = {}
    try:
        for client in clients or default_clients():
            markets = client.fetch_markets()
            counts[client.source] = len(markets)
            for m in markets:  # sources may list a market twice (shared events, page overlap): keep the first
                unique.setdefault((m.source, m.external_id), harmonize(m, now))
    except Exception as exc:  # a crashing client must not leave the run marked "running"
        logger.exception("snapshot run %d failed while fetching", run.id)
        run.status, run.note, run.finished_at = "failed", f"fetch error: {exc}"[:512], utcnow()
        session.commit()
        return run
    harmonized = list(unique.values())

    session.execute(delete(MarketSnapshot).where(MarketSnapshot.snapshot_date == snapshot_date))
    rows = [
        dict(
            run_id=run.id, snapshot_date=snapshot_date, source=h.source, external_id=h.external_id,
            title=h.title[:512], tags=h.tags, url=h.url, price=h.price, best_bid=h.best_bid,
            best_ask=h.best_ask, spread=h.spread, depth_usd=h.depth_usd, volume_24h_usd=h.volume_24h_usd,
            liquidity_score=h.liquidity_score, end_date=h.end_date, active=h.active, eligible=h.eligible,
            exclusion_reason=h.exclusion_reason, fetched_at=now,
        )
        for h in harmonized
    ]
    for start in range(0, len(rows), 5000):  # bulk insert: ~100k markets per day
        session.execute(insert(MarketSnapshot), rows[start : start + 5000])
    run.source_counts = counts
    run.eligible_count = sum(1 for h in harmonized if h.eligible)
    empty = [s for s, n in counts.items() if n == 0]
    run.status = "failed" if len(empty) == len(counts) else ("partial" if empty else "ok")
    run.note = f"no markets from: {', '.join(empty)}" if empty else None
    run.finished_at = utcnow()
    session.commit()
    logger.info("snapshot run %d finished: %s, counts=%s, eligible=%d", run.id, run.status, counts, run.eligible_count)
    return run


def snapshot_rows(session: Session, day: date, eligible_only: bool = False) -> list[MarketSnapshot]:
    stmt = select(MarketSnapshot).where(MarketSnapshot.snapshot_date == day)
    if eligible_only:
        stmt = stmt.where(MarketSnapshot.eligible.is_(True))
    return list(session.scalars(stmt))


def latest_snapshot_date(session: Session) -> date | None:
    return session.scalar(select(func.max(MarketSnapshot.snapshot_date)))


def recent_runs(session: Session, limit: int = 30) -> list[SnapshotRun]:
    return list(session.scalars(select(SnapshotRun).order_by(SnapshotRun.id.desc()).limit(limit)))
