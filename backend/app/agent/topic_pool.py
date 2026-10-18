"""Pool of the "Metriken anlegen" mode: only the markets accepted in a risk topic's review list, with their reading.

The review list (``risk_assessments``) is the single gate: a market enters the pool only with status ``accepted`` for
the chosen topic; its reading (changed by a human or proposed by the classifier) becomes the direction of its chain link.
History comes from the stored daily snapshots. Refs outside the pool are explained (rejected, not yet reviewed,
not in the review list, unknown) so every discarded reference can be logged with its reason.
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.agent.chaining import Chain, build_chains
from backend.app.agent.pool import Candidate
from backend.app.directory.models import DirectoryMarket
from backend.app.eui.models import MarketSnapshot
from backend.app.metrics import chain_rules
from backend.app.risk import models as risk

logger = logging.getLogger(__name__)

# review-list reading -> chain link reading
READING_MAP = {risk.HIGH: chain_rules.UP, risk.LOW: chain_rules.DOWN, risk.UNCERTAINTY: chain_rules.UNCERTAINTY,
               risk.VOLATILITY: chain_rules.VOLATILITY}
REASONS = {risk.REJECTED: "abgelehnt", risk.PROPOSED: "noch nicht angenommen"}


def link_reading(reading: str | None) -> str:
    return READING_MAP.get(reading or "", chain_rules.UP)


@dataclass
class TopicPool:
    topic: dict
    candidates: list[Candidate]
    chains: list[Chain]
    readings: dict[str, str] = field(default_factory=dict)  # market key -> chain reading from the review list

    @property
    def by_key(self) -> dict[str, Candidate]:
        return {c.key: c for c in self.candidates}


def topic_pool(session: Session, topic: risk.RiskTopic, keys: set[tuple[str, str]] | None = None) -> TopicPool:
    """Accepted markets of ``topic`` (optionally only ``keys``) with stored history and the chains among them."""
    a, d, s = risk.RiskAssessment, DirectoryMarket, MarketSnapshot
    history = (select(s.source, s.external_id, func.min(s.snapshot_date).label("first"), func.max(s.snapshot_date).label("last"),
                      func.count(s.id).label("n"), func.avg(s.volume_24h_usd).label("vol"), func.max(s.end_date).label("end"))
               .where(s.price.is_not(None)).group_by(s.source, s.external_id).subquery())
    stmt = (select(d.source, d.external_id, d.title, d.end_date, a.reading, history.c.first, history.c.last, history.c.n, history.c.vol,
                   history.c.end).join(a, a.market_id == d.id)
            .outerjoin(history, (history.c.source == d.source) & (history.c.external_id == d.external_id))
            .where(a.topic_id == topic.id, a.status == risk.ACCEPTED).order_by(d.id))
    candidates, readings = [], {}
    for source, eid, title, end, reading, first, last, n, vol, stored_end in session.execute(stmt):
        if keys is not None and (source, eid) not in keys:
            continue
        end = end or stored_end  # directory end date, else the one of the stored snapshots
        c = Candidate(source, eid, title, first, last, n or 0, float(vol or 0.0), end.date() if end else None)
        candidates.append(c)
        readings[c.key] = link_reading(reading)
    pool = TopicPool({"id": topic.id, "name": topic.name, "question": topic.question}, candidates,
                     build_chains(candidates) if keys is None else [], readings)
    logger.info("topic pool %r: %d accepted markets, %d with history, %d chains", topic.name, len(candidates),
                sum(1 for c in candidates if c.days), len(pool.chains))
    return pool


def ref_reason(session: Session, topic_id: int, ref: str) -> str:
    """Why a ref is not in the pool: rejected / not yet accepted / not in the review list / unknown."""
    if ref.startswith("chain:") or ":" not in ref:
        return "unbekannte Referenz"
    source, eid = ref.split(":", 1)
    market = session.scalar(select(DirectoryMarket).where(DirectoryMarket.source == source, DirectoryMarket.external_id == eid))
    if market is None:
        return "unbekannter Markt"
    status = session.scalar(select(risk.RiskAssessment.status).where(risk.RiskAssessment.topic_id == topic_id,
                                                                    risk.RiskAssessment.market_id == market.id))
    return REASONS.get(status, "nicht in der Prüfliste des Themas")
