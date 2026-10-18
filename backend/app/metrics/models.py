"""Persisted state of a catalog metric (status, release, block, last check results; one row per metric) and the
custom metrics: market baskets (Index Manufacture, tab "Metriken") and structured chain / single-market metrics (agent step 2)."""

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
    """Custom metric. ``basket``: liquidity-weighted price of ``markets`` per snapshot day (unchanged since 1015-2351).
    ``chain`` / ``single``: structured ``spec`` {links: [{source, external_id, title, roll_on, direction}], rules, notes,
    domain_id, assessment}, computed by ``metrics.chain``."""

    __tablename__ = "custom_metrics"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    calculation: Mapped[str] = mapped_column(String(4000), default="")
    markets: Mapped[list] = mapped_column(JSON, default=list)  # [{source, external_id, title}]
    draft_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="basket", server_default="basket")
    spec: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricBookmark(Base):
    """Catalog metric bookmarked in the agent tab ("Metriken finden") for the index builder."""

    __tablename__ = "metric_bookmarks"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
