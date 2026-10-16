"""Index Manufacture assistant: fallback proposals, Claude call with faked client, draft normalization. No network."""

import json
from types import SimpleNamespace

import pytest

from backend.app.manufacture.assistant import (
    MANUFACTURE_MODEL,
    AnthropicAssistant,
    AssistantError,
    KeywordAssistant,
    assistant_from_env,
    empty_draft,
    normalize_draft,
)
from backend.app.manufacture.catalog import build_catalog
from backend.tests.eui_helpers import market

CANDIDATES = build_catalog([
    market(external_id="fed", title="Will the Fed cut rates in December?", tags=["Economics"], liquidity=50_000.0),
    market(source="kalshi", external_id="rec", title="US recession declared in 2026?", tags=["Economics"], liquidity=20_000.0),
    market(external_id="nba", title="Lakers win the NBA title?", tags=["Sports"], liquidity=90_000.0),
])


class FakeMessages:
    def __init__(self, text, stop_reason="end_turn", error=None):
        self.text, self.stop_reason, self.error, self.calls = text, stop_reason, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)])


ANSWER = json.dumps({
    "reply": "Ich schlage zwei Märkte vor.",
    "draft": {"name": "US Recession Risk", "description": "Tracks recession odds.", "methodology": "Liquidity weighted.",
              "keywords": ["recession", "Recession", " fed "], "tags": ["Economics"], "min_liquidity": 5_000,
              "markets": [{"id": "kalshi:rec", "reason": "direct recession question"},
                          {"id": "polymarket:fed", "reason": "rate cuts signal slowdown"},
                          {"id": "polymarket:ghost", "reason": "does not exist"}]},
})


def test_claude_call_carries_context_and_draft_is_normalized():
    fake = FakeMessages(ANSWER)
    assistant = AnthropicAssistant(client=SimpleNamespace(messages=fake))
    messages = [{"role": "user", "content": "Rezessionsrisiko USA"}]

    result = assistant.respond(messages, empty_draft(), CANDIDATES)

    call = fake.calls[0]
    assert call["model"] == MANUFACTURE_MODEL == "claude-opus-5"
    assert call["output_config"]["format"]["type"] == "json_schema" and call["output_config"]["effort"] == "medium"
    sent = call["messages"][-1]["content"]
    assert sent.startswith("Rezessionsrisiko USA") and "<context>" in sent
    context = json.loads(sent.split("<context>")[1].split("</context>")[0])
    assert [c["id"] for c in context["candidate_markets"]] == ["polymarket:fed", "kalshi:rec", "polymarket:nba"]
    assert context["liquidity_floor_usd"] == 10_000.0
    assert result.reply == "Ich schlage zwei Märkte vor." and result.model == "claude-opus-5"
    assert result.draft["keywords"] == ["recession", "fed"]
    assert result.draft["min_liquidity"] == 10_000.0  # lifted to the floor
    assert [m["id"] for m in result.draft["markets"]] == ["kalshi:rec", "polymarket:fed"]  # ghost dropped
    assert result.draft["markets"][0]["title"] == "US recession declared in 2026?"
    assert result.draft["markets"][0]["reason"] == "direct recession question"


@pytest.mark.parametrize("fake, message", [
    (FakeMessages("", error=RuntimeError("boom")), "Modellaufruf fehlgeschlagen"),
    (FakeMessages("{}", stop_reason="refusal"), "abgelehnt"),
    (FakeMessages("{}", stop_reason="max_tokens"), "unvollständig"),
    (FakeMessages("not json"), "kein gültiges JSON"),
])
def test_failures_raise_assistant_error(fake, message):
    assistant = AnthropicAssistant(client=SimpleNamespace(messages=fake))
    with pytest.raises(AssistantError, match=message):
        assistant.respond([{"role": "user", "content": "x"}], empty_draft(), CANDIDATES)


def test_last_message_must_be_from_user():
    assistant = AnthropicAssistant(client=SimpleNamespace(messages=FakeMessages(ANSWER)))
    with pytest.raises(AssistantError):
        assistant.respond([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], empty_draft(), CANDIDATES)


def test_normalize_keeps_previous_user_picks_outside_the_shortlist():
    previous = {"markets": [{"id": "kalshi:oil", "source": "kalshi", "external_id": "oil", "title": "Brent", "reason": "mine"}]}
    raw = {"markets": [{"id": "kalshi:oil", "reason": ""}, {"id": "polymarket:nba", "reason": "x"}], "min_liquidity": "abc"}

    draft = normalize_draft(raw, CANDIDATES, previous)

    assert [m["id"] for m in draft["markets"]] == ["kalshi:oil", "polymarket:nba"]
    assert draft["markets"][0]["reason"] == "mine" and draft["min_liquidity"] == 10_000.0
    assert normalize_draft(None, CANDIDATES) == empty_draft()


def test_keyword_fallback_proposes_term_matches():
    result = KeywordAssistant().respond([{"role": "user", "content": "recession and fed rates"}], empty_draft(), CANDIDATES)

    assert result.model == "keyword-fallback" and "ANTHROPIC_API_KEY" in result.reply
    assert {m["id"] for m in result.draft["markets"]} == {"kalshi:rec", "polymarket:fed"}
    assert result.draft["keywords"] == ["recession", "fed", "rates"] and result.draft["name"] == "Recession Fed Rates"


def test_assistant_from_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(assistant_from_env(), KeywordAssistant)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MANUFACTURE_MODEL", "claude-sonnet-5")
    monkeypatch.setattr("anthropic.Anthropic", lambda: SimpleNamespace(messages=None))
    assert assistant_from_env().name == "claude-sonnet-5"
