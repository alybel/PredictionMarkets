"""Domain tables: ``domains`` (definition plus last load run) and ``domain_markets`` (members found at the sources).

A member row remembers whether the market's full history was loaded (``loaded_at``), so the daily run only
loads markets that are new to the domain. Snapshot rows themselves live in ``eui_market_snapshots`` (migration 0014).
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    topic: Mapped[str] = mapped_column(String(200))
    subtopic: Mapped[str | None] = mapped_column(String(200))  # None: the whole topic
    terms: Mapped[list] = mapped_column(JSON, default=list)    # empty: every market of the topic
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run: Mapped[dict | None] = mapped_column(JSON)


class DomainMarket(Base):
    __tablename__ = "domain_markets"
    __table_args__ = (UniqueConstraint("domain_id", "source", "external_id", name="uq_domain_markets_market"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(512))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # full history stored
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # loads that found no history yet
