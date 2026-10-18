"""Risk topics: pre-filter the market directory, classify each market once per topic, review list, hand-over, timeline.

Pre-filter: a directory market is a candidate when one of its tags equals a topic tag or its title contains a search
term. Each candidate is sent to the classifier once per topic; the stored assessment is reused on every later run.
Only a keyword-fallback row that nobody reviewed yet is replaced once a language model is available. New markets of
the inbox are classified automatically by the daily job (``classify_new_markets``).

Timeline: a market counts from its start date (listing; without one from the day it was first seen) to its end date,
never earlier, so no market looks back in time. The window covers at least three years; a shorter real history is
shown as it is and flagged.
"""

import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from backend.app.directory.models import DirectoryMarket
from backend.app.models import utcnow
from backend.app.quality.pit import as_utc
from backend.app.risk.classify import FALLBACK, Classifier, Item
from backend.app.risk.models import ACCEPTED, PROPOSED, READINGS, REJECTED, STATUSES, RiskAssessment, RiskTopic

logger = logging.getLogger(__name__)

RUN_LIMIT = 400  # markets sent per run and topic; the rest follows on the next run
MIN_YEARS = 3
PILOT = {
    "slug": "marktcrash", "name": "Marktcrash-Risiko",
    "question": "Wie wahrscheinlich ist ein starker Einbruch der Aktienmärkte (Crash, Bärenmarkt, tiefe Korrektur)?",
    "search_terms": ["crash", "stock market", "s&p", "sp500", "nasdaq", "dow jones", "bear market", "correction", "vix"],
    "tags": ["stocks", "equities", "indices"],
}


def _iso(value) -> str | None:
    value = as_utc(value) if isinstance(value, datetime) else value
    return value.isoformat() if value else None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64] or "thema"


# ---- topics --------------------------------------------------------------------------------

def topic_payload(t: RiskTopic) -> dict:
    return {"id": t.id, "slug": t.slug, "name": t.name, "question": t.question, "search_terms": list(t.search_terms or []),
            "tags": list(t.tags or [])}


def ensure_pilot(session: Session) -> RiskTopic:
    topic = session.scalar(select(RiskTopic).where(RiskTopic.slug == PILOT["slug"]))
    if topic is None:
        topic = RiskTopic(**PILOT, created_at=utcnow())
        session.add(topic)
        session.commit()
        logger.info("risk topic %r created (pilot)", topic.name)
    return topic


def list_topics(session: Session) -> list[dict]:
    ensure_pilot(session)
    return [topic_payload(t) for t in session.scalars(select(RiskTopic).order_by(RiskTopic.id))]


def create_topic(session: Session, name: str, question: str, search_terms: list[str], tags: list[str]) -> RiskTopic:
    slug = slugify(name)
    if session.scalar(select(RiskTopic.id).where(RiskTopic.slug == slug)):
        raise ValueError(f"Thema {name!r} existiert bereits")
    clean = lambda xs: sorted({x.strip().lower() for x in xs if x.strip()})  # noqa: E731
    topic = RiskTopic(slug=slug, name=name.strip(), question=question.strip(), search_terms=clean(search_terms), tags=clean(tags),
                      created_at=utcnow())
    session.add(topic)
    session.commit()
    logger.info("risk topic %r created: %d search terms, %d tags", topic.name, len(topic.search_terms), len(topic.tags))
    return topic


def get_topic(session: Session, topic_id: int) -> RiskTopic:
    topic = session.get(RiskTopic, topic_id)
    if topic is None:
        raise LookupError(f"Thema {topic_id} nicht gefunden")
    return topic


# ---- pre-filter and classification ---------------------------------------------------------

def prefilter(session: Session, topic: RiskTopic, only_new: bool = False):
    """Directory markets matching a topic tag or search term (query, not yet executed)."""
    d = DirectoryMarket
    tags_text = func.lower(cast(d.tags, String))
    conditions = [func.lower(d.title).like(f"%{t.lower()}%") for t in topic.search_terms or []]
    conditions += [tags_text.like(f'%"{t.lower()}"%') for t in topic.tags or []]
    if not conditions:
        return None
    query = select(d).where(or_(*conditions))
    return query.where(d.is_new.is_(True)) if only_new else query


def classify_topic(session: Session, topic: RiskTopic, classifier: Classifier, limit: int = RUN_LIMIT, only_new: bool = False) -> dict:
    """Send pre-filtered markets without a stored assessment (once per topic) and store the valid answers."""
    query = prefilter(session, topic, only_new)
    candidates = list(session.scalars(query.order_by(DirectoryMarket.id))) if query is not None else []
    known = {a.market_id: a for a in session.scalars(select(RiskAssessment).where(RiskAssessment.topic_id == topic.id))}
    upgradable = classifier.name != FALLBACK
    pending = [m for m in candidates if m.id not in known
               or (upgradable and known[m.id].model == FALLBACK and known[m.id].status == PROPOSED)]
    batch = pending[:limit]
    summary = {"topic_id": topic.id, "topic": topic.name, "model": classifier.name, "fallback": classifier.name == FALLBACK,
               "candidates": len(candidates), "already": len(candidates) - len(pending), "sent": len(batch), "stored": 0,
               "upgraded": 0, "discarded": 0, "remaining": len(pending) - len(batch)}
    if batch:
        result = classifier.classify({"name": topic.name, "question": topic.question},
                                     [Item(m.id, m.title, tuple(m.tags or ())) for m in batch])
        now = utcnow()
        for dec in result.decisions:
            row = known.get(dec.market_id)
            if row is None:
                session.add(RiskAssessment(topic_id=topic.id, market_id=dec.market_id, relevant=dec.relevant, rationale=dec.rationale,
                                           proposed_reading=dec.reading, reading=dec.reading, status=PROPOSED, model=dec.model, created_at=now))
                summary["stored"] += 1
            else:  # fallback row replaced by the model (still unreviewed)
                row.relevant, row.rationale, row.proposed_reading, row.reading, row.model, row.created_at = (
                    dec.relevant, dec.rationale, dec.reading, dec.reading, dec.model, now)
                summary["upgraded"] += 1
        session.commit()
        summary["discarded"] = result.discarded
    logger.info("risk classification %r with %s%s: %d candidates, %d already classified, %d sent, %d stored, %d upgraded, "
                "%d answers discarded, %d left for later", topic.name, classifier.name, " (FALLBACK, no API key)" if summary["fallback"] else "",
                summary["candidates"], summary["already"], summary["sent"], summary["stored"], summary["upgraded"], summary["discarded"],
                summary["remaining"])
    return summary


def classify_new_markets(session: Session, classifier: Classifier) -> list[dict]:
    """Daily job: classify the new markets of the inbox for every topic."""
    ensure_pilot(session)
    return [classify_topic(session, t, classifier, only_new=True) for t in session.scalars(select(RiskTopic).order_by(RiskTopic.id))]


# ---- review list and hand-over -------------------------------------------------------------

def assessment_payload(a: RiskAssessment, m: DirectoryMarket) -> dict:
    return {"id": a.id, "market_id": m.id, "source": m.source, "external_id": m.external_id, "title": m.title, "tags": list(m.tags or []),
            "listed_at": _iso(m.listed_at), "end_date": _iso(m.end_date), "market_status": m.status, "relevant": a.relevant,
            "rationale": a.rationale, "proposed_reading": a.proposed_reading, "reading": a.reading, "status": a.status, "model": a.model,
            "fallback": a.model == FALLBACK, "decided_at": _iso(a.decided_at)}


def list_assessments(session: Session, topic: RiskTopic, status: str = "", relevant: bool | None = None, limit: int = 100, offset: int = 0) -> dict:
    a, m = RiskAssessment, DirectoryMarket
    query = select(a, m).join(m, m.id == a.market_id).where(a.topic_id == topic.id)
    if status:
        query = query.where(a.status == status)
    if relevant is not None:
        query = query.where(a.relevant.is_(relevant))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    counts = dict(session.execute(select(a.status, func.count()).where(a.topic_id == topic.id).group_by(a.status)).all())
    rows = session.execute(query.order_by(a.relevant.desc(), m.listed_at.desc().nulls_last(), a.id).limit(limit).offset(offset))
    return {"topic": topic_payload(topic), "total": total, "counts": {s: counts.get(s, 0) for s in STATUSES}, "limit": limit, "offset": offset,
            "readings": READINGS, "assessments": [assessment_payload(x, y) for x, y in rows]}


def review(session: Session, assessment_id: int, status: str | None = None, reading: str | None = None) -> dict:
    """Accept/reject a market and/or change its reading; every change is logged."""
    row = session.get(RiskAssessment, assessment_id)
    if row is None:
        raise LookupError(f"Einstufung {assessment_id} nicht gefunden")
    if status is not None and status not in STATUSES:
        raise ValueError(f"ungültiger Status {status!r}")
    if reading is not None and reading not in READINGS:
        raise ValueError(f"ungültige Lesart {reading!r}")
    old = (row.status, row.reading)
    if status is not None:
        row.status = status
    if reading is not None:
        row.reading = reading
    row.decided_at = utcnow()
    session.commit()
    logger.info("risk review: assessment %d (topic %d, market %d) status %s -> %s, reading %s -> %s", row.id, row.topic_id, row.market_id,
                old[0], row.status, old[1], row.reading)
    return assessment_payload(row, session.get(DirectoryMarket, row.market_id))


def accepted_markets(session: Session, topic: RiskTopic) -> list[dict]:
    """Hand-over to the next steps: only accepted markets with their (possibly changed) reading."""
    a, m = RiskAssessment, DirectoryMarket
    rows = session.execute(select(a, m).join(m, m.id == a.market_id).where(a.topic_id == topic.id, a.status == ACCEPTED).order_by(m.id))
    return [{"market_id": y.id, "source": y.source, "external_id": y.external_id, "title": y.title, "reading": x.reading,
             "start": _iso(y.listed_at or y.first_seen_at), "end": _iso(y.end_date)} for x, y in rows]


# ---- timeline ------------------------------------------------------------------------------

MAX_TIMELINE_MARKETS = 500


def _next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def timeline(session: Session, topic: RiskTopic, today: date | None = None) -> dict:
    """Months over at least three years with the number of relevant, not rejected markets existing in each month."""
    today = today or utcnow().date()
    a, m = RiskAssessment, DirectoryMarket
    rows = session.execute(select(a, m).join(m, m.id == a.market_id)
                           .where(a.topic_id == topic.id, a.relevant.is_(True), a.status != REJECTED))
    markets = []
    for x, y in rows:
        start = as_utc(y.listed_at or y.first_seen_at).date()  # counts only from its start date
        end = min(as_utc(y.end_date).date(), today) if y.end_date else today
        markets.append({"market_id": y.id, "title": y.title, "source": y.source, "status": x.status, "reading": x.reading,
                        "start": start.isoformat(), "end": max(end, start).isoformat(), "dated": y.listed_at is not None})
    markets.sort(key=lambda r: (r["start"], r["market_id"]))
    history_start = date.fromisoformat(markets[0]["start"]) if markets else None
    window_start = date(today.year - MIN_YEARS, today.month, min(today.day, 28))
    if history_start and history_start < window_start:
        window_start = history_start
    months, ms = [], date(window_start.year, window_start.month, 1)
    while ms <= today:
        nxt = _next_month(ms)
        me = (nxt - timedelta(days=1)).isoformat()
        months.append({"month": ms.strftime("%Y-%m"), "count": sum(1 for r in markets if r["start"] <= me and r["end"] >= ms.isoformat())})
        ms = nxt
    years = round((today - history_start).days / 365.25, 1) if history_start else 0.0
    short = years < MIN_YEARS
    note = None
    if not markets:
        note = "Noch keine relevanten Märkte – die Zeitleiste ist leer."
    elif short:
        note = f"Historie nur {years:.1f} Jahre (seit {history_start.strftime('%d.%m.%Y')}) – kürzer als {MIN_YEARS} Jahre."
    return {"topic": topic_payload(topic), "window_start": window_start.isoformat(), "window_end": today.isoformat(),
            "history_start": history_start.isoformat() if history_start else None, "history_years": years, "short_history": short,
            "note": note, "months": months, "total_markets": len(markets), "markets": markets[-MAX_TIMELINE_MARKETS:]}
