"""Event tables: ``events`` (one per source event) and ``event_markets`` (its outcome markets).

A market belongs to at most one event (unique source/external_id); markets without a source event
have no row here and stay standalone. Market and snapshot tables are untouched (migration 0013).
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("source", "event_key", name="uq_events_source_event_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    event_key: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventMarket(Base):
    __tablename__ = "event_markets"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_event_markets_source_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    outcome_label: Mapped[str | None] = mapped_column(String(256))
    price: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
