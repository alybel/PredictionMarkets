"""Risk topic tables: ``risk_topics`` (question + search terms) and ``risk_assessments`` (one row per topic and directory market).

An assessment is written once per topic and market and reused; only a human changes it afterwards (status, reading).
A row decided by the keyword fallback (``model`` = ``keyword-fallback``) and not yet reviewed may be replaced once by
the language model when a key becomes available.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow

# Risk readings of a market price p (probability of "Yes"):
HIGH, LOW, UNCERTAINTY, VOLATILITY = "high", "low", "uncertainty", "volatility"
READINGS = {
    HIGH: "Risiko = p (Ja ist das Risiko)",
    LOW: "Risiko = 1 − p (Nein ist das Risiko)",
    UNCERTAINTY: "Unsicherheit = 1 − 2·|p − 0,5|",
    VOLATILITY: "Volatilität der Preisbewegung",
}
PROPOSED, ACCEPTED, REJECTED = "proposed", "accepted", "rejected"
STATUSES = (PROPOSED, ACCEPTED, REJECTED)


class RiskTopic(Base):
    __tablename__ = "risk_topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    question: Mapped[str] = mapped_column(String(512))
    search_terms: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"
    __table_args__ = (UniqueConstraint("topic_id", "market_id", name="uq_risk_assessment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("risk_topics.id", ondelete="CASCADE"), index=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("market_directory.id", ondelete="CASCADE"), index=True)
    relevant: Mapped[bool] = mapped_column(Boolean)
    rationale: Mapped[str] = mapped_column(String(512), default="")
    proposed_reading: Mapped[str] = mapped_column(String(16))
    reading: Mapped[str] = mapped_column(String(16))  # current reading (a human may change it)
    status: Mapped[str] = mapped_column(String(16), default=PROPOSED, index=True)
    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
