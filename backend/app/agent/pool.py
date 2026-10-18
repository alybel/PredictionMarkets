"""Candidate pool of the index agent: stored markets with history plus live markets, matched to search terms.

History matters more than the current state (chaining needs expired markets), so the stored daily snapshots
are searched first; live markets add the current links and the source event (``event_key``). Pinned keys
(markets of the previous blueprint) are always included so a refined blueprint keeps its components.
"""

import logging
import re
from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from backend.app.eui.models import MarketSnapshot

logger = logging.getLogger(__name__)

STORED_LIMIT = 400
LIVE_LIMIT = 150


@dataclass(frozen=True)
class Candidate:
    source: str
    external_id: str
    title: str
    first_date: date | None = None
    last_date: date | None = None
    days: int = 0
    avg_volume: float = 0.0
    end_date: date | None = None
    price: float | None = None
    live: bool = False
    event_key: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"


def score(title: str, tags, terms: list[str]) -> int:
    """Terms found at a word start ("rate" hits "rates", not "corporate")."""
    text = (title + " " + " ".join(tags or ())).lower()
    return sum(1 for t in terms if re.search(r"\b" + re.escape(t), text))


def stored(session: Session, terms: list[str], pinned: list[tuple[str, str]] = ()) -> list[Candidate]:
    """Stored markets whose title contains a term (longest history first) plus the pinned ones."""
    m = MarketSnapshot
    conds = [func.lower(m.title).like(f"%{t}%") for t in terms]
    conds += [and_(m.source == s, m.external_id == e) for s, e in pinned]
    if not conds:
        return []
    stmt = (select(m.source, m.external_id, func.max(m.title), func.min(m.snapshot_date), func.max(m.snapshot_date), func.count(),
                   func.avg(m.volume_24h_usd), func.max(m.end_date))
            .where(or_(*conds)).group_by(m.source, m.external_id).order_by(func.count().desc()).limit(STORED_LIMIT))
    out = [Candidate(s, e, t, a, b, n, float(v or 0.0), end.date() if end else None)
           for s, e, t, a, b, n, v, end in session.execute(stmt)]
    logger.info("agent pool: %d stored markets for %d terms, %d pinned", len(out), len(terms), len(pinned))
    return out


def live(markets, terms: list[str], pinned: set[str] = frozenset()) -> list[Candidate]:
    """Live markets matching a term (most hits, then most turnover), pinned ones always."""
    ranked = sorted(((score(m.title, m.tags, terms), m) for m in markets), key=lambda r: (-r[0], -(r[1].volume_24h or 0.0)))
    hits = [m for n, m in ranked if n][:LIVE_LIMIT]
    taken = {id(m) for m in hits}
    hits += [m for m in markets if f"{m.source}:{m.external_id}" in pinned and id(m) not in taken]
    return [Candidate(m.source, m.external_id, m.title, avg_volume=float(m.volume_24h or 0.0),
                      end_date=m.end_date.date() if m.end_date else None, price=m.price, live=True,
                      event_key=getattr(m, "event_key", None)) for m in hits]


def gather(session: Session, markets, terms: list[str], pinned: list[str] = ()) -> list[Candidate]:
    """Stored and live candidates merged by key: history from the store, live state and event from the source."""
    pairs = [tuple(k.split(":", 1)) for k in pinned if ":" in k]
    merged = {c.key: c for c in stored(session, terms, pairs)}
    for c in live(markets, terms, set(pinned)):
        old = merged.get(c.key)
        merged[c.key] = c if old is None else replace(old, price=c.price, live=True, event_key=c.event_key,
                                                      end_date=old.end_date or c.end_date)
    logger.info("agent pool: %d candidates (%d live)", len(merged), sum(1 for c in merged.values() if c.live))
    return list(merged.values())
