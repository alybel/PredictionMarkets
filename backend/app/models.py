"""Data model: normalized markets and rule-based index definitions."""

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Markets priced outside this corridor are near-decided and carry no
# information for an index; rules can override the bounds per index.
DEFAULT_MIN_PRICE = 0.03
DEFAULT_MAX_PRICE = 0.97

# Global population floor: markets with less liquidity are never loaded
# and no index may lower its own threshold below it. Both sources quote USD.
MIN_LIQUIDITY_USD = 10_000.0


class Market(Base):
    """One market snapshot, normalized across sources (Polymarket, Kalshi)."""

    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_markets_source_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(128), index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    price: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    liquidity: Mapped[float | None] = mapped_column(Float)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    url: Mapped[str | None] = mapped_column(String(512))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexRule(Base):
    """Rule set defining which markets belong to a named index."""

    __tablename__ = "index_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    index_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    min_liquidity: Mapped[float] = mapped_column(Float, default=MIN_LIQUIDITY_USD)
    min_price: Mapped[float] = mapped_column(Float, default=DEFAULT_MIN_PRICE)
    max_price: Mapped[float] = mapped_column(Float, default=DEFAULT_MAX_PRICE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
