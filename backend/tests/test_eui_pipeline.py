"""Snapshot persistence, monthly composition, daily value and the full daily run - no network."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.eui.classify import KeywordClassifier, classify_missing
from backend.app.eui.compute import build_composition, compute_index, get_composition, period_of
from backend.app.eui.daily import run_daily
from backend.app.eui.models import (
    POLARITY_BAD_IF_YES,
    STATUS_REJECTED,
    IndexComposition,
    MarketClassification,
    MarketSnapshot,
    SnapshotRun,
)
from backend.app.eui.snapshot import snapshot_rows, take_snapshot
from backend.tests.eui_helpers import FakeClient, days_ago, market, memory_session

DAY1 = date(2026, 9, 22)
DAY2 = date(2026, 9, 23)
NEXT_MONTH = date(2026, 10, 1)
NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)

ECON = [
    market(external_id="fed", title="Will the Fed cut rates in December 2026?", price=0.50, liquidity=40_000.0, volume_24h=10_000.0),
    market(external_id="rec", title="Will the US enter a recession in 2026?", price=0.30, liquidity=10_000.0, volume_24h=0.0),
    market(external_id="cpi", title="Will CPI inflation exceed 4% in 2026?", price=0.80, liquidity=2_500.0, volume_24h=0.0),
]
NOISE = [
    market(external_id="nba", title="Who wins the NBA finals?", tags=["Sports"], liquidity=500_000.0),
    market(external_id="old", title="Will GDP growth exceed 3%?", end_date=days_ago(1)),
    market(external_id="decided", title="Will the Fed hike rates?", price=0.99),
]
KALSHI = [market(source="kalshi", external_id="FED-DEC", title="Fed cuts rates December 2026?", price=0.40, liquidity=0.0, volume_24h=10_000.0)]


def clients(kalshi=KALSHI):
    return [FakeClient("polymarket", ECON + NOISE), FakeClient("kalshi", kalshi)]


def test_snapshot_stores_complete_state_with_eligibility_and_replaces_same_day():
    session = memory_session()
    run = take_snapshot(session, clients=clients(), snapshot_date=DAY1, now=NOW)
    assert run.status == "ok" and run.source_counts == {"polymarket": 6, "kalshi": 1}
    rows = snapshot_rows(session, DAY1)
    assert len(rows) == 7 and run.eligible_count == 5
    by_id = {r.external_id: r for r in rows}
    assert by_id["old"].exclusion_reason == "ended" and by_id["decided"].exclusion_reason == "price_outside_corridor"
    assert by_id["nba"].eligible  # eligibility is not about topic
    assert by_id["fed"].liquidity_score == 50_000.0 and by_id["fed"].spread == pytest.approx(0.02)
    # Second run the same day replaces the rows instead of duplicating them.
    take_snapshot(session, clients=clients(), snapshot_date=DAY1, now=NOW)
    assert len(snapshot_rows(session, DAY1)) == 7
    assert session.scalar(select(SnapshotRun.id).order_by(SnapshotRun.id.desc())) == 2


def test_snapshot_marks_partial_and_failed_runs():
    session = memory_session()
    partial = take_snapshot(session, clients=[FakeClient("polymarket", ECON), FakeClient("kalshi", [])], snapshot_date=DAY1, now=NOW)
    assert partial.status == "partial" and "kalshi" in partial.note
    failed = take_snapshot(session, clients=[FakeClient("polymarket", []), FakeClient("kalshi", [])], snapshot_date=DAY2, now=NOW)
    assert failed.status == "failed"


def test_snapshot_deduplicates_repeated_markets_and_survives_client_crash():
    session = memory_session()
    twice = [market(external_id="fed"), market(external_id="fed", price=0.7)]
    run = take_snapshot(session, clients=[FakeClient("polymarket", twice), FakeClient("kalshi", [])], snapshot_date=DAY1, now=NOW)
    rows = snapshot_rows(session, DAY1)
    assert len(rows) == 1 and rows[0].price == 0.5 and run.source_counts["polymarket"] == 2

    class Crashing:
        source = "kalshi"
        def fetch_markets(self):
            raise RuntimeError("boom")
    failed = take_snapshot(session, clients=[Crashing()], snapshot_date=DAY2, now=NOW)
    assert failed.status == "failed" and "boom" in failed.note and failed.finished_at is not None


def prepared_session():
    session = memory_session()
    take_snapshot(session, clients=clients(), snapshot_date=DAY1, now=NOW)
    classify_missing(session, snapshot_rows(session, DAY1, eligible_only=True), KeywordClassifier())
    return session


def test_composition_merges_duplicates_and_is_fixed_per_month():
    session = prepared_session()
    composition = build_composition(session, DAY1)
    assert composition.period == "2026-09"
    ids = {c["component_id"] for c in composition.constituents}
    assert ids == {"polymarket:fed+kalshi:FED-DEC", "polymarket:rec", "polymarket:cpi"}
    assert sum(c["weight"] for c in composition.constituents) == pytest.approx(1.0)
    merged = next(c for c in composition.constituents if "+" in c["component_id"])
    assert merged["primary"] == ["polymarket", "fed"] and merged["liquidity_score"] == 60_000.0
    # A day later in the same month the stored composition is reused, even though the snapshot differs.
    take_snapshot(session, clients=[FakeClient("polymarket", ECON[:1]), FakeClient("kalshi", [])], snapshot_date=DAY2, now=NOW)
    compute_index(session, DAY2)
    assert session.scalars(select(IndexComposition)).all() == [composition]
    # The first snapshot of the next month rebuilds it.
    take_snapshot(session, clients=[FakeClient("polymarket", ECON[:1]), FakeClient("kalshi", [])], snapshot_date=NEXT_MONTH, now=NOW)
    compute_index(session, NEXT_MONTH)
    assert get_composition(session, "2026-10") is not None and len(get_composition(session, "2026-10").constituents) == 1


def test_compute_index_value_polarity_dropouts_and_review():
    session = prepared_session()
    value = compute_index(session, DAY1)
    assert value.value_date == DAY1 and value.constituent_count == 3
    details = {d["component_id"]: d for d in value.details}
    assert details["polymarket:fed+kalshi:FED-DEC"]["price"] == pytest.approx((0.5 * 50_000 + 0.4 * 10_000) / 60_000)
    assert details["polymarket:rec"]["polarity"] == POLARITY_BAD_IF_YES and details["polymarket:rec"]["contribution"] == 0.30
    assert details["polymarket:cpi"]["contribution"] == pytest.approx(0.80)  # "exceed" -> bad if yes
    expected = 100 * sum(d["weight"] * d["contribution"] for d in value.details)
    assert value.value == pytest.approx(expected) and 0 <= value.value <= 100

    # Next day: the recession market ended, so it drops out and the weights renormalize.
    ended = [market(external_id="rec", title="Will the US enter a recession in 2026?", end_date=days_ago(1))]
    take_snapshot(session, clients=[FakeClient("polymarket", ECON[:1] + ended + ECON[2:]), FakeClient("kalshi", KALSHI)], snapshot_date=DAY2, now=NOW)
    day2 = compute_index(session, DAY2)
    assert day2.constituent_count == 2 and sum(d["weight"] for d in day2.details) == pytest.approx(1.0)

    # Human review: a rejected classification leaves immediately, a polarity change applies at once.
    cpi = session.scalar(select(MarketClassification).where(MarketClassification.external_id == "cpi"))
    cpi.status = STATUS_REJECTED
    fed = session.scalar(select(MarketClassification).where(MarketClassification.external_id == "fed"))
    fed.polarity = POLARITY_BAD_IF_YES
    session.commit()
    reviewed = compute_index(session, DAY2)
    assert reviewed.constituent_count == 1
    assert reviewed.details[0]["contribution"] == pytest.approx(reviewed.details[0]["price"])
    assert session.scalar(select(MarketSnapshot.id).where(MarketSnapshot.snapshot_date == DAY2)) is not None


def test_run_daily_returns_summary_and_stores_value():
    session = memory_session()
    summary = run_daily(session, clients=clients(), classifier=KeywordClassifier(), day=DAY1)
    assert summary["snapshot"]["status"] == "ok" and summary["classified"] == 5
    assert summary["index"]["value_date"] == DAY1.isoformat() and summary["index"]["constituents"] == 3
    assert period_of(DAY1) == "2026-09"
    # Recompute without fetching reuses the stored snapshot.
    again = run_daily(session, day=DAY1, classify=False, fetch=False)
    assert again["index"]["value"] == pytest.approx(summary["index"]["value"]) and "snapshot" not in again
