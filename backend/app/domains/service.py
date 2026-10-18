"""Domains: definition (CRUD), membership rule, load run with full history and the overview figures.

Membership: a market belongs to a domain if its tags contain the subtopic (if set; the sources tag one subject
under different topics, e.g. Polymarket "Fed" under "Economy", Kalshi under "Economics"), else the topic, and, when
search terms are given, its title, event title or outcome label contains at least one of them (case-insensitive).

Load run: scan both sources (open and expired markets, ``discovery``), store new members, then load the full
daily history of every member not loaded yet (``backfill.service.backfill_markets``: only missing days are added,
existing rows are never touched, rows carry the backfill mark of the point-in-time rule). The daily job calls
``refresh_domains``, so new markets of a domain get their full history on the next run. Scan and histories come from
the raw data layer (refresh request, then read from ``raw_responses``) unless a scanner/fetcher is injected (tests).
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from backend.app.backfill.service import HistoryFetcher, backfill_markets, history_fetcher
from backend.app.domains.models import Domain, DomainMarket
from backend.app.eui.models import MarketSnapshot
from backend.app.events.tree import GENERAL
from backend.app.models import utcnow
from backend.app.quality.pit import as_utc
from backend.app.rawdata.providers import RawScanner

logger = logging.getLogger(__name__)

FULL_HISTORY_DAYS = 3650  # "full": every source point of the last ten years
MAX_LOAD = 300            # history lookups per domain and run (the rest follows on the next run)
WORKERS = 8
MAX_TERMS = 20


class DomainError(ValueError):
    """Invalid domain definition (HTTP 400)."""


# ---- definition ---------------------------------------------------------------------------

def clean_definition(name: str, topic: str, subtopic: str | None, terms) -> dict:
    name, topic, subtopic = (name or "").strip(), (topic or "").strip(), (subtopic or "").strip()
    if not name:
        raise DomainError("Name fehlt")
    if not topic:
        raise DomainError("Thema fehlt")
    cleaned: list[str] = []
    for t in terms or []:
        t = str(t).strip()
        if t and t.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(t[:100])
    if len(cleaned) > MAX_TERMS:
        raise DomainError(f"höchstens {MAX_TERMS} Suchbegriffe")
    return {"name": name[:120], "topic": topic, "subtopic": None if subtopic in ("", GENERAL) else subtopic, "terms": cleaned}


def check_topic(tree: list[dict], topic: str, subtopic: str | None) -> None:
    """The topic (and subtopic) must exist in the topic tree."""
    node = next((t for t in tree if t["name"].lower() == topic.lower()), None)
    if node is None:
        raise DomainError(f"unbekanntes Thema {topic!r}")
    if subtopic and not any(s["name"].lower() == subtopic.lower() for s in node["subtopics"]):
        raise DomainError(f"unbekanntes Unterthema {subtopic!r} in {topic!r}")


def get_domain(session: Session, domain_id: int) -> Domain | None:
    return session.get(Domain, domain_id)


def save_domain(session: Session, definition: dict, domain: Domain | None = None) -> Domain:
    """Create or update (commit); raises DomainError on a duplicate name."""
    clash = session.scalar(select(Domain).where(func.lower(Domain.name) == definition["name"].lower()))
    if clash is not None and clash is not domain:
        raise DomainError(f"Domäne {definition['name']!r} existiert bereits")
    now = utcnow()
    created = domain is None
    domain = domain or Domain(created_at=now)
    changed = created or (domain.topic, domain.subtopic, domain.terms) != (definition["topic"], definition["subtopic"], definition["terms"])
    for key, value in definition.items():
        setattr(domain, key, value)
    domain.updated_at = now
    session.add(domain)
    session.flush()
    if changed and not created:  # new rule: drop members that no longer match; the next load adds new ones (history stays)
        for member in session.scalars(select(DomainMarket).where(DomainMarket.domain_id == domain.id)):
            if not matches(domain, member):
                session.delete(member)
    session.commit()
    logger.info("domain %d %s: %s", domain.id, "created" if created else "updated", definition)
    return domain


def delete_domain(session: Session, domain: Domain) -> None:
    """Remove the definition and its member list; stored snapshot rows stay (raw data is never deleted)."""
    session.execute(delete(DomainMarket).where(DomainMarket.domain_id == domain.id))
    session.delete(domain)
    session.commit()
    logger.info("domain %d %r deleted", domain.id, domain.name)


# ---- membership and load run -----------------------------------------------------------------

def matches(domain: Domain, market) -> bool:
    tags = {t.strip().lower() for t in (market.tags or []) if isinstance(t, str)}
    if (domain.subtopic or domain.topic).lower() not in tags:
        return False
    if not domain.terms:
        return True
    text = " ".join(filter(None, [market.title, getattr(market, "event_title", None), getattr(market, "outcome_label", None)])).lower()
    return any(t.lower() in text for t in domain.terms)


def is_active(market, now: datetime) -> bool:
    if market.active is not None:
        return bool(market.active)
    end = as_utc(market.end_date)
    return end is None or end > now


def _store_members(session: Session, domain: Domain, markets: list, now: datetime) -> int:
    """Insert new members, refresh status of known ones; returns the number of new members."""
    known = {(m.source, m.external_id): m for m in session.scalars(select(DomainMarket).where(DomainMarket.domain_id == domain.id))}
    new = 0
    for m in markets:
        row = known.get((m.source, m.external_id))
        if row is None:
            row = known[(m.source, m.external_id)] = DomainMarket(domain_id=domain.id, source=m.source, external_id=m.external_id,
                                                                  found_at=now, attempts=0)
            session.add(row)
            new += 1
        row.title, row.url, row.tags, row.end_date, row.active = m.title[:512], m.url, list(m.tags or []), m.end_date, is_active(m, now)
    session.flush()
    return new


def _template(member: DomainMarket) -> MarketSnapshot:
    """Transient row carrying title, tags, link and end date into the backfill rows (never added to the session)."""
    return MarketSnapshot(source=member.source, external_id=member.external_id, title=member.title, tags=list(member.tags or []),
                          url=member.url, volume_24h_usd=0.0, end_date=member.end_date)


def _load_histories(session: Session, pending: list[DomainMarket], fetcher: HistoryFetcher, today: date) -> dict:
    """Full history of the pending members; marks loaded ones, counts empty answers per source."""
    start = today - timedelta(days=FULL_HISTORY_DAYS)
    fetcher = history_fetcher(session, fetcher, ((p.source, p.external_id) for p in pending), start, "domains")
    with ThreadPoolExecutor(min(WORKERS, len(pending) or 1)) as pool:
        found = dict(zip(((p.source, p.external_id) for p in pending), pool.map(lambda p: fetcher(p.source, p.external_id, start), pending)))
    result = backfill_markets(session, [_template(p) for p in pending], FULL_HISTORY_DAYS,
                              lambda s, e, _: found.get((s, e), {}), today)
    empty: dict[str, int] = {}
    now = utcnow()
    for p in pending:
        if any(start <= d < today for d in found[(p.source, p.external_id)]):
            p.loaded_at = now
        else:
            p.attempts += 1
            empty[p.source] = empty.get(p.source, 0) + 1
    session.commit()
    return {"rows_added": result["rows_added"], "loaded": sum(1 for p in pending if p.loaded_at == now), "empty": empty}


def load_domain(session: Session, domain: Domain, scanner=None, fetcher: HistoryFetcher | None = None,
                today: date | None = None, trigger: str = "manual") -> dict:
    """One load run: find members at both sources, load the full history of members not loaded yet."""
    now = utcnow()
    today = today or now.date()
    logger.info("domain %d %r: load run started (%s)", domain.id, domain.name, trigger)
    scanner = scanner or RawScanner.for_session(session, trigger)
    scan = scanner(domain.topic, domain.subtopic, list(domain.terms or []))
    sources: dict[str, dict] = {}
    members: dict[tuple[str, str], object] = {}
    for source, found in scan.items():
        hits = [m for m in found["markets"] if matches(domain, m)]
        for m in hits:
            members.setdefault((m.source, m.external_id), m)
        sources[source] = {"scanned": len(found["markets"]), "markets": len(hits), "errors": found["errors"], "rows_added": 0}
    new = _store_members(session, domain, list(members.values()), now)
    pending = list(session.scalars(select(DomainMarket).where(DomainMarket.domain_id == domain.id, DomainMarket.loaded_at.is_(None))
                                   .order_by(DomainMarket.attempts, DomainMarket.id).limit(MAX_LOAD)))
    loaded = {"rows_added": 0, "loaded": 0, "empty": {}}
    for source in sources:
        part = [p for p in pending if p.source == source]
        if part:
            r = _load_histories(session, part, fetcher, today)
            sources[source].update(rows_added=r["rows_added"], without_history=r["empty"].get(source, 0))
            loaded["rows_added"] += r["rows_added"]
            loaded["loaded"] += r["loaded"]
    summary = {"trigger": trigger, "at": now.isoformat(), "new_markets": new, "pending": len(pending), "loaded": loaded["loaded"],
               "rows_added": loaded["rows_added"], "sources": sources}
    domain.last_run_at, domain.last_run = now, summary
    session.commit()
    logger.info("domain %d %r: load run finished: %s", domain.id, domain.name, summary)
    return summary


def refresh_domains(session: Session, scanner=None, fetcher: HistoryFetcher | None = None, today: date | None = None) -> dict:
    """Daily job: one load run per domain; a failing domain never stops the others."""
    out: dict = {}
    for domain in session.scalars(select(Domain).order_by(Domain.id)).all():
        try:
            out[domain.name] = load_domain(session, domain, scanner, fetcher, today, trigger="daily")
        except Exception as exc:
            session.rollback()
            logger.exception("domain %d %r: daily load run failed", domain.id, domain.name)
            out[domain.name] = {"error": str(exc)[:300]}
    return out


# ---- overview -------------------------------------------------------------------------------

def coverage(session: Session, domain_id: int) -> dict:
    """Member counts plus history reach and gaps from the stored snapshot rows."""
    dm, s = DomainMarket, MarketSnapshot
    active, expired = session.execute(select(func.count().filter(dm.active.is_(True)), func.count().filter(dm.active.is_(False)))
                                      .where(dm.domain_id == domain_id)).one()
    per_market = session.execute(select(func.min(s.snapshot_date), func.max(s.snapshot_date), func.count(s.id))
                                 .join(dm, and_(dm.source == s.source, dm.external_id == s.external_id))
                                 .where(dm.domain_id == domain_id).group_by(s.source, s.external_id)).all()
    gaps = [(last - first).days + 1 - n for first, last, n in per_market]
    return {"markets_active": active or 0, "markets_expired": expired or 0, "markets_with_history": len(per_market),
            "first_date": min((f for f, _, _ in per_market), default=None), "last_date": max((l for _, l, _ in per_market), default=None),
            "gap_days": sum(gaps), "markets_with_gaps": sum(1 for g in gaps if g > 0)}


def payload(session: Session, domain: Domain) -> dict:
    cov = coverage(session, domain.id)
    for key in ("first_date", "last_date"):
        cov[key] = cov[key].isoformat() if cov[key] else None
    return {"id": domain.id, "name": domain.name, "topic": domain.topic, "subtopic": domain.subtopic, "terms": list(domain.terms or []),
            "created_at": domain.created_at.isoformat(), "updated_at": domain.updated_at.isoformat(),
            "last_run_at": domain.last_run_at.isoformat() if domain.last_run_at else None, "last_run": domain.last_run, **cov}


def list_domains(session: Session) -> list[dict]:
    return [payload(session, d) for d in session.scalars(select(Domain).order_by(func.lower(Domain.name)))]
