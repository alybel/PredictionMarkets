"""Index builder (1015-0912): history replay over daily snapshots plus the reworked frontend. No network."""

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.engine import apply_funnel, build_rule, get_rule, upsert_rule
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.history import MAX_DAYS
from backend.tests.eui_helpers import memory_session

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
DAY1, DAY2 = date(2026, 3, 1), date(2026, 3, 2)


def snap(session, day: date, ext: str, *, title="Fed cuts rates?", tags=("Economics",), price=0.5,
         volume_24h=20_000.0, end_date=datetime(2027, 1, 1, tzinfo=timezone.utc)):
    run = session.query(SnapshotRun).filter_by(snapshot_date=day).first()
    if run is None:
        run = SnapshotRun(snapshot_date=day, status="ok")
        session.add(run)
        session.flush()
    session.add(MarketSnapshot(run_id=run.id, snapshot_date=day, source="polymarket", external_id=ext, title=title,
                               tags=list(tags), price=price, volume_24h_usd=volume_24h, end_date=end_date,
                               fetched_at=datetime.combine(day, time(8), tzinfo=timezone.utc)))  # observed on its day, before the US close
    session.commit()


@pytest.fixture()
def env():
    session = memory_session()
    upsert_rule(session, "econ", categories=["economics"])
    # day 1: two weighted markets -> (0.2*10k + 0.5*30k) / 40k = 0.425
    snap(session, DAY1, "a", price=0.2, volume_24h=10_000.0)
    snap(session, DAY1, "b", price=0.5, volume_24h=30_000.0)
    # day 2: only "b" qualifies; the others fail tags, liquidity floor, price corridor
    snap(session, DAY2, "b", price=0.6, volume_24h=30_000.0)
    snap(session, DAY2, "c", tags=("Sports",), price=0.9)
    snap(session, DAY2, "d", price=0.1, volume_24h=9_000.0)
    snap(session, DAY2, "e", price=0.99)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_markets] = lambda: []
    yield TestClient(app), session
    app.dependency_overrides.clear()


def test_history_replays_the_stored_rule_per_snapshot_day(env):
    client, _ = env
    body = client.get("/indices/econ/history").json()

    assert [p["date"] for p in body["points"]] == ["2026-03-01", "2026-03-02"]
    first, second = body["points"]
    assert first["value"] == pytest.approx(0.425) and first["market_count"] == 2
    assert first["total_liquidity"] == pytest.approx(40_000.0)
    assert second["value"] == pytest.approx(0.6) and second["market_count"] == 1
    summary = body["summary"]
    assert summary["first"] == pytest.approx(0.425) and summary["last"] == pytest.approx(0.6)
    assert summary["change"] == pytest.approx(0.175)
    assert summary["min"] == pytest.approx(0.425) and summary["max"] == pytest.approx(0.6)


def test_history_limits_to_the_most_recent_days(env):
    client, _ = env
    body = client.get("/indices/econ/history", params={"days": 1}).json()
    assert [p["date"] for p in body["points"]] == ["2026-03-02"]
    assert client.get("/indices/econ/history", params={"days": 0}).status_code == 422
    assert client.get("/indices/econ/history", params={"days": MAX_DAYS + 1}).status_code == 422  # 5 years since 1015-2258


def test_history_end_date_is_checked_against_the_snapshot_day(env):
    """A market that ended after the snapshot day (but before today) still counts on that day."""
    client, session = env
    snap(session, DAY1, "f", price=0.8, volume_24h=40_000.0, end_date=datetime(2026, 3, 1, 18, tzinfo=timezone.utc))
    snap(session, DAY2, "f", price=0.8, volume_24h=40_000.0, end_date=datetime(2026, 3, 1, 18, tzinfo=timezone.utc))
    first, second = client.get("/indices/econ/history").json()["points"]
    assert first["market_count"] == 3
    assert second["market_count"] == 1  # already ended on day 2


def test_history_preview_uses_the_body_rule_and_stores_nothing(env):
    client, session = env
    res = client.post("/indices/econ/history/preview",
                      json={"categories": ["sports"], "keywords": [], "min_liquidity": 10_000, "min_price": 0.03, "max_price": 0.97})
    assert res.status_code == 200
    points = res.json()["points"]
    assert points[0]["value"] is None and points[0]["market_count"] == 0
    assert points[1]["value"] == pytest.approx(0.9)
    assert get_rule(session, "econ").categories == ["economics"]  # stored rule unchanged
    bad = client.post("/indices/econ/history/preview", json={"min_liquidity": 5})
    assert bad.status_code == 400


def test_history_of_unknown_index_and_without_snapshots():
    session = memory_session()
    upsert_rule(session, "empty", keywords=["x"])
    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        assert client.get("/indices/nope/history").status_code == 404
        body = client.get("/indices/empty/history").json()
        assert body["points"] == [] and body["summary"]["last"] is None
    finally:
        app.dependency_overrides.clear()


def test_apply_funnel_accepts_a_reference_time():
    class M:
        title, tags, category, liquidity, price = "x", [], None, 50_000.0, 0.5
        end_date = datetime(2026, 1, 10, tzinfo=timezone.utc)

    rule = build_rule("r")
    assert apply_funnel(rule, [M()], now=M.end_date - timedelta(days=1))[0]
    assert not apply_funnel(rule, [M()])[0]  # default: current time, market already ended


# --- frontend -------------------------------------------------------------------------


def test_builder_is_the_first_tab_with_rules_on_the_right():
    html = (STATIC / "index.html").read_text()
    tabs = html[html.index('<nav class="tabs"'):html.index("</nav>")]
    assert tabs.index('data-view="indices"') < tabs.index('data-view="quality"') and "Index-Builder" in tabs
    aside = html[html.index('<aside class="ib-side">'):html.index("</aside>")]
    assert 'id="rule-summary"' in aside and 'id="save-rule"' not in aside  # archive: read only (1015-2157)
    assert 'id="ib-chart"' in html and 'id="ib-range"' in html and 'data-key="weight"' in html
    assert '<script src="/static/builder.js"></script>' in html
    assert 'let initialView = "indices"' in (STATIC / "shell.js").read_text()
    assert len(html.splitlines()) <= 500


def test_builder_script_loads_history_and_logs(env):
    client, _ = env
    js = client.get("/static/builder.js")
    assert js.status_code == 200
    assert '"/history?days="' in js.text and '"index-loaded"' in js.text
    assert '"builder.history.failed"' in js.text and 'id: "ib-range"' in js.text
    assert "Liquiditätsfilter" in js.text and "Gewichtung" in js.text
    assert "function assignWeights" in (STATIC / "indices.js").read_text()
