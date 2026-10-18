"""Bundle markets into events and read them back.

``group_markets`` groups normalized markets by their source event (``event_key``); markets without one
stay standalone. ``sync_events`` upserts the groups into ``events``/``event_markets`` (daily snapshot and
``POST /events/sync``), so an event always holds every outcome market seen so far.
"""

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.events.models import Event, EventMarket
from backend.app.models import utcnow

logger = logging.getLogger(__name__)

PAGE_LIMIT = 100


def group_markets(markets) -> tuple[dict[tuple[str, str], list], list]:
    """(source, event_key) -> outcome markets (first occurrence wins), plus the standalone markets."""
    events: dict[tuple[str, str], dict] = {}
    standalone = []
    for m in markets:
        if not m.event_key:
            standalone.append(m)
            continue
        events.setdefault((m.source, m.event_key), {}).setdefault(m.external_id, m)
    return {key: list(outcomes.values()) for key, outcomes in events.items()}, standalone


def sync_events(session: Session, markets, now: datetime | None = None) -> dict:
    """Upsert events and their outcome markets (no commit); returns counts for the log."""
    now = now or utcnow()
    groups, standalone = group_markets(markets)
    known = {(e.source, e.event_key): e for e in session.scalars(select(Event))}
    links = {(l.source, l.external_id): l for l in session.scalars(select(EventMarket))}
    created = 0
    for (source, key), outcomes in groups.items():
        event = known.get((source, key))
        if event is None:
            event = Event(source=source, event_key=key, created_at=now, title="")
            session.add(event)
            created += 1
        event.title = (outcomes[0].event_title or outcomes[0].title)[:512]
        event.updated_at = now
        session.flush()
        for m in outcomes:
            link = links.get((m.source, m.external_id)) or EventMarket(source=m.source, external_id=m.external_id)
            link.event_id, link.title, link.outcome_label = event.id, m.title[:512], m.outcome_label
            link.price, link.volume_24h_usd, link.url, link.updated_at = m.price, m.liquidity, m.url, now
            session.add(link)
    session.flush()
    counts = {"events": len(groups), "created": created, "outcomes": sum(len(o) for o in groups.values()),
              "standalone": len(standalone)}
    logger.info("events synced: %s", counts)
    return counts


def _payload(event: Event, outcomes: list[EventMarket]) -> dict:
    return {"id": event.id, "source": event.source, "event_key": event.event_key, "title": event.title,
            "outcome_count": len(outcomes), "updated_at": event.updated_at.isoformat(),
            "outcomes": [{"source": o.source, "external_id": o.external_id, "title": o.title, "outcome_label": o.outcome_label,
                          "price": o.price, "volume_24h_usd": o.volume_24h_usd, "url": o.url} for o in outcomes]}


def _outcomes(session: Session, event_ids: list[int]) -> dict[int, list[EventMarket]]:
    out: dict[int, list[EventMarket]] = {i: [] for i in event_ids}
    stmt = select(EventMarket).where(EventMarket.event_id.in_(event_ids)).order_by(EventMarket.price.desc().nulls_last(), EventMarket.id)
    for o in session.scalars(stmt):
        out[o.event_id].append(o)
    return out


def list_events(session: Session, *, q: str = "", source: str = "", min_outcomes: int = 1, limit: int = 50, offset: int = 0) -> dict:
    """One page of events (largest first) with their outcome markets, plus the total."""
    n = func.count(EventMarket.id).label("n")
    stmt = select(Event, n).join(EventMarket, EventMarket.event_id == Event.id).group_by(Event.id).having(n >= min_outcomes)
    if q.strip():
        stmt = stmt.where(func.lower(Event.title).like(f"%{q.strip().lower()}%"))
    if source:
        stmt = stmt.where(Event.source == source)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page = [e for e, _ in session.execute(stmt.order_by(n.desc(), Event.id).limit(min(limit, PAGE_LIMIT)).offset(offset))]
    outcomes = _outcomes(session, [e.id for e in page])
    logger.info("events q=%r source=%r min_outcomes=%d offset=%d: %d of %d", q, source, min_outcomes, offset, len(page), total)
    return {"total": total, "offset": offset, "items": [_payload(e, outcomes[e.id]) for e in page]}


def get_event(session: Session, event_id: int) -> dict | None:
    event = session.get(Event, event_id)
    if event is None:
        return None
    return _payload(event, _outcomes(session, [event.id])[event.id])
