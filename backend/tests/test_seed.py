"""Seeding of the predefined dashboard indices, no network."""

from sqlalchemy import create_engine

from backend.app.db import Base, make_session_factory
from backend.app.engine import get_rule, list_rules, upsert_rule
from backend.app.models import MIN_LIQUIDITY_USD
from backend.app.seed import PREDEFINED_INDICES, seed_indices


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def test_seed_creates_all_predefined_indices():
    with make_session() as session:
        created = seed_indices(session)

        assert created == [spec["index_name"] for spec in PREDEFINED_INDICES]
        rules = {r.index_name: r for r in list_rules(session)}
        assert rules["global-politics"].min_liquidity == MIN_LIQUIDITY_USD
        assert "Politics" in rules["global-politics"].categories
        assert {"election", "trump", "ceasefire", "iran", "ukraine"} <= set(rules["global-politics"].keywords)
        assert "Economics" in rules["global-economics"].categories
        assert {"fed", "inflation", "tariff", "recession", "gdp"} <= set(rules["global-economics"].keywords)


def test_seed_is_idempotent_and_keeps_adjusted_rules():
    with make_session() as session:
        seed_indices(session)
        upsert_rule(session, "global-politics", categories=["custom"], min_liquidity=25_000.0)

        assert seed_indices(session) == []
        rules = {r.index_name: r for r in list_rules(session)}
        assert rules["global-politics"].categories == ["custom"]
        assert rules["global-politics"].min_liquidity == 25_000.0


def test_seed_does_not_recreate_deleted_indices():
    with make_session() as session:
        seed_indices(session)
        session.delete(get_rule(session, "global-economics"))
        session.commit()

        assert seed_indices(session) == []
        assert get_rule(session, "global-economics") is None
