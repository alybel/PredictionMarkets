"""Tables of the Economic Uncertainty Index.

* ``eui_snapshot_runs`` / ``eui_market_snapshots``: one complete, harmonized
  market state per day (the raw material for later backtests).
* ``eui_classifications``: per market the LLM's topic decision and polarity,
  reviewed by a human; reused on every later run and never re-decided.
* ``eui_compositions``: the constituent set with weights, fixed per month.
* ``eui_index_values``: one index value per day plus its per-constituent details.
* ``eui_events``: event calendar (rate decisions, labour data, geopolitics)
  for the plausibility check that stands in for the backtest.
"""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow

POLARITY_SYMMETRIC = "symmetric"      # no good/bad side: 1 - 2 * |p - 0.5|
POLARITY_BAD_IF_YES = "bad_if_yes"    # "Yes" is the bad outcome: contribution = p
POLARITY_BAD_IF_NO = "bad_if_no"      # "No" is the bad outcome: contribution = 1 - p
POLARITIES = (POLARITY_SYMMETRIC, POLARITY_BAD_IF_YES, POLARITY_BAD_IF_NO)

STATUS_PROPOSED = "proposed"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PROPOSED, STATUS_CONFIRMED, STATUS_REJECTED)


class SnapshotRun(Base):
    __tablename__ = "eui_snapshot_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | partial | failed
    source_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(512))


class MarketSnapshot(Base):
    __tablename__ = "eui_market_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", "source", "external_id", name="uq_eui_snapshot_market"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("eui_snapshot_runs.id"), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    url: Mapped[str | None] = mapped_column(String(512))
    price: Mapped[float | None] = mapped_column(Float)
    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    spread: Mapped[float | None] = mapped_column(Float)
    depth_usd: Mapped[float] = mapped_column(Float, default=0.0)
    volume_24h_usd: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_score: Mapped[float] = mapped_column(Float, default=0.0)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool | None] = mapped_column(Boolean)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.external_id)


class MarketClassification(Base):
    __tablename__ = "eui_classifications"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_eui_classification_market"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    is_economic: Mapped[bool] = mapped_column(Boolean, index=True)
    polarity: Mapped[str] = mapped_column(String(16), default=POLARITY_SYMMETRIC)
    rationale: Mapped[str | None] = mapped_column(String(512))
    model: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=STATUS_PROPOSED, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexComposition(Base):
    """Constituents of one monthly period: [{component_id, title, members, primary, weight, sources}]."""

    __tablename__ = "eui_compositions"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), unique=True, index=True)  # YYYY-MM
    snapshot_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    constituents: Mapped[list] = mapped_column(JSON, default=list)


class IndexValue(Base):
    __tablename__ = "eui_index_values"

    id: Mapped[int] = mapped_column(primary_key=True)
    value_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    value: Mapped[float | None] = mapped_column(Float)
    constituent_count: Mapped[int] = mapped_column(Integer, default=0)
    composition_id: Mapped[int | None] = mapped_column(ForeignKey("eui_compositions.id"))
    details: Mapped[list] = mapped_column(JSON, default=list)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexEvent(Base):
    __tablename__ = "eui_events"
    __table_args__ = (UniqueConstraint("event_date", "label", name="uq_eui_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    label: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # rates | labour | geopolitical | other
