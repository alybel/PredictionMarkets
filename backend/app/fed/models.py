"""Raw layer of the Fed decision markets, kept apart from ``eui_market_snapshots``.

Append-only: a market row is written once, a price row once per (market, day); nothing is ever updated,
so a repeated load only adds what is missing.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.models import utcnow

LIVE = "live"              # observed on the day of the load
HISTORICAL = "historical"  # taken from the source's price history of an earlier day


class FedMarket(Base):
    """One outcome market of one FOMC meeting (e.g. "Cut 25 bps at the Dec 2025 meeting")."""

    __tablename__ = "fed_markets"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_fed_markets_source_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128))
    session_date: Mapped[date] = mapped_column(Date, index=True)
    outcome_bp: Mapped[int] = mapped_column(Integer)
    outcome_label: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FedPrice(Base):
    """Price of one outcome market on one day, as observed at ``observed_at`` (before the US close of that day)."""

    __tablename__ = "fed_prices"
    __table_args__ = (UniqueConstraint("market_id", "price_date", name="uq_fed_prices_market_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("fed_markets.id"), index=True)
    price_date: Mapped[date] = mapped_column(Date, index=True)
    price: Mapped[float] = mapped_column(Float)
    volume_usd: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    origin: Mapped[str] = mapped_column(String(16))
