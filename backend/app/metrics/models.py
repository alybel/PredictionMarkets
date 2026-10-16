"""Persisted state of a catalog metric (status, release, block, last check results; one row per metric) and the
market-basket metrics created from Index Manufacture drafts."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow

DRAFT = "draft"          # not released yet (never checked or checked green)
VALIDATED = "validated"  # all checks green and released manually
BLOCKED = "blocked"      # a check failed (automatic) or blocked manually
STATUSES = (DRAFT, VALIDATED, BLOCKED)


class MetricState(Base):
    __tablename__ = "metric_states"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default=DRAFT)
    status_reason: Mapped[str | None] = mapped_column(String(500))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last manual release
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checks: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CustomMetric(Base):
    """Market-basket metric from an Index Manufacture draft: liquidity-weighted price of its markets per snapshot day."""

    __tablename__ = "custom_metrics"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    calculation: Mapped[str] = mapped_column(String(4000), default="")
    markets: Mapped[list] = mapped_column(JSON, default=list)  # [{source, external_id, title}]
    draft_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
