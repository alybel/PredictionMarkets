"""EUI endpoints: public value without raw data, admin review, manual run - no network."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app
from backend.app.deps import get_session
from backend.app.eui.api import get_classifier, get_clients
from backend.app.eui.classify import KeywordClassifier
from backend.tests.eui_helpers import FakeClient, market, memory_session

MARKETS = [
    market(external_id="fed", title="Will the Fed cut rates in December 2026?", price=0.5, liquidity=40_000.0),
    market(external_id="rec", title="Will the US enter a recession in 2026?", price=0.3, liquidity=10_000.0),
    market(external_id="nba", title="Who wins the NBA finals?", tags=["Sports"], liquidity=500_000.0),
]


@pytest.fixture()
def client():
    session = memory_session()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_clients] = lambda: [FakeClient("polymarket", MARKETS), FakeClient("kalshi", [])]
    app.dependency_overrides[get_classifier] = lambda: KeywordClassifier()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_public_value_before_and_after_run_names_sources_only(client):
    empty = client.get("/eui").json()
    assert empty["value"] is None and empty["sources"] == ["Polymarket", "Kalshi"]

    summary = client.post("/eui/admin/run").json()
    assert summary["snapshot"]["status"] == "partial" and summary["index"]["constituents"] == 2

    body = client.get("/eui").json()
    assert body["value_date"] == date.today().isoformat()
    assert 0 <= body["value"] <= 100 and body["constituent_count"] == 2
    assert "Polymarket" in body["attribution"] and "Kalshi" in body["attribution"]
    assert set(body) == {"value_date", "value", "constituent_count", "as_of", "sources", "attribution"}  # no raw data

    history = client.get("/eui/history?days=30").json()
    assert len(history) == 1 and set(history[0]) == {"value_date", "value", "constituent_count"}


def test_admin_review_flow_and_validation(client):
    client.post("/eui/admin/run")
    proposals = client.get("/eui/admin/classifications").json()
    assert {p["external_id"] for p in proposals} == {"fed", "rec"}  # economic proposals only
    assert all(p["status"] == "proposed" for p in proposals)
    rec = next(p for p in proposals if p["external_id"] == "rec")

    updated = client.put(f"/eui/admin/classifications/{rec['id']}", json={"status": "confirmed", "polarity": "symmetric"}).json()
    assert updated["status"] == "confirmed" and updated["polarity"] == "symmetric" and updated["reviewed_at"]
    assert client.get("/eui/admin/classifications?status=confirmed").json()[0]["id"] == rec["id"]

    assert client.put(f"/eui/admin/classifications/{rec['id']}", json={"status": "maybe"}).status_code == 400
    assert client.put(f"/eui/admin/classifications/{rec['id']}", json={"polarity": "up"}).status_code == 400
    assert client.put("/eui/admin/classifications/9999", json={"status": "confirmed"}).status_code == 404

    constituents = client.get("/eui/admin/constituents").json()
    assert constituents["period"] == date.today().strftime("%Y-%m") and len(constituents["constituents"]) == 2
    runs = client.get("/eui/admin/runs").json()
    assert runs[0]["status"] == "partial" and runs[0]["source_counts"] == {"polymarket": 3, "kalshi": 0}


def test_events_and_plausibility_endpoints(client):
    created = client.post("/eui/admin/events", json={"event_date": "2026-09-22", "label": "Strait closure", "kind": "geopolitical"})
    assert created.status_code == 201 and created.json()["kind"] == "geopolitical"
    assert client.post("/eui/admin/events", json={"event_date": "2026-09-22", "label": "  "}).status_code == 400
    report = client.get("/eui/plausibility").json()
    assert report["verdict"] == "insufficient_data" and report["backtest_possible"] is False


def test_dashboard_ships_eui_section(client):
    html = client.get("/").text
    assert 'id="eui"' in html and "/static/eui.js" in html
    assert "Polymarket und Kalshi" in html
    assert client.get("/static/eui.js").status_code == 200
