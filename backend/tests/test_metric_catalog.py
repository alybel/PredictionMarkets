"""Metric catalog (1015-2157): checks (lookahead, coverage, roll jumps, plausibility), status logic, release (409 on a red
check), automatic block after a data load, persistence across a restart, overview endpoint. No network."""

import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from backend.app.api import app, get_session
from backend.app.db import make_session_factory
from backend.app.fed.api import get_fed_source
from backend.app.fed.metrics import RawPoint, build_chains, combine, session_values, all_sessions, Computed
from backend.app.fed.models import FedMarket, FedPrice
from backend.app.metrics.catalog import BY_KEY, metric_points
from backend.app.metrics.checks import check_lookahead, check_plausibility, check_roll_jumps
from backend.app.metrics.models import MetricState
from backend.app.metrics.service import approve, block, evaluate
from backend.app.schema_state import alembic_config
from backend.tests.eui_helpers import memory_session

A, B, C = date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29)   # three FOMC meetings
FIRST, LAST = date(2026, 1, 19), date(2026, 2, 6)
TODAY = date(2026, 3, 1)
PROBS = {A: {0: 0.7, -25: 0.3}, B: {0: 0.4, -25: 0.6}, C: {0: 0.2, -25: 0.8}}  # -7.5, -15, -20 bp
SKEWED_B = {-25: 0.2, -50: 0.8}  # Polymarket expects -45 bp for B, Kalshi -15 bp
FRONT, SECOND = BY_KEY["fed_front_linked"], BY_KEY["fed_second_linked"]


def days(first=FIRST, last=LAST):
    return [first + timedelta(n) for n in range((last - first).days + 1)]


def raw_points(day_list=None, hour=4, probs=PROBS, sources=("kalshi", "polymarket")):
    """Prices of every meeting not decided yet on each day (a meeting is priced up to its own day)."""
    out = []
    for d in day_list or days():
        for m, outcome in probs.items():
            if d <= m:
                for s in sources:
                    out += [RawPoint(s, m, bp, f"{bp:+d} bp", f"{s}-{m}-{bp}", d, p, None,
                                     datetime.combine(d, time(hour), tzinfo=timezone.utc), "historical") for bp, p in outcome.items()]
    return out


def computed(points):
    accepted, rejected = session_values(points)
    return Computed(points, combine(accepted), rejected, all_sessions(points))


def evaluated(points, defn=FRONT):
    data = computed(points)
    chains = build_chains(data.values, data.sessions)
    return data, metric_points(defn, chains)


def store(session, points):
    """Write raw points into the Fed raw layer (one market per source, meeting and outcome)."""
    markets = {(m.source, m.external_id): m for m in session.query(FedMarket)}
    for p in points:
        market = markets.get((p.source, p.external_id))
        if market is None:
            market = FedMarket(source=p.source, external_id=p.external_id, event_id=str(p.session_date), session_date=p.session_date,
                               outcome_bp=p.outcome_bp, outcome_label=p.outcome_label, title=p.outcome_label)
            session.add(market)
            session.flush()
            markets[(p.source, p.external_id)] = market
        session.add(FedPrice(market_id=market.id, price_date=p.day, price=p.price, observed_at=p.observed_at, origin=p.origin))
    session.commit()


class EmptySource:
    def list_markets(self):
        return []

    def history(self, info, now):
        return []


@pytest.fixture()
def db():
    session = memory_session()
    store(session, raw_points())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_fed_source] = EmptySource
    yield session
    app.dependency_overrides.clear()


@pytest.fixture()
def client(db):
    return TestClient(app)


def by_key(payload):
    return {m["key"]: m for m in payload["metrics"]}


# ---- checks -----------------------------------------------------------------------------------

def test_linked_fed_chain_shows_no_jump_on_the_roll_day():
    data, points = evaluated(raw_points())
    roll = next(p for p in points if p.roll)
    before = next(p for p in points if p.day == roll.day - timedelta(1))
    assert roll.day == A + timedelta(1) and roll.session_date == B
    raw = build_chains(data.values, data.sessions)["combined"]["front"]
    assert next(p.value for p in raw if p.day == roll.day) - next(p.value for p in raw if p.day == A) == pytest.approx(-7.5)
    assert roll.value == pytest.approx(before.value)          # linked level: no jump although -7.5 -> -15 bp
    assert check_roll_jumps(points)["status"] == "ok"
    unlinked = metric_points(replace(FRONT, field="value"), build_chains(data.values, data.sessions))
    jumps = check_roll_jumps(unlinked)
    assert jumps["status"] == "blocked" and jumps["examples"][0]["sprung_bp"] == pytest.approx(-7.5)


def test_lookahead_check_fires_on_a_value_after_the_decision_on_the_meeting_day():
    late = [p for p in raw_points([A], hour=19) if p.session_date == A]
    points = [p for p in raw_points() if not (p.day == A and p.session_date == A)] + late
    data, front = evaluated(points)
    result = check_lookahead(FRONT, data, front, TODAY)
    assert result["status"] == "blocked" and result["count"] == len(late)
    assert result["examples"][0]["grund"] == "nach der Zinsentscheidung"
    _, second = evaluated(points, SECOND)
    assert check_lookahead(SECOND, data, second, TODAY)["status"] == "ok"   # meeting B on day A is unaffected
    clean, clean_front = evaluated(raw_points())
    assert check_lookahead(FRONT, clean, clean_front, TODAY)["status"] == "ok"


def test_plausibility_check_fires_on_a_large_kalshi_polymarket_gap():
    points = raw_points(sources=("kalshi",)) + raw_points(probs={**PROBS, B: SKEWED_B}, sources=("polymarket",))
    data, front = evaluated(points)
    result = check_plausibility(data, front)
    assert result["status"] == "blocked" and result["examples"][0]["abstand_bp"] == pytest.approx(30.0)
    data_ok, front_ok = evaluated(raw_points())
    assert check_plausibility(data_ok, front_ok)["status"] == "ok"


def test_all_checks_green_on_clean_data():
    data = computed(raw_points())
    evaluations = evaluate(data, build_chains(data.values, data.sessions), TODAY)
    assert set(evaluations) == {"fed_front_linked", "fed_second_linked"}
    for ev in evaluations.values():
        assert [c["key"] for c in ev.checks] == ["lookahead", "coverage", "roll_jumps", "plausibility"] and ev.failing == []


# ---- status logic and endpoints --------------------------------------------------------------

def test_overview_lists_status_and_checks_of_all_metrics(client):
    metrics = by_key(client.get("/metrics").json())
    assert list(metrics) == ["fed_front_linked", "fed_second_linked"]
    front = metrics["fed_front_linked"]
    assert front["name"] == "Fed Front-Kette, verkettet (bp)" and front["unit"] == "bp"
    assert front["status"] == "draft" and front["all_green"] and front["can_approve"] and front["validated_at"] is None
    assert {c["key"]: c["status"] for c in front["checks"]} == dict.fromkeys(["lookahead", "coverage", "roll_jumps", "plausibility"], "ok")
    assert front["series"][0]["date"] == FIRST.isoformat() and len(front["series"]) == len(days())


def test_green_metric_can_be_approved(client, caplog):
    caplog.set_level(logging.INFO)
    r = client.post("/metrics/fed_front_linked/approve")
    assert r.status_code == 200 and r.json()["status"] == "validated" and r.json()["validated_at"]
    front = by_key(client.get("/metrics").json())["fed_front_linked"]
    assert front["status"] == "validated" and front["validated_at"] and not front["can_approve"]
    assert "metric fed_front_linked status draft -> validated (manual release)" in caplog.text
    assert "metric fed_front_linked released at" in caplog.text


def test_release_with_a_red_check_is_refused_with_409(db, client):
    db.query(FedPrice).delete()
    db.query(FedMarket).delete()
    store(db, raw_points(sources=("kalshi",)) + raw_points(probs={**PROBS, B: SKEWED_B}, sources=("polymarket",)))
    r = client.post("/metrics/fed_front_linked/approve")
    assert r.status_code == 409 and "Plausibilität" in r.json()["detail"]
    front = by_key(client.get("/metrics").json())["fed_front_linked"]
    assert front["status"] == "blocked" and not front["can_approve"]


def test_validated_metric_with_new_gap_is_blocked_after_a_data_load(db, client, caplog):
    assert client.post("/metrics/fed_front_linked/approve").json()["status"] == "validated"
    store(db, raw_points([LAST + timedelta(14)]))  # 13 missing days of 33 -> 39 % > 10 %
    caplog.set_level(logging.INFO)
    assert client.post("/fed/load").status_code == 200
    front = by_key(client.get("/metrics").json())["fed_front_linked"]
    assert front["status"] == "blocked" and "Mindestabdeckung" in front["status_reason"]
    assert next(c for c in front["checks"] if c["key"] == "coverage")["status"] == "blocked"
    assert "metric fed_front_linked status validated -> blocked (automatic, fed load)" in caplog.text
    assert "metric fed_front_linked checks (fed load)" in caplog.text


def test_manual_block_and_unknown_metric(client, caplog):
    caplog.set_level(logging.INFO)
    client.post("/metrics/fed_second_linked/approve")
    r = client.post("/metrics/fed_second_linked/block", json={"reason": "Test"})
    assert r.status_code == 200 and r.json()["status"] == "blocked" and r.json()["status_reason"] == "Manuell gesperrt: Test"
    assert by_key(client.get("/metrics").json())["fed_second_linked"]["can_approve"]  # green again -> release possible
    assert client.post("/metrics/nope/approve").status_code == 404 and client.post("/metrics/nope/block").status_code == 404
    assert "metric fed_second_linked blocked manually (Test)" in caplog.text
    assert client.post("/metrics/check").status_code == 200


def test_status_release_and_block_survive_a_restart(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'metrics.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(alembic_config(), "head")
    engine = create_engine(url)
    with make_session_factory(engine)() as session:
        store(session, raw_points())
        approve(session, "fed_front_linked", TODAY)
        block(session, "fed_second_linked", "Neustart-Test")
    engine.dispose()
    with make_session_factory(create_engine(url))() as session:  # "restart": fresh engine and session
        front, second = session.get(MetricState, "fed_front_linked"), session.get(MetricState, "fed_second_linked")
        assert front.status == "validated" and front.validated_at is not None and len(front.checks) == 4
        assert second.status == "blocked" and second.blocked_at is not None and second.status_reason == "Manuell gesperrt: Neustart-Test"
