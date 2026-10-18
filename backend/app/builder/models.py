"""Metric-based index: a name plus weighted catalog metrics and the weighting method (risk index 5/7)."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow


class MetricIndex(Base):
    __tablename__ = "metric_indices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    components: Mapped[list] = mapped_column(JSON, default=list)  # [{"key": metric key, "weight": float > 0}]
    weighting: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual")  # see builder.weighting
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
