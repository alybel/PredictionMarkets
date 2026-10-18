"""Chain metrics of agent step 2 (1016-2013): the chain calculation really runs the catalog rules (no roll jump, front chain,
direction 1 - p, gaps stay empty, no forward fill), structured storage as draft with checks, and the market-basket
metrics keep their values. No network, no LLM."""

import logging
from datetime import date, timedelta

import pytest

from backend.app.metrics import chain_rules
from backend.app.metrics.basket import basket_points
from backend.app.metrics.catalog import ChainLink, basket_def
from backend.app.metrics.chain import history_summary, link_series
from backend.app.metrics.models import CustomMetric, MetricState
from backend.app.metrics.service import create_basket, create_structured, evaluate_baskets
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_index_builder import days, snapshots

D = date(2026, 1, 1)
RULES = (chain_rules.DIRECTION, chain_rules.ROLL_END_DATE, chain_rules.NO_GAP_FILL, chain_rules.NO_FORWARD_FILL)


def day(n: int) -> date:
    return D + timedelta(days=n)


def prices(**series) -> dict:
    """{"a": {0: 0.2, ...}} -> link key -> day -> (price, volume)."""
    return {("kalshi", k): {day(n): (p, 100.0) for n, p in s.items()} for k, s in series.items()}


def link(key: str, roll: int | None, direction: str = "up") -> ChainLink:
    return ChainLink("kalshi", key, key.upper(), day(roll) if roll is not None else None, direction)


def test_two_links_roll_without_jump():
    data = prices(a={0: 0.2, 1: 0.3, 2: 0.4}, b={0: 0.6, 1: 0.6, 2: 0.6, 3: 0.7, 4: 0.8})
    pts = link_series([link("b", 10), link("a", 2)], RULES, data)
    assert [p.day for p in pts] == [day(n) for n in range(5)]
    assert [round(p.level, 6) for p in pts] == [0.2, 0.3, 0.4, 0.7, 0.8]          # level of the active link
    assert [round(p.value, 6) for p in pts] == [0.2, 0.3, 0.4, 0.5, 0.6]          # linked: roll day moves only by B's own change
    assert [p.roll for p in pts] == [False, False, False, True, False]
    roll = pts[3]
    assert roll.session_date == day(10) and round(roll.change, 6) == 0.1 and round(roll.value - pts[2].value, 6) == 0.1


def test_parallel_links_use_the_nearest_due_link():
    data = prices(near={n: 0.1 + 0.01 * n for n in range(6)}, far={n: 0.9 for n in range(10)})
    pts = link_series([link("far", 30), link("near", 5)], RULES + (chain_rules.FRONT_CHAIN,), data)
    assert [p.session_date for p in pts] == [day(5)] * 6 + [day(30)] * 4          # front chain: near until its end, then far
    assert [round(p.level, 6) for p in pts[:6]] == [round(0.1 + 0.01 * n, 6) for n in range(6)]
    data[("kalshi", "near")].pop(day(2))                                          # front chain takes the next traded link on a missing day
    front = link_series([link("far", 30), link("near", 5)], RULES + (chain_rules.FRONT_CHAIN,), data)
    plain = link_series([link("far", 30), link("near", 5)], RULES, data)
    assert {p.day: p.session_date for p in front}[day(2)] == day(30)
    assert day(2) not in {p.day for p in plain}                                     # without the rule: gap, never filled


def test_direction_down_enters_as_one_minus_p():
    pts = link_series([link("a", None, "down")], RULES, prices(a={0: 0.3, 1: 0.4}))
    assert [round(p.level, 6) for p in pts] == [0.7, 0.6] and round(pts[1].change, 6) == -0.1


def test_gap_between_links_stays_empty_without_forward_fill():
    data = prices(a={0: 0.2, 1: 0.25, 2: 0.9, 3: 0.95}, b={5: 0.5, 6: 0.55})   # a's prices after its end date must not count
    pts = link_series([link("a", 1), link("b", 6)], RULES, data)
    assert [p.day for p in pts] == [day(0), day(1), day(5), day(6)]              # days 2-4: no value, nothing carried forward
    assert pts[2].roll and pts[2].change is None and pts[2].value == pts[1].value  # no previous B price: level kept, no jump
    assert round(pts[3].value - pts[2].value, 6) == 0.05
    assert not link_series([link("a", 1)], RULES, prices(a={5: 0.4}))            # expired single link: nothing after the end
    summary = history_summary(pts)
    assert summary["trading_days"] == 4 and summary["gap_days"] == 3 and summary["gaps"] == [
        {"from": day(2).isoformat(), "to": day(4).isoformat(), "days": 3}] and summary["short_history"]


def test_rule_split_keeps_only_catalog_rules(caplog):
    with caplog.at_level(logging.WARNING):
        rules, notes = chain_rules.split(["front_chain", "smooth_7d"], ["Gewicht 50 % auf Kalshi"], "chain", "test")
    assert rules == ["direction", "roll_end_date", "front_chain", "no_gap_fill", "no_forward_fill"]
    assert notes == ["Unbekannte Regel „smooth_7d“", "Gewicht 50 % auf Kalshi"] and "outside the catalog" in caplog.text
    assert chain_rules.split(["roll_end_date", "front_chain"], [], "single")[0] == ["direction", "no_gap_fill", "no_forward_fill"]


def spec(notes=()) -> dict:
    first, last = days()[0], days()[-1]
    return {"links": [{"source": "polymarket", "external_id": "pm-x", "title": "Recession?", "roll_on": last.isoformat(), "direction": "down"}],
            "rules": ["direction", "no_gap_fill", "no_forward_fill", "invented"], "notes": list(notes), "domains": ["Makro"],
            "assessment": {"first": first.isoformat()}}


def test_structured_metric_is_stored_as_draft_and_checked(caplog):
    session = memory_session()
    snapshots(session, lambda i: 0.2 + 0.01 * i)
    with caplog.at_level(logging.INFO):
        key, state, ev = create_structured(session, "Kein Rezessionsrisiko", "single", spec(["Glätten über 7 Tage"]), "Begründung")
    row = session.get(CustomMetric, key)
    assert row.kind == "single" and row.spec["links"][0]["direction"] == "down" and row.spec["notes"] == ["Glätten über 7 Tage"]
    assert state.status == "draft" and session.get(MetricState, key).checks
    assert {c["key"]: c["status"] for c in ev.checks} == {"lookahead": "ok", "coverage": "ok", "roll_jumps": "ok"}
    assert ev.defn.rules == ("direction", "no_gap_fill", "no_forward_fill")        # unknown rule key never executed
    assert "nicht automatisch umgesetzt: Glätten über 7 Tage" in ev.defn.calculation
    assert [round(p.level, 6) for p in ev.points[:2]] == [0.8, 0.79]
    plain = create_structured(session, "Ohne Hinweis", "single", spec(), "", None)[2]
    assert [p.value for p in plain.points] == [p.value for p in ev.points]        # notes do not change the calculation
    assert "structured metric" in caplog.text and "chain metric" in caplog.text


def test_structured_metric_needs_links():
    with pytest.raises(ValueError):
        create_structured(memory_session(), "X", "chain", {"links": []})


def test_basket_metric_keeps_its_values():
    session = memory_session()
    snapshots(session, lambda i: 0.2 + 0.01 * i)
    create_basket(session, "cm_korb", "Korb", "", [{"source": "polymarket", "external_id": "pm-x", "title": "Recession?"}])
    defn = basket_def(session.get(CustomMetric, "cm_korb"))
    before = [(p.day, p.value, p.liquidity, p.level) for p in basket_points(session, defn, days()[-1])[0]]
    create_structured(session, "Kette", "single", spec())
    after = evaluate_baskets(session, days()[-1])
    assert [(p.day, p.value, p.liquidity, p.level) for p in after["cm_korb"].points] == before
    assert [round(v, 6) for _, v, _, _ in before] == [round(0.2 + 0.01 * i, 6) for i in range(len(days()))]
    assert {c["key"] for c in after["cm_korb"].checks} == {"lookahead", "coverage"} and session.get(CustomMetric, "cm_korb").kind == "basket"


def test_migration_adds_kind_and_spec(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    db_url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    cols = {c["name"]: c for c in inspect(create_engine(db_url)).get_columns("custom_metrics")}
    assert {"kind", "spec", "markets"} <= set(cols) and "basket" in str(cols["kind"]["default"])
