"""Index builder from validated metrics (1015-2157): selection, save rules, standardization, warning after a block,
read-only archive of rule indices, metric draft from Index Manufacture, frontend wiring. No network, no LLM."""

import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets, get_session
from backend.app.builder.service import index_series, standardize
from backend.app.engine import get_rule, upsert_rule
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.fed.api import get_fed_source
from backend.app.manufacture.models import IndexDraft
from backend.app.metrics.catalog import MetricPoint
from backend.app.metrics.models import MetricState
from backend.app.metrics.service import create_basket
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_catalog import EmptySource, days, raw_points, store

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FED, BASKET, DRAFT = "fed_front_linked", "mf_basket", "mf_draft"


def snapshots(session, prices):
    """One point-in-time snapshot row of market pm-x per Fed fixture day."""
    for i, day in enumerate(days()):
        run = SnapshotRun(snapshot_date=day, status="ok")
        session.add(run)
        session.flush()
        session.add(MarketSnapshot(run_id=run.id, snapshot_date=day, source="polymarket", external_id="pm-x", title="Recession?",
                                   tags=["Economics"], price=prices(i), volume_24h_usd=20_000.0,
                                   fetched_at=datetime.combine(day, time(8), tzinfo=timezone.utc)))
    session.commit()


@pytest.fixture()
def env():
    session = memory_session()
    store(session, raw_points())
    snapshots(session, lambda i: 0.2 + 0.01 * i)
    create_basket(session, BASKET, "Rezessions-Korb", "", [{"source": "polymarket", "external_id": "pm-x", "title": "Recession?"}], None)
    create_basket(session, DRAFT, "Entwurf ohne Freigabe", "", [{"source": "polymarket", "external_id": "pm-x"}], None)
    upsert_rule(session, "legacy", categories=["Economics"])
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_fed_source] = EmptySource
    app.dependency_overrides[get_markets] = lambda: []
    client = TestClient(app)
    for key in (FED, BASKET):
        assert client.post(f"/metrics/{key}/approve").status_code == 200
    yield client, session
    app.dependency_overrides.clear()


def two_metric_index(client, name="Makro"):
    return client.post("/builder/indices", json={"name": name, "components": [{"key": FED, "weight": 3}, {"key": BASKET, "weight": 1}]})


# ---- selection and saving -----------------------------------------------------------------

def test_selection_lists_only_validated_metrics(env, caplog):
    client, _ = env
    caplog.set_level(logging.INFO)
    client.post("/metrics/fed_second_linked/block", json={"reason": "Test"})
    keys = [m["key"] for m in client.get("/builder/metrics").json()["metrics"]]
    assert keys == [FED, BASKET]  # mf_draft (draft) and fed_second_linked (blocked) never appear
    assert "builder selection: 2 validated of 4 metrics" in caplog.text


@pytest.mark.parametrize("key", [DRAFT, "fed_second_linked"])
def test_saving_with_a_draft_or_blocked_metric_is_refused(env, key, caplog):
    client, _ = env
    client.post("/metrics/fed_second_linked/block")
    caplog.set_level(logging.INFO)
    res = client.post("/builder/indices", json={"name": "X", "components": [{"key": FED, "weight": 1}, {"key": key, "weight": 1}]})
    assert res.status_code == 409 and "nicht freigegeben" in res.json()["detail"]
    assert "metric index 'X' create refused (409)" in caplog.text
    assert client.get("/builder/indices").json()["indices"] == []


@pytest.mark.parametrize("components", [[], [{"key": FED, "weight": 0}], [{"key": "nope", "weight": 1}],
                                        [{"key": FED, "weight": 1}, {"key": FED, "weight": 2}]])
def test_invalid_components_are_refused_with_400(env, components):
    client, _ = env
    assert client.post("/builder/indices", json={"name": "X", "components": components}).status_code == 400


def test_create_update_and_name_conflicts(env, caplog):
    client, _ = env
    caplog.set_level(logging.INFO)
    assert two_metric_index(client).status_code == 201
    assert "metric index 'Makro' saved with" in caplog.text
    assert two_metric_index(client).status_code == 409 and two_metric_index(client, "legacy").status_code == 409
    res = client.put("/builder/indices/Makro", json={"components": [{"key": BASKET, "weight": 2}]})
    assert res.status_code == 200 and res.json()["components"] == [{"key": BASKET, "weight": 2.0}]
    assert client.put("/builder/indices/missing", json={"components": [{"key": FED}]}).status_code == 404


# ---- standardization and warning ----------------------------------------------------------------

def test_standardization_is_an_expanding_z_score_without_look_ahead():
    start = days()[0]
    bp = [MetricPoint(start + timedelta(i), -i, start, None, False) for i in range(6)]
    prob = [MetricPoint(start + timedelta(i), 0.1 * (i + 1), start, None, False) for i in range(6)]
    z_bp, z_prob = standardize(bp), standardize(prob)
    assert start + timedelta(3) not in z_bp                       # fewer than five values: no z-score yet
    assert z_bp[start + timedelta(4)] == pytest.approx(-2 ** 0.5)  # uses only days 0..4, never day 5
    assert z_prob[start + timedelta(4)] == pytest.approx(2 ** 0.5)
    series = index_series({"bp": z_bp, "p": z_prob}, {"bp": 3, "p": 1})
    assert series[0]["value"] == pytest.approx(-(2 ** 0.5) / 2, abs=1e-4) and len(series) == 2


def test_index_of_two_units_returns_the_standardized_value(env):
    client, _ = env
    two_metric_index(client)
    body = client.get("/builder/indices/Makro").json()
    assert {c["unit"] for c in body["components"]} == {"bp", "probability"} and not body["warning"]
    last = body["points"][-1]
    expected = (3 * last["components"][FED] + last["components"][BASKET]) / 4
    assert last["value"] == pytest.approx(expected, abs=1e-3)
    assert len(body["points"]) == len(days()) - 4 and all(abs(p["value"]) < 5 for p in body["points"])


def test_blocked_metric_keeps_the_index_running_with_a_warning(env, caplog):
    client, _ = env
    two_metric_index(client)
    before = client.get("/builder/indices/Makro").json()["points"]
    caplog.set_level(logging.INFO)
    assert client.post(f"/metrics/{BASKET}/block", json={"reason": "Quelle fehlerhaft"}).status_code == 200
    body = client.get("/builder/indices/Makro").json()
    assert body["points"] == before                                   # still computed
    assert body["warning"] and "gesperrt" in body["warnings"][0] and "Quelle fehlerhaft" in body["warnings"][0]
    assert next(c for c in body["components"] if c["key"] == BASKET)["status"] == "blocked"
    assert client.get("/builder/indices").json()["indices"][0]["warning"] is True
    assert "metric index 'Makro' computed with warnings" in caplog.text
    assert two_metric_index(client, "Neu").status_code == 409            # no new index with the blocked metric


# ---- archive of rule-based indices ----------------------------------------------------------------

def test_rule_indices_are_a_read_only_archive(env, caplog):
    client, session = env
    caplog.set_level(logging.INFO)
    listing = client.get("/indices").json()
    assert listing[0]["index_name"] == "legacy" and listing[0]["archived"] is True
    assert client.get("/indices/legacy").status_code == 200
    assert client.get("/indices/legacy/history").status_code == 200
    rule = {"categories": ["Sports"], "keywords": [], "min_liquidity": 10_000, "min_price": 0.03, "max_price": 0.97}
    assert client.put("/indices/legacy", json=rule).status_code == 409
    assert client.post("/indices", json={"index_name": "neu", **rule}).status_code == 409
    assert client.put("/builder/indices/legacy", json={"components": [{"key": FED}]}).status_code == 409
    assert client.put("/indices/missing", json=rule).status_code == 404
    assert get_rule(session, "legacy").categories == ["Economics"]
    assert "archive index 'legacy' update refused (409)" in caplog.text


# ---- Index Manufacture ---------------------------------------------------------------------------

def test_manufacture_creates_a_metric_draft(env, caplog):
    client, session = env
    draft = IndexDraft(name="Inflation-Korb", methodology="Mittel der Inflationsmärkte",
                       markets=[{"id": "polymarket:pm-x", "source": "polymarket", "external_id": "pm-x", "title": "Recession?"}])
    session.add(draft)
    session.commit()
    caplog.set_level(logging.INFO)
    res = client.post(f"/manufacture/drafts/{draft.id}/metric")
    assert res.status_code == 201, res.text
    key = res.json()["metric_key"]
    assert res.json()["status"] == "draft" and session.get(MetricState, key).status == "draft"
    assert res.json()["draft"]["status"] == "metric" and res.json()["draft"]["published_index"] == key
    assert key not in [m["key"] for m in client.get("/builder/metrics").json()["metrics"]]
    assert client.post(f"/manufacture/drafts/{draft.id}/metric").status_code == 409
    assert client.post("/manufacture/drafts/999/metric").status_code == 404
    assert f"metric {key} 'Inflation-Korb' created from draft {draft.id}" in caplog.text


def test_manufacture_draft_without_markets_is_refused():
    session = memory_session()
    session.add(IndexDraft(name="Leer", markets=[]))
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    try:
        assert TestClient(app).post("/manufacture/drafts/1/metric").status_code == 400
    finally:
        app.dependency_overrides.clear()


# ---- frontend --------------------------------------------------------------------------------------

def test_builder_frontend_offers_metrics_only_and_shows_the_warning():
    html = (STATIC / "index.html").read_text()
    builder = html[html.index('data-view-panel="indices"'):html.index('data-view-panel="quality"')]
    for gone in ('id="min-liquidity"', 'id="min-price"', 'id="keyword-input"', 'id="tag-list"', 'id="save-rule"', 'id="delete-index"'):
        assert gone not in builder
    for element in ('id="mi-choices"', 'id="mi-warning"', 'id="mi-result"', 'id="mi-components"', "Archiv, nur lesbar"):
        assert element in builder
    assert '<script src="/static/metricindex.js"></script>' in html and "/static/params.js" not in html
    assert "Als Index veröffentlichen" not in html
    js = (STATIC / "metricindex.js").read_text()
    assert 'miFetch("/builder/metrics")' in js and '"/builder/indices/"' in js and 'method: "PUT"' in js
    for event in ('"builder.metrics.loaded"', '"builder.index.saved"', '"builder.index.save.refused"', '"builder.index.warning"'):
        assert event in js
    assert 'fetch("/builder/indices")' in (STATIC / "indices.js").read_text()
