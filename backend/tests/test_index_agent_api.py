"""Index agent (1016-1617), agent side: Claude calls with a fake client, rule fallback, endpoints, tab. No network, no live LLM."""

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.agent import api as agent_api
from backend.app.agent.assistant import AGENT_MODEL, TERMS_MODEL, AgentError, AnthropicAgent, RuleAgent, agent_from_env
from backend.app.agent.chaining import build_chains
from backend.app.api import app
from backend.app.deps import get_session
from backend.app.metrics.models import CustomMetric
from backend.tests.eui_helpers import memory_session
from backend.tests.test_index_agent import D, POOL, add
from backend.tests.topic_helpers import review_list

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


class FakeMessages:
    def __init__(self, text, stop_reason="end_turn", error=None):
        self.text, self.stop_reason, self.error, self.calls = text, stop_reason, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=self.text)])


def fake_client(blueprint_text, terms_text='{"terms": ["Fed", "rate cut"]}', **kw):
    return SimpleNamespace(messages=FakeMessages(terms_text), beta=SimpleNamespace(messages=FakeMessages(blueprint_text, **kw)))


def test_claude_agent_uses_haiku_for_terms_and_sonnet_for_blueprint():
    chains = build_chains(POOL)
    answer = {"reply": "Hier ist ein Start.", "blueprint": {"name": "Zinsen", "thesis": "t", "search_terms": ["fed"], "caveats": [],
              "components": [{"ref": chains[0].key, "direction": "up", "weight": 1, "reason": "r"},
                             {"ref": "kalshi:invented", "direction": "up", "weight": 1, "reason": "r"}], "harmonization": []}}
    client = fake_client(json.dumps(answer))
    agent = AnthropicAgent(client)
    assert agent.terms("Zinssenkungen der Fed") == ["fed", "rate cut"]
    assert client.messages.calls[0]["model"] == TERMS_MODEL == "claude-haiku-5-5"
    result = agent.respond([{"role": "user", "content": "Zinsindex"}], {}, chains, POOL)
    call = client.beta.messages.calls[0]
    assert call["model"] == AGENT_MODEL == "claude-sonnet-5-5" and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    context = json.loads(call["messages"][-1]["content"].split("<context>\n")[1].split("\n</context>")[0])
    assert context["chains"][0]["id"] == chains[0].key and context["chains"][0]["links"] == 3
    assert "polymarket:plain" in [m["id"] for m in context["markets"]]
    assert "polymarket:jan" not in [m["id"] for m in context["markets"]]  # chain links are not offered twice
    assert result.reply == "Hier ist ein Start." and [c["ref"] for c in result.blueprint["components"]] == [chains[0].key]


def test_terms_fall_back_to_keywords_on_error():
    client = fake_client("{}")
    client.messages = FakeMessages("", error=RuntimeError("down"))
    assert AnthropicAgent(client).terms("Rezession USA Arbeitsmarkt") == ["rezession", "usa", "arbeitsmarkt"]


@pytest.mark.parametrize("kw", [{"stop_reason": "refusal"}, {"stop_reason": "max_tokens"}, {"error": RuntimeError("boom")}, {}])
def test_failures_raise_agent_error(kw):
    agent = AnthropicAgent(fake_client("not json", **kw))
    with pytest.raises(AgentError):
        agent.respond([{"role": "user", "content": "x"}], {}, [], POOL)
    with pytest.raises(AgentError):
        agent.respond([{"role": "assistant", "content": "x"}], {}, [], POOL)


def test_rule_agent_prefers_chains_and_long_histories():
    result = RuleAgent().respond([{"role": "user", "content": "Fed cut Index"}], {}, build_chains(POOL), POOL)
    kinds = [c["kind"] for c in result.blueprint["components"]]
    assert kinds == ["chain"] and result.model == "rule-fallback" and "ANTHROPIC_API_KEY" in result.reply


def test_agent_from_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    assert isinstance(agent_from_env(), RuleAgent)


@pytest.fixture()
def api():
    s = memory_session()
    for i in range(5):
        add(s, D + timedelta(days=i), "jan", "Fed cut in January 2026?")
        add(s, D + timedelta(days=40 + i), "mar", "Fed cut in March 2026?")
        add(s, D + timedelta(days=i), "out", "Fed cut in April 2026?")  # rejected in the review list: never in the pool
    s.commit()
    s.info["topic"] = review_list(s, [("polymarket", "jan", "Fed cut in January 2026?", "high", "accepted"),
                                      ("polymarket", "mar", "Fed cut in March 2026?", "high", "accepted"),
                                      ("polymarket", "out", "Fed cut in April 2026?", "high", "rejected")]).id
    app.dependency_overrides[get_session] = lambda: s
    app.dependency_overrides[agent_api.get_agent_markets] = lambda: []
    app.dependency_overrides[agent_api.get_agent] = RuleAgent
    app.dependency_overrides[agent_api.get_history_fetcher] = lambda: lambda *_: {}  # never the network
    yield TestClient(app), s
    app.dependency_overrides.clear()


def test_blueprint_endpoint_and_metric_draft(api):
    client, session = api
    tid = session.info["topic"]
    body = client.post("/agent/blueprint", json={"topic_id": tid, "messages": [{"role": "user", "content": "Fed cut"}]}).json()
    assert body["model"] == "rule-fallback" and body["chain_total"] == 1 and body["candidates"] == 2
    bp = body["blueprint"]
    assert bp["components"][0]["kind"] == "chain" and bp["history"]["start"] == "2026-01-01"
    assert body["topic"]["id"] == tid
    again = client.post("/agent/blueprint", json={"topic_id": tid, "messages": [{"role": "user", "content": "Fed cut"}, {"role": "assistant", "content": "ok"},
                                                                {"role": "user", "content": "mehr"}], "blueprint": bp}).json()
    assert again["blueprint"]["components"][0]["ref"] == bp["components"][0]["ref"]
    created = client.post("/agent/metric", json={"topic_id": tid, "blueprint": bp})
    assert created.status_code == 201 and created.json()["markets"] == 2 and created.json()["status"] == "draft"
    row = session.get(CustomMetric, created.json()["key"])
    assert [m["external_id"] for m in row.markets] == ["jan", "mar"] and "Verkettung:" in row.calculation


def test_endpoint_errors(api):
    client, session = api
    tid = session.info["topic"]
    assert client.post("/agent/blueprint", json={"topic_id": tid, "messages": [{"role": "assistant", "content": "x"}]}).status_code == 400
    assert client.post("/agent/metric", json={"topic_id": tid, "blueprint": {"name": "x", "components": []}}).status_code == 400
    assert client.post("/agent/blueprint", json={"topic_id": 999, "messages": [{"role": "user", "content": "Fed"}]}).status_code == 404
    assert client.post("/agent/blueprint", json={"messages": [{"role": "user", "content": "Fed"}]}).status_code == 422  # topic required
    outside = {"name": "x", "components": [{"markets": [{"source": "polymarket", "external_id": "out"}]}]}
    assert client.post("/agent/metric", json={"topic_id": tid, "blueprint": outside}).status_code == 400  # rejected market never stored
    app.dependency_overrides[agent_api.get_agent] = lambda: AnthropicAgent(fake_client("kaputt"))
    assert client.post("/agent/blueprint", json={"topic_id": tid, "messages": [{"role": "user", "content": "Fed"}]}).status_code == 502


def test_agent_tab_is_added_and_existing_tabs_stay():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for view in ("indices", "quality", "metrics", "catalog", "topics", "agent"):
        assert f'data-view="{view}"' in html and f'data-view-panel="{view}"' in html
    assert '<script src="/static/agent.js"></script>' in html
    src = (STATIC / "agent.js").read_text(encoding="utf-8")
    for event in ("agent.send", "agent.reply", "agent.failed", "agent.metric.create", "agent.reset", "agent.chain.open"):
        assert f'"{event}"' in src
    if shutil.which("node"):
        subprocess.run(["node", "--check", str(STATIC / "agent.js")], check=True)
