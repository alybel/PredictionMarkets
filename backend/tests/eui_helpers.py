"""Shared fixtures for the Economic Uncertainty Index tests (no network, no LLM)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from backend.app.clients.base import NormalizedMarket
from backend.app.db import Base, make_session_factory
from backend.app.eui import models as eui_models  # noqa: F401  (register tables)

FUTURE = datetime(2027, 6, 30, tzinfo=timezone.utc)


def market(**overrides) -> NormalizedMarket:
    defaults = dict(
        source="polymarket", external_id="pm-1", title="Will the Fed cut rates in December?", category="Economics",
        tags=["Economics"], price=0.5, volume=10_000.0, liquidity=20_000.0, end_date=FUTURE, url=None,
        volume_24h=5_000.0, best_bid=0.49, best_ask=0.51, active=True,
    )
    defaults.update(overrides)
    return NormalizedMarket(**defaults)


class FakeClient:
    def __init__(self, source: str, markets: list):
        self.source = source
        self._markets = markets
        self.calls = 0

    def fetch_markets(self) -> list:
        self.calls += 1
        return list(self._markets)


def memory_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)
