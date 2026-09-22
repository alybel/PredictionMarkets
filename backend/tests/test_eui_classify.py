"""Classification: keyword fallback, tag rule, Claude client parsing (faked) and persistence."""

import json
from types import SimpleNamespace

from backend.app.eui.classify import (
    AnthropicClassifier,
    ClassificationInput,
    KeywordClassifier,
    classifications_for,
    classifier_from_env,
    classify_missing,
    tag_rule,
)
from backend.app.eui.models import POLARITY_BAD_IF_YES, POLARITY_SYMMETRIC, STATUS_PROPOSED
from backend.tests.eui_helpers import market, memory_session


def test_keyword_fallback_and_tag_rule():
    items = [
        ClassificationInput("polymarket", "1", "Will the US enter a recession in 2026?"),
        ClassificationInput("polymarket", "2", "Will the Fed cut rates in December?"),
        ClassificationInput("polymarket", "3", "Who wins the Super Bowl?", ("Sports",)),
    ]
    decisions = KeywordClassifier().classify(items)
    assert [d.is_economic for d in decisions] == [True, True, False]
    assert decisions[0].polarity == POLARITY_BAD_IF_YES and decisions[1].polarity == POLARITY_SYMMETRIC
    assert tag_rule(items[2]).is_economic is False and tag_rule(items[2]).model == "tag-rule"
    assert tag_rule(items[0]) is None


def test_classifier_from_env_falls_back_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(classifier_from_env(), KeywordClassifier)


class FakeMessages:
    def __init__(self, text, stop_reason="end_turn", error=None):
        self.text, self.stop_reason, self.error, self.calls = text, stop_reason, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)])


def test_anthropic_classifier_parses_schema_response_without_network():
    answer = json.dumps({"decisions": [
        {"id": "polymarket:1", "is_economic": True, "polarity": "bad_if_yes", "rationale": "recession"},
        {"id": "polymarket:2", "is_economic": False, "polarity": "weird", "rationale": "sport"},
        {"id": "polymarket:unknown", "is_economic": True, "polarity": "symmetric", "rationale": "ignored"},
    ]})
    fake = FakeMessages(answer)
    clf = AnthropicClassifier(client=SimpleNamespace(messages=fake), model="claude-opus-5", batch_size=2)
    items = [ClassificationInput("polymarket", str(i), f"Q{i}") for i in (1, 2, 3)]
    decisions = clf.classify(items)
    assert len(fake.calls) == 2  # two batches
    assert fake.calls[0]["model"] == "claude-opus-5"
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"
    by_id = {d.external_id: d for d in decisions}
    assert by_id["1"].polarity == "bad_if_yes" and by_id["1"].model == "claude-opus-5"
    assert by_id["2"].polarity == POLARITY_SYMMETRIC  # unknown polarity falls back to symmetric
    assert "unknown" not in by_id


def test_anthropic_classifier_survives_errors_and_refusals():
    failing = AnthropicClassifier(client=SimpleNamespace(messages=FakeMessages("", error=RuntimeError("boom"))))
    assert failing.classify([ClassificationInput("polymarket", "1", "Q")]) == []
    refused = AnthropicClassifier(client=SimpleNamespace(messages=FakeMessages("{}", stop_reason="refusal")))
    assert refused.classify([ClassificationInput("polymarket", "1", "Q")]) == []


def test_classify_missing_persists_once_and_reuses_decisions():
    session = memory_session()
    markets = [
        market(external_id="1", title="Will the US enter a recession in 2026?"),
        market(external_id="2", title="Who wins the Super Bowl?", tags=["Sports"]),
    ]
    assert classify_missing(session, markets, KeywordClassifier()) == 2
    stored = classifications_for(session, [("polymarket", "1"), ("polymarket", "2")])
    assert stored[("polymarket", "1")].is_economic and stored[("polymarket", "1")].status == STATUS_PROPOSED
    assert stored[("polymarket", "2")].model == "tag-rule"
    # Second run: nothing new, existing decisions untouched even if the classifier would decide differently.
    class Contrarian:
        name = "contrarian"
        def classify(self, items):
            raise AssertionError("must not be called for known markets")
    assert classify_missing(session, markets, Contrarian()) == 0
    assert classify_missing(session, markets + [market(external_id="3", title="Fed decision?")], KeywordClassifier(), limit=0) == 0
