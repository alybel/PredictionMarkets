"""Index Manufacture endpoints: catalog search, assistant turn, draft CRUD and metric drafts - no network, no LLM."""

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets
from backend.app.deps import get_session
from backend.app.engine import get_rule
from backend.app.manufacture.api import get_assistant, get_catalog_markets
from backend.app.manufacture.assistant import AssistantError, AssistantResult, normalize_draft
from backend.app.manufacture.catalog import build_catalog
from backend.tests.eui_helpers import market, memory_session

MARKETS = [
    market(external_id="fed", title="Will the Fed cut rates in December?", tags=["Economics"], liquidity=50_000.0,
           description="Resolves Yes if the FOMC lowers the target range."),
    market(source="kalshi", external_id="rec", title="US recession declared in 2026?", tags=["Economics"], liquidity=20_000.0),
    market(external_id="nba", title="Lakers win the NBA title?", tags=["Sports"], liquidity=90_000.0),
]


class FakeAssistant:
    name = "fake-model"

    def __init__(self):
        self.calls = []
        self.error = None

    def respond(self, messages, draft, candidates):
        self.calls.append((messages, draft, [c.key for c in candidates]))
        if self.error:
            raise self.error
        raw = {**draft, "name": draft["name"] or "Recession Watch", "keywords": ["recession"],
               "markets": draft["markets"] + [{"id": "kalshi:rec", "reason": "core question"}]}
        return AssistantResult("Vorschlag steht.", normalize_draft(raw, candidates, draft), self.name)


@pytest.fixture()
def client():
    session = memory_session()
    assistant = FakeAssistant()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_catalog_markets] = lambda: MARKETS
    app.dependency_overrides[get_markets] = lambda: MARKETS  # /indices must never hit the live sources
    app.dependency_overrides[get_assistant] = lambda: assistant
    tc = TestClient(app)
    tc.assistant = assistant
    yield tc
    app.dependency_overrides.clear()
    session.close()


DRAFT = {"name": "Recession Watch", "intent": "US recession risk", "description": "Odds of a US recession.",
         "methodology": "Liquidity weighted.", "keywords": ["recession"], "tags": ["Economics"], "min_liquidity": 15_000,
         "markets": [{"id": "kalshi:rec", "source": "kalshi", "external_id": "rec", "title": "US recession declared in 2026?", "reason": "core"}],
         "messages": [{"role": "user", "content": "US recession risk"}, {"role": "assistant", "content": "Vorschlag steht."}],
         "model": "fake-model"}


def test_catalog_search_with_facets_and_detail(client):
    data = client.get("/manufacture/catalog", params={"q": "fomc"}).json()
    assert data["total"] == 1 and data["items"][0]["id"] == "polymarket:fed" and data["population"] == 3
    assert data["items"][0]["description"].startswith("Resolves") and data["min_liquidity_floor"] == 10_000.0

    data = client.get("/manufacture/catalog", params={"tag": ["Economics"], "sort": "liquidity"}).json()
    assert [i["id"] for i in data["items"]] == ["polymarket:fed", "kalshi:rec"]
    assert data["facets"]["sources"] == [{"source": "kalshi", "count": 1}, {"source": "polymarket", "count": 1}]

    assert client.get("/manufacture/catalog", params={"source": "kalshi"}).json()["facets"]["tags"] == [
        {"tag": "Economics", "count": 1}]  # only tags of markets matching the filters

    assert client.get("/manufacture/catalog/kalshi/rec").json()["title"] == "US recession declared in 2026?"
    assert client.get("/manufacture/catalog/kalshi/nope").status_code == 404


def test_catalog_top_100_query_caps_at_the_most_liquid_hundred(client):
    """Query the "Top 100 liquideste" button sends: no filters, sort=liquidity, limit=100."""
    many = [market(source="kalshi" if i % 3 else "polymarket", external_id=f"m{i}", liquidity=10_000.0 + i) for i in range(120)]
    app.dependency_overrides[get_catalog_markets] = lambda: many

    data = client.get("/manufacture/catalog", params={"sort": "liquidity", "limit": 100, "offset": 0}).json()

    assert data["total"] == 120 and data["population"] == 120 and len(data["items"]) == 100
    liquidity = [i["liquidity"] for i in data["items"]]
    assert liquidity == sorted(liquidity, reverse=True) and liquidity[-1] == 10_020.0


def test_catalog_price_corridor_defaults_to_3_and_97_cents(client):
    app.dependency_overrides[get_catalog_markets] = lambda: [
        market(external_id=f"p{p}", price=p / 100) for p in (1, 3, 50, 97, 99)]

    data = client.get("/manufacture/catalog").json()
    assert (data["min_price"], data["max_price"]) == (0.03, 0.97)
    assert (data["default_min_price"], data["default_max_price"]) == (0.03, 0.97)
    assert sorted(i["external_id"] for i in data["items"]) == ["p3", "p50", "p97"]

    data = client.get("/manufacture/catalog", params={"min_price": 0.4, "max_price": 1}).json()
    assert sorted(i["external_id"] for i in data["items"]) == ["p50", "p97", "p99"]


@pytest.mark.parametrize("bounds", [(0.5, 0.5), (0.6, 0.4), (-0.1, 0.5), (0.1, 1.2)])
def test_catalog_rejects_invalid_price_corridor(client, bounds):
    res = client.get("/manufacture/catalog", params={"min_price": bounds[0], "max_price": bounds[1]})
    assert res.status_code == 400


def test_assist_turn_returns_reply_and_normalized_draft(client):
    body = {"messages": [{"role": "user", "content": "recession risk"}], "draft": {"markets": [{"id": "polymarket:nba", "reason": "mine"}], "min_liquidity": 1}}
    res = client.post("/manufacture/assist", json=body)

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["reply"] == "Vorschlag steht." and data["model"] == "fake-model" and data["candidates"] == 3
    assert [m["id"] for m in data["draft"]["markets"]] == ["polymarket:nba", "kalshi:rec"]
    messages, draft, keys = client.assistant.calls[0]
    assert keys[0] == "polymarket:nba" and draft["min_liquidity"] == 10_000.0  # pinned pick first, floor applied


def test_assist_validation_and_model_failure(client):
    assert client.post("/manufacture/assist", json={"messages": []}).status_code == 422
    bad = {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]}
    assert client.post("/manufacture/assist", json=bad).status_code == 400
    client.assistant.error = AssistantError("Das Modell hat die Anfrage abgelehnt.")
    res = client.post("/manufacture/assist", json={"messages": [{"role": "user", "content": "a"}]})
    assert res.status_code == 502 and "abgelehnt" in res.json()["detail"]


def test_draft_crud(client):
    created = client.post("/manufacture/drafts", json=DRAFT)
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["status"] == "draft" and row["version"] == 1 and row["market_count"] == 1 and len(row["messages"]) == 2
    assert row["markets"][0]["title"] == "US recession declared in 2026?"

    listing = client.get("/manufacture/drafts").json()
    assert [d["id"] for d in listing] == [row["id"]] and "messages" not in listing[0]

    updated = client.put(f"/manufacture/drafts/{row['id']}", json={**DRAFT, "name": "Recession Watch v2", "min_liquidity": 100}).json()
    assert updated["name"] == "Recession Watch v2" and updated["version"] == 2 and updated["min_liquidity"] == 10_000.0
    assert client.get(f"/manufacture/drafts/{row['id']}").json()["name"] == "Recession Watch v2"

    assert client.delete(f"/manufacture/drafts/{row['id']}").status_code == 204
    assert client.get(f"/manufacture/drafts/{row['id']}").status_code == 404
    assert client.put("/manufacture/drafts/999", json=DRAFT).status_code == 404
    assert client.post("/manufacture/drafts", json={**DRAFT, "name": ""}).status_code == 422


def test_metric_draft_replaces_publishing_as_index(client):
    row = client.post("/manufacture/drafts", json=DRAFT).json()
    session = app.dependency_overrides[get_session]()

    res = client.post(f"/manufacture/drafts/{row['id']}/metric")
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "draft" and res.json()["draft"]["status"] == "metric"
    assert get_rule(session, "Recession Watch") is None  # no rule-based index any more
    assert client.post(f"/manufacture/drafts/{row['id']}/publish").status_code in (404, 405)


def test_catalog_entries_expose_source_descriptions():
    entries = build_catalog(MARKETS)
    assert entries[0].description.startswith("Resolves") and entries[1].description is None
