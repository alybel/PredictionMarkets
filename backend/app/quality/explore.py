"""Browse the stored daily rows of single markets: search by title, then the full row history of one market."""

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.eui.models import MarketSnapshot
from backend.app.models import utcnow
from backend.app.quality.checks import JUMP
from backend.app.quality.pit import BACKFILL, as_utc, snapshot_violation

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 50


def search_markets(session: Session, query: str, limit: int = 20) -> list[dict]:
    """Markets whose title contains ``query`` (case-insensitive), most stored days first."""
    m = MarketSnapshot
    pattern = f"%{query.strip().lower()}%"
    stmt = (select(m.source, m.external_id, func.max(m.title), func.count(), func.min(m.snapshot_date), func.max(m.snapshot_date))
            .where(func.lower(m.title).like(pattern)).group_by(m.source, m.external_id)
            .order_by(func.count().desc(), m.source, m.external_id).limit(min(limit, SEARCH_LIMIT)))
    out = [{"source": s, "external_id": e, "title": t, "days": n, "first_date": a.isoformat(), "last_date": b.isoformat()}
           for s, e, t, n, a, b in session.execute(stmt)]
    logger.info("quality market search %r: %d hits", query, len(out))
    return out


def market_rows(session: Session, source: str, external_id: str, today: date | None = None) -> list[dict]:
    """Every stored day of one market (oldest first) with origin, observation time and point-in-time verdict."""
    today = today or utcnow().date()
    rows = session.scalars(select(MarketSnapshot).where(MarketSnapshot.source == source, MarketSnapshot.external_id == external_id)
                           .order_by(MarketSnapshot.snapshot_date))
    out, prev = [], None
    for r in rows:
        backfill = r.exclusion_reason == BACKFILL
        jump = r.price is not None and prev is not None and abs(r.price - prev) >= JUMP
        out.append({"date": r.snapshot_date.isoformat(), "price": r.price, "volume_24h_usd": r.volume_24h_usd,
                    "depth_usd": r.depth_usd, "origin": "backfill" if backfill else "live",
                    "fetched_at": as_utc(r.fetched_at).isoformat() if r.fetched_at else None,
                    "violation": snapshot_violation(r.snapshot_date, r.fetched_at, r.exclusion_reason, today), "jump": jump})
        prev = r.price if r.price is not None else prev
    logger.info("quality market rows %s/%s: %d days", source, external_id, len(out))
    return out
