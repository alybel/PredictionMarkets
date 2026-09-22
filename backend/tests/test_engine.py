"""Rule engine: configurable per-index rules select the matching markets."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from backend.app.clients.base import NormalizedMarket
from backend.app.db import Base, make_session_factory
from backend.app.engine import (
    get_rule,
    list_rules,
    market_matches,
    markets_for_index,
    select_markets,
    upsert_rule,
)
from backend.app.models import IndexRule, Market


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        yield s


def make_market(**overrides) -> NormalizedMarket:
    defaults = dict(
        source="polymarket",
        external_id="pm-1",
        title="Will the president win the election?",
        category="politics",
        price=0.5,
        volume=100_000.0,
        liquidity=50_000.0,
    )
    defaults.update(overrides)
    return NormalizedMarket(**defaults)


def rule(**overrides) -> IndexRule:
    defaults = dict(index_name="political", categories=[], keywords=[], min_liquidity=0.0)
    defaults.update(overrides)
    return IndexRule(**defaults)


# --- matching -----------------------------------------------------------


def test_empty_rule_matches_everything():
    assert market_matches(rule(), make_market())


def test_category_filter_is_case_insensitive():
    r = rule(categories=["Politics"])
    assert market_matches(r, make_market(category="politics"))
    assert not market_matches(r, make_market(category="economics"))
    assert not market_matches(r, make_market(category=None))


def test_keyword_filter_matches_title_case_insensitive():
    r = rule(keywords=["Election", "fed"])
    assert market_matches(r, make_market(title="US ELECTION 2028"))
    assert market_matches(r, make_market(title="Will the Fed cut rates?"))
    assert not market_matches(r, make_market(title="Bitcoin above 100k?"))


def test_liquidity_threshold():
    r = rule(min_liquidity=10_000.0)
    assert market_matches(r, make_market(liquidity=10_000.0))
    assert not market_matches(r, make_market(liquidity=9_999.0))
    assert not market_matches(r, make_market(liquidity=None))


def test_all_filters_combined():
    r = rule(categories=["politics"], keywords=["election"], min_liquidity=1_000.0)
    assert market_matches(r, make_market())
    assert not market_matches(r, make_market(category="sports"))
    assert not market_matches(r, make_market(title="Fed decision"))
    assert not market_matches(r, make_market(liquidity=10.0))


def test_price_corridor_excludes_near_decided_markets():
    r = rule(min_price=0.03, max_price=0.97)
    assert market_matches(r, make_market(price=0.03))
    assert market_matches(r, make_market(price=0.97))
    assert not market_matches(r, make_market(price=0.02))
    assert not market_matches(r, make_market(price=0.98))


def test_price_corridor_unknown_price_passes():
    assert market_matches(rule(min_price=0.03, max_price=0.97), make_market(price=None))


def test_missing_corridor_bounds_mean_no_restriction():
    # Transient rules without corridor values (as before this feature) stay permissive.
    assert market_matches(rule(), make_market(price=0.99))
    assert market_matches(rule(), make_market(price=0.01))


def test_past_end_date_excludes_market():
    now = datetime.now(timezone.utc)
    assert not market_matches(rule(), make_market(end_date=now - timedelta(days=1)))
    assert market_matches(rule(), make_market(end_date=now + timedelta(days=1)))
    assert market_matches(rule(), make_market(end_date=None))


def test_naive_end_date_counts_as_utc():
    naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    naive_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    assert not market_matches(rule(), make_market(end_date=naive_past))
    assert market_matches(rule(), make_market(end_date=naive_future))


def test_select_markets_returns_only_matching():
    r = rule(categories=["politics"], min_liquidity=1_000.0)
    matching = make_market(external_id="a")
    wrong_category = make_market(external_id="b", category="crypto")
    illiquid = make_market(external_id="c", liquidity=5.0)
    assert select_markets(r, [matching, wrong_category, illiquid]) == [matching]


def test_select_markets_works_on_orm_rows(session):
    session.add(
        Market(
            source="kalshi",
            external_id="k-1",
            title="Recession in 2027?",
            category="economics",
            price=0.3,
            volume=1.0,
            liquidity=20_000.0,
        )
    )
    session.commit()
    r = rule(categories=["economics"], min_liquidity=10_000.0)
    assert len(select_markets(r, session.query(Market).all())) == 1


# --- rule storage per index ---------------------------------------------


def test_upsert_creates_and_updates_rule(session):
    upsert_rule(session, "political", categories=["politics"], min_liquidity=1_000.0)
    upsert_rule(session, "political", categories=["politics", "elections"], keywords=["vote"])

    stored = get_rule(session, "political")
    assert stored.categories == ["politics", "elections"]
    assert stored.keywords == ["vote"]
    assert stored.min_liquidity == 10_000.0  # never below the global floor
    assert len(list_rules(session)) == 1


def test_stored_rules_default_to_price_corridor(session):
    upsert_rule(session, "political")
    stored = get_rule(session, "political")
    assert stored.min_price == 0.03
    assert stored.max_price == 0.97


def test_upsert_accepts_custom_price_corridor(session):
    upsert_rule(session, "political", min_price=0.1, max_price=0.9)
    stored = get_rule(session, "political")
    assert stored.min_price == 0.1
    assert stored.max_price == 0.9
    assert not market_matches(stored, make_market(price=0.95))
    assert market_matches(stored, make_market(price=0.5))


def test_rules_are_stored_per_index(session):
    upsert_rule(session, "political", categories=["politics"])
    upsert_rule(session, "economic", categories=["economics"], min_liquidity=15_000.0)

    assert [r.index_name for r in list_rules(session)] == ["economic", "political"]
    assert get_rule(session, "economic").min_liquidity == 15_000.0


def test_markets_for_index_uses_stored_rule(session):
    upsert_rule(session, "political", categories=["politics"], min_liquidity=10_000.0)
    markets = [make_market(), make_market(external_id="x", category="sports")]

    result = markets_for_index(session, "political", markets)
    assert [m.external_id for m in result] == ["pm-1"]


def test_markets_for_index_unknown_index_raises(session):
    with pytest.raises(LookupError):
        markets_for_index(session, "missing", [])
