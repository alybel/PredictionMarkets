"""History check of the index agent: markets with a short stored history get their past days from the source.

The daily snapshots only reach back to the first daily run (often two days), while Polymarket and Kalshi
publish daily price histories over the whole life of a market. Before the agent answers, the relevant
candidates (term hits or pinned, most hits and turnover first) with fewer than ``SHORT_DAYS`` stored days
are looked up at the source in parallel and their missing days are stored as backfill rows
(``backfill.service``), so pool, chains and blueprint see the real reach. Live-only markets serve as their
own template. A market found without more history is not asked again on the same day.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from backend.app.agent.pool import Candidate, score
from backend.app.backfill.service import HistoryFetcher, backfill_markets, history_fetcher
from backend.app.catalog.grading import MIN_DAYS
from backend.app.eui.models import MarketSnapshot
from backend.app.history import MAX_DAYS
from backend.app.models import utcnow

logger = logging.getLogger(__name__)

SHORT_DAYS = MIN_DAYS  # below this the stored history is too short for an index (catalog rule)
MAX_CHECK = 20         # source lookups per agent turn (Polymarket needs two requests each)
WORKERS = 8
_exhausted: dict[str, date] = {}  # key -> day it was checked without finding more history


def short_candidates(pool: list[Candidate], terms: list[str], pinned=(), limit: int = MAX_CHECK, today: date | None = None) -> list[Candidate]:
    """Relevant candidates with a short stored history, most term hits and turnover first."""
    today = today or utcnow().date()
    pinned = set(pinned)
    ranked = sorted(((score(c.title, (), terms), c) for c in pool), key=lambda r: (-r[0], -r[1].avg_volume))
    return [c for n, c in ranked if (n or c.key in pinned) and c.days < SHORT_DAYS and _exhausted.get(c.key) != today][:limit]


def _templates(session: Session, picks: list[Candidate], markets) -> list[MarketSnapshot]:
    """Latest stored row per market; live-only markets become a transient template (never added to the session)."""
    m = MarketSnapshot
    latest = select(m.source, m.external_id, func.max(m.snapshot_date).label("d")).where(
        or_(*[and_(m.source == c.source, m.external_id == c.external_id) for c in picks])).group_by(m.source, m.external_id).subquery()
    rows = session.scalars(select(m).join(latest, and_(m.source == latest.c.source, m.external_id == latest.c.external_id,
                                                       m.snapshot_date == latest.c.d)))
    by_key = {f"{r.source}:{r.external_id}": r for r in rows}
    live = {f"{x.source}:{x.external_id}": x for x in markets}
    out = []
    for c in picks:
        if c.key in by_key:
            out.append(by_key[c.key])
        elif c.key in live:
            x = live[c.key]
            out.append(MarketSnapshot(source=x.source, external_id=x.external_id, title=x.title, tags=list(x.tags or []), url=x.url,
                                      volume_24h_usd=float(x.volume_24h or 0.0), end_date=x.end_date))
    return out


def extend_history(session: Session, pool: list[Candidate], markets, terms: list[str], fetcher: HistoryFetcher,
                   pinned=(), days: int = MAX_DAYS, limit: int = MAX_CHECK, today: date | None = None) -> dict:
    """Load the missing past days of short-history candidates from their source; summary for the reply."""
    today = today or utcnow().date()
    picks = short_candidates(pool, terms, pinned, limit, today)
    summary = {"checked": 0, "extended": 0, "rows_added": 0, "markets": []}
    if not picks:
        return summary
    templates = _templates(session, picks, markets)
    start = today - timedelta(days=days)
    fetcher = history_fetcher(session, fetcher, ((t.source, t.external_id) for t in templates), start, "agent")
    with ThreadPoolExecutor(min(WORKERS, len(templates) or 1)) as pool_exec:
        found = dict(zip(((t.source, t.external_id) for t in templates),
                         pool_exec.map(lambda t: fetcher(t.source, t.external_id, start), templates)))
    result = backfill_markets(session, templates, days, lambda s, e, _: found.get((s, e), {}), today)
    for t in templates:
        key, points = f"{t.source}:{t.external_id}", [d for d in found[(t.source, t.external_id)] if start <= d < today]
        if points:
            summary["markets"].append({"ref": key, "title": t.title, "first_date": min(points).isoformat(), "days": len(points)})
        else:
            _exhausted[key] = today
    summary.update(checked=len(templates), extended=len(summary["markets"]), rows_added=result["rows_added"])
    logger.info("agent history check: %d short markets asked at the source, %d with history, %d rows added",
                summary["checked"], summary["extended"], summary["rows_added"])
    return summary
