"""Daily market snapshot: fetch both sources in one pass and persist the harmonized state.

Every run stores all active markets of the day (eligible or not, with the
exclusion reason), so future backtests can replay any filter. A second run
on the same day replaces that day's rows. Sources are fetched without the
dashboard's liquidity floor and with a small pause between pages: by default one
refresh request to the raw data layer (raw job "markets", profile "snapshot"),
then the clients read exactly that job's answers from ``raw_responses``.
"""

import logging
from datetime import date, datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from backend.app.eui.harmonize import harmonize
from backend.app.events.service import sync_events
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.models import utcnow
from backend.app.rawdata.providers import RawMarkets

logger = logging.getLogger(__name__)


def default_clients(session: Session, trigger: str = "snapshot") -> list:
    """Snapshot clients reading from raw data, after one refresh request to the raw layer."""
    return RawMarkets.for_session(session, trigger).clients("snapshot")


def take_snapshot(session: Session, clients=None, snapshot_date: date | None = None, now: datetime | None = None) -> SnapshotRun:
    """Fetch, harmonize and store today's market state; never raises on source failures."""
    now = now or utcnow()
    snapshot_date = snapshot_date or now.date()
    clients = clients or default_clients(session)  # before the run row: the raw job writes through its own connection
    run = SnapshotRun(snapshot_date=snapshot_date, started_at=now)
    session.add(run)
    session.flush()
    logger.info("snapshot run %d for %s started", run.id, snapshot_date)

    counts: dict[str, int] = {}
    unique: dict[tuple[str, str], object] = {}
    raw: dict[tuple[str, str], object] = {}
    try:
        for client in clients:
            markets = client.fetch_markets()
            counts[client.source] = len(markets)
            for m in markets:  # sources may list a market twice (shared events, page overlap): keep the first
                if (m.source, m.external_id) not in unique:
                    unique[(m.source, m.external_id)], raw[(m.source, m.external_id)] = harmonize(m, now), m
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
    _sync_events(session, run.id, list(raw.values()), now)
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


def _sync_events(session: Session, run_id: int, markets: list, now: datetime) -> None:
    """Bundle the day's markets into events; a failure is logged and never breaks the snapshot."""
    try:
        with session.begin_nested():
            sync_events(session, markets, now)
    except Exception:
        logger.exception("snapshot run %d: event bundling failed", run_id)
