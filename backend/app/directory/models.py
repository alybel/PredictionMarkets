"""Directory tables: ``market_directory`` (one row per market, unique per source + external id) and ``market_directory_runs``.

A row is written once when a market is first seen and changed only when the source reports different data
(title, tags, dates, status); ``first_seen_at`` never changes. ``is_new`` marks markets found after the source's
first (baseline) run; they stay in the inbox until ``reviewed_at`` is set. ``group_key``/``group_title`` name the market
group (source event) a market belongs to; markets of one group are shown and picked as one group market.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow

ACTIVE, EXPIRED, RESOLVED = "active", "expired", "resolved"
STATUSES = (ACTIVE, EXPIRED, RESOLVED)


class DirectoryMarket(Base):
    __tablename__ = "market_directory"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_market_directory_market"),
                      Index("ix_market_directory_group", "source", "group_key"))

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # start date (listing) at the source
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_seen_run: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last real change
    is_new: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    group_key: Mapped[str | None] = mapped_column(String(128))    # None: market stands alone (group of one)
    group_title: Mapped[str | None] = mapped_column(String(512))


class DirectoryRun(Base):
    __tablename__ = "market_directory_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict | None] = mapped_column(JSON)
