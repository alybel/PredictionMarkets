"""Haiku pre-screening: default model, reference values in the request, new fields parsed and stored,
reference-value endpoints - all with a faked client, no network."""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app
from backend.app.deps import get_session
from backend.app.eui.api import get_classifier, get_clients
from backend.app.eui.classify import (
    CLAUDE_MODEL,
    AnthropicClassifier,
    ClassificationInput,
    KeywordClassifier,
    classifications_for,
    classifier_from_env,
    classify_missing,
    output_config,
)
from backend.app.eui.models import DIRECTION_NA, POLARITY_SYMMETRIC
from backend.app.eui.reference import (
    DEFAULT_INDICATORS,
    reference_payload,
    seed_reference_indicators,
    upsert_reference_value,
)
from backend.tests.eui_helpers import FakeClient, market, memory_session


class FakeMessages:
    def __init__(self, text):
        self.text, self.calls = text, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.text)])


ANSWER = json.dumps({"decisions": [
    {"id": "kalshi:fed-450", "is_economic": True, "polarity": "bad_if_yes", "direction": "increase",
     "reference_indicator": "fed_funds_upper", "uncertainty_note": "A hike above the current 4.25 % signals inflation worry.",
     "rationale": "range above current rate"},
    {"id": "polymarket:rec", "is_economic": True, "polarity": "bad_if_yes", "direction": "sideways",
     "reference_indicator": "", "uncertainty_note": "Recession odds.", "rationale": "recession"},
]})


def test_default_model_is_haiku_and_effort_only_for_other_models(monkeypatch):
    assert CLAUDE_MODEL == "claude-haiku-4-5"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("EUI_CLASSIFIER_MODEL", raising=False)
    monkeypatch.setattr("anthropic.Anthropic", lambda: SimpleNamespace(messages=None))
    assert classifier_from_env().name == "claude-haiku-4-5"
    assert "effort" not in output_config("claude-haiku-4-5")
    assert output_config("claude-opus-5")["effort"] == "low"
    assert output_config("claude-haiku-4-5")["format"]["type"] == "json_schema"


def test_request_carries_reference_values_and_new_fields_are_parsed():
    fake = FakeMessages(ANSWER)
    clf = AnthropicClassifier(client=SimpleNamespace(messages=fake))
    items = [ClassificationInput("kalshi", "fed-450", "Fed funds 4.50-4.75 % after December?"),
             ClassificationInput("polymarket", "rec", "US recession in 2026?")]
    refs = [{"indicator": "fed_funds_upper", "label": "Fed upper", "value": 4.25, "unit": "%", "as_of": "2026-09-16"}]
    decisions = {d.external_id: d for d in clf.classify(items, refs)}

    sent = json.loads(fake.calls[0]["messages"][0]["content"])
    assert sent["reference_values"] == refs and [m["id"] for m in sent["markets"]] == ["kalshi:fed-450", "polymarket:rec"]
    assert fake.calls[0]["model"] == "claude-haiku-4-5" and "effort" not in fake.calls[0]["output_config"]
    schema = fake.calls[0]["output_config"]["format"]["schema"]
    assert {"uncertainty_note", "direction", "reference_indicator"} <= set(schema["properties"]["decisions"]["items"]["required"])

    fed = decisions["fed-450"]
    assert fed.direction == "increase" and fed.reference_indicator == "fed_funds_upper" and fed.reference_value == 4.25
    assert fed.uncertainty_note.startswith("A hike") and fed.polarity == "bad_if_yes"
    rec = decisions["rec"]
    assert rec.direction == DIRECTION_NA and rec.reference_indicator is None and rec.reference_value is None


def test_prescreening_is_stored_once_with_reference_context():
    session = memory_session()
    upsert_reference_value(session, "fed_funds_upper", 4.25, unit="%", as_of=date(2026, 9, 16), label="Fed upper")
    fake = FakeMessages(ANSWER)
    clf = AnthropicClassifier(client=SimpleNamespace(messages=fake))
    markets = [market(source="kalshi", external_id="fed-450", title="Fed funds 4.50-4.75 % after December?"),
               market(external_id="rec", title="US recession in 2026?")]
    assert classify_missing(session, markets, clf) == 2
    assert json.loads(fake.calls[0]["messages"][0]["content"])["reference_values"][0]["value"] == 4.25

    stored = classifications_for(session, [("kalshi", "fed-450"), ("polymarket", "rec")])
    fed = stored[("kalshi", "fed-450")]
    assert fed.direction == "increase" and fed.reference_value == 4.25 and fed.uncertainty_note.startswith("A hike")
    assert stored[("polymarket", "rec")].direction == DIRECTION_NA and stored[("polymarket", "rec")].uncertainty_note == "Recession odds."
    # Seen markets are never sent again.
    assert classify_missing(session, markets, clf) == 0 and len(fake.calls) == 1


def test_keyword_fallback_fills_defaults():
    session = memory_session()
    assert classify_missing(session, [market(external_id="x", title="Fed decision?")], KeywordClassifier()) == 1
    row = classifications_for(session, [("polymarket", "x")])[("polymarket", "x")]
    assert row.direction == DIRECTION_NA and row.uncertainty_note is None and row.polarity == POLARITY_SYMMETRIC


def test_reference_seed_and_payload_only_with_values():
    session = memory_session()
    assert seed_reference_indicators(session) == len(DEFAULT_INDICATORS)
    assert seed_reference_indicators(session) == 0  # idempotent
    assert reference_payload(session) == []  # no values yet
    upsert_reference_value(session, "us_cpi_yoy", 2.9, as_of=date(2026, 9, 10))
    payload = reference_payload(session)
    assert payload == [{"indicator": "us_cpi_yoy", "label": "US CPI inflation, year over year", "value": 2.9, "unit": "%", "as_of": "2026-09-10"}]


@pytest.fixture()
def client():
    session = memory_session()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_clients] = lambda: [FakeClient("polymarket", [market(external_id="rec", title="US recession in 2026?")]), FakeClient("kalshi", [])]
    app.dependency_overrides[get_classifier] = lambda: KeywordClassifier()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_reference_endpoints_and_classification_payload(client):
    rows = client.get("/eui/admin/references").json()
    assert {r["indicator"] for r in rows} >= {"fed_funds_upper", "us_unemployment"} and all(r["value"] is None for r in rows)

    updated = client.put("/eui/admin/references/Fed_Funds_Upper", json={"value": 4.25, "as_of": "2026-09-16", "note": "FOMC"}).json()
    assert updated["indicator"] == "fed_funds_upper" and updated["value"] == 4.25 and updated["as_of"] == "2026-09-16"
    assert next(r for r in client.get("/eui/admin/references").json() if r["indicator"] == "fed_funds_upper")["value"] == 4.25
    assert client.put("/eui/admin/references/%20", json={"value": 1}).status_code == 400

    client.post("/eui/admin/run")
    proposal = client.get("/eui/admin/classifications").json()[0]
    assert {"uncertainty_note", "direction", "reference_indicator", "reference_value"} <= set(proposal)
    assert proposal["direction"] == "n/a"
