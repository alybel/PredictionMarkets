"""Latest forecast report per metric index (risk index 6/7); rebuilt on every save and by the daily run."""

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow


class ForecastReport(Base):
    __tablename__ = "forecast_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    index_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    as_of: Mapped[date] = mapped_column(Date)                   # cutoff: nothing after this day enters
    trigger: Mapped[str] = mapped_column(String(16))            # save | daily | manual | on_demand
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
