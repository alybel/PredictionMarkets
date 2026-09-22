"""Data model can be filled with dummy data and read back."""

import pytest
from sqlalchemy import create_engine

from backend.app.db import Base, make_session_factory
from backend.app.models import IndexRule, Market


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        yield s


def test_market_roundtrip(session):
    session.add(
        Market(
            source="polymarket",
            external_id="pm-123",
            title="Will X win the election?",
            category="politics",
            price=0.62,
            volume=150_000.0,
            liquidity=42_000.0,
        )
    )
    session.commit()

    market = session.query(Market).one()
    assert market.source == "polymarket"
    assert market.external_id == "pm-123"
    assert market.price == 0.62
    assert market.liquidity == 42_000.0
    assert market.fetched_at is not None


def test_index_rule_roundtrip(session):
    session.add(
        IndexRule(
            index_name="political",
            categories=["politics", "elections"],
            keywords=["election", "president"],
            min_liquidity=10_000.0,
        )
    )
    session.commit()

    rule = session.query(IndexRule).one()
    assert rule.index_name == "political"
    assert rule.categories == ["politics", "elections"]
    assert rule.keywords == ["election", "president"]
    assert rule.min_liquidity == 10_000.0
    assert rule.created_at is not None
