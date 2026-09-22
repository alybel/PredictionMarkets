"""Index composition (fixed per month) and the daily index value.

The composition of a month is built from the first snapshot of that month:
eligible markets classified as economic (not rejected), cross-platform
duplicates merged, weights = capped square-root of the liquidity score.
Each day the constituents are priced from that day's snapshot; constituents
that ended, became inactive or lost their price drop out and the remaining
weights are renormalized. Prices that left the corridor stay in - consensus
forming is exactly what the index should reflect. Polarity is read live
from the classification table so a human review takes effect immediately.
"""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.eui.classify import classifications_for
from backend.app.eui.dedupe import merge_duplicates
from backend.app.eui.models import STATUS_REJECTED, IndexComposition, IndexValue, MarketClassification
from backend.app.eui.scoring import contribution, index_value, renormalize, weights_from_scores
from backend.app.eui.snapshot import snapshot_rows
from backend.app.models import utcnow

logger = logging.getLogger(__name__)

DROP_REASONS = {"ended", "inactive", "no_price", "no_end_date"}


def period_of(day: date) -> str:
    return day.strftime("%Y-%m")


def get_composition(session: Session, period: str) -> IndexComposition | None:
    return session.scalar(select(IndexComposition).where(IndexComposition.period == period))


def _usable(cls: MarketClassification | None) -> bool:
    return cls is not None and cls.is_economic and cls.status != STATUS_REJECTED


def build_composition(session: Session, day: date) -> IndexComposition:
    """Constituents and weights for the period of ``day`` from that day's snapshot."""
    rows = snapshot_rows(session, day, eligible_only=True)
    classes = classifications_for(session, [r.key for r in rows])
    economic = [r for r in rows if _usable(classes.get(r.key))]
    components = [c for c in merge_duplicates(economic) if c.liquidity_score > 0]
    weights = weights_from_scores([c.liquidity_score for c in components])
    constituents = [
        {
            "component_id": c.component_id, "title": c.title, "weight": w, "sources": c.sources,
            "members": [[m.source, m.external_id] for m in c.members],
            "primary": [c.primary.source, c.primary.external_id], "liquidity_score": c.liquidity_score,
        }
        for c, w in zip(components, weights)
    ]
    composition = IndexComposition(period=period_of(day), snapshot_date=day, constituents=constituents)
    session.add(composition)
    session.commit()
    logger.info("composition %s built from %s: %d constituents (%d economic markets)", composition.period, day, len(constituents), len(economic))
    return composition


def get_or_create_composition(session: Session, day: date) -> IndexComposition:
    return get_composition(session, period_of(day)) or build_composition(session, day)


def compute_index(session: Session, day: date) -> IndexValue:
    """Value of the index for ``day`` from its snapshot and the month's composition."""
    composition = get_or_create_composition(session, day)
    rows = {r.key: r for r in snapshot_rows(session, day)}
    classes = classifications_for(session, [tuple(c["primary"]) for c in composition.constituents])
    kept_weights, contributions, details = [], [], []
    for c in composition.constituents:
        cls = classes.get(tuple(c["primary"]))
        members = [rows.get(tuple(m)) for m in c["members"]]
        members = [m for m in members if m is not None and m.price is not None and m.exclusion_reason not in DROP_REASONS]
        if not members or not _usable(cls):
            continue
        price = _score_weighted_price(members)
        u = contribution(price, cls.polarity)
        kept_weights.append(c["weight"])
        contributions.append(u)
        details.append({"component_id": c["component_id"], "title": c["title"], "price": price, "polarity": cls.polarity, "contribution": u})
    weights = renormalize(kept_weights)
    for d, w in zip(details, weights):
        d["weight"] = w
    value = index_value(weights, contributions)

    row = session.scalar(select(IndexValue).where(IndexValue.value_date == day)) or IndexValue(value_date=day)
    row.value, row.constituent_count, row.composition_id, row.details, row.computed_at = value, len(details), composition.id, details, utcnow()
    session.add(row)
    session.commit()
    logger.info("index %s = %s from %d constituents (composition %s)", day, None if value is None else round(value, 2), len(details), composition.period)
    return row


def _score_weighted_price(members) -> float:
    total = sum(m.liquidity_score for m in members)
    if total > 0:
        return sum(m.price * m.liquidity_score for m in members) / total
    return sum(m.price for m in members) / len(members)


def latest_value(session: Session) -> IndexValue | None:
    return session.scalar(select(IndexValue).order_by(IndexValue.value_date.desc()).limit(1))


def history(session: Session, days: int = 90) -> list[IndexValue]:
    rows = session.scalars(select(IndexValue).order_by(IndexValue.value_date.desc()).limit(days))
    return sorted(rows, key=lambda r: r.value_date)
