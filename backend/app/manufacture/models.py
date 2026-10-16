"""Index drafts of the Index Manufacture: the description worked out with the assistant plus its market picks."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import MIN_LIQUIDITY_USD, utcnow

DRAFT_STATUS_DRAFT = "draft"
DRAFT_STATUS_PUBLISHED = "published"  # legacy: became a rule-based index (now archive)
DRAFT_STATUS_METRIC = "metric"        # became a metric draft of the catalog (published_index holds its key)


class IndexDraft(Base):
    """One draft: name, intent, description, methodology, rule parameters, chosen markets and the chat transcript."""

    __tablename__ = "manufacture_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    intent: Mapped[str] = mapped_column(String(2000), default="")
    description: Mapped[str] = mapped_column(String(4000), default="")
    methodology: Mapped[str] = mapped_column(String(4000), default="")
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    min_liquidity: Mapped[float] = mapped_column(Float, default=MIN_LIQUIDITY_USD)
    markets: Mapped[list] = mapped_column(JSON, default=list)      # [{id, source, external_id, title, reason, url}]
    messages: Mapped[list] = mapped_column(JSON, default=list)     # [{role, content}]
    status: Mapped[str] = mapped_column(String(16), default=DRAFT_STATUS_DRAFT, index=True)
    published_index: Mapped[str | None] = mapped_column(String(128))
    model: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
