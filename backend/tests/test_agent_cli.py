"""Index agent via the Claude console (1016-1639): fake process runner, no real CLI call, no live LLM."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from backend.app.agent import cli_agent
from backend.app.agent.assistant import AGENT_MODEL, TERMS_MODEL, AgentError, AnthropicAgent, RuleAgent, agent_from_env
from backend.app.agent.chaining import build_chains
from backend.app.agent.cli_agent import ClaudeCliAgent, transcript
from backend.tests.test_index_agent import POOL


class FakeRun:
    """Stands in for subprocess.run; answers each call with the next prepared result."""

    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def done(payload, rc=0, stderr=""):
    return SimpleNamespace(returncode=rc, stdout=json.dumps(payload), stderr=stderr)


def agent(*outputs):
    return ClaudeCliAgent("/bin/claude", AGENT_MODEL, TERMS_MODEL, run=FakeRun(*outputs), timeout=5)


def test_terms_and_blueprint_via_console():
    chains = build_chains(POOL)
    ref = chains[0].key
    bp = {"name": "Fed", "thesis": "t", "search_terms": ["fed"], "harmonization": [], "caveats": [],
          "components": [{"ref": ref, "direction": "up", "weight": 1, "reason": "r"}, {"ref": "chain:erfunden", "direction": "up", "weight": 1, "reason": "x"}]}
    a = agent(done({"is_error": False, "structured_output": {"terms": ["Fed", " Rate Cut "]}}),
              done({"is_error": False, "result": "", "structured_output": {"reply": "Hier dein Entwurf", "blueprint": bp}}))
    assert a.terms("Zinssenkungen") == ["fed", "rate cut"]
    result = a.respond([{"role": "user", "content": "Fed"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "mehr"}], {}, chains, POOL)
    assert result.reply == "Hier dein Entwurf" and result.model == AGENT_MODEL + " (Konsole)"
    assert [c["ref"] for c in result.blueprint["components"]] == [ref]  # invented reference dropped
    (terms_cmd, _), (cmd, kw) = a._run.calls
    assert terms_cmd[terms_cmd.index("--model") + 1] == TERMS_MODEL and cmd[cmd.index("--model") + 1] == AGENT_MODEL
    assert cmd[:2] == ["/bin/claude", "-p"] and cmd[cmd.index("--tools") + 1] == "" and "--no-session-persistence" in cmd
    assert "<assistant>\nok\n</assistant>" in kw["input"] and "<context>" in kw["input"] and kw["timeout"] == 5


def test_result_text_is_parsed_when_structured_output_missing():
    a = agent(done({"is_error": False, "result": '{"terms": ["cpi"]}'}))
    assert a.terms("Inflation") == ["cpi"]


@pytest.mark.parametrize("out", [done({"is_error": True, "result": "Not logged in"}, rc=1), SimpleNamespace(returncode=1, stdout="", stderr="boom"),
                                 subprocess.TimeoutExpired("claude", 5), FileNotFoundError("claude"), done({"is_error": False, "result": "kein json"})])
def test_console_errors_raise_agent_error_and_terms_fall_back(out):
    with pytest.raises(AgentError):
        agent(out).respond([{"role": "user", "content": "Fed"}], {}, [], [])
    assert agent(out).terms("Fed cut") == ["fed", "cut"]


def test_last_message_must_be_user():
    with pytest.raises(AgentError):
        agent().respond([{"role": "assistant", "content": "x"}], {}, [], [])


def test_transcript_keeps_roles_and_context():
    text = transcript([{"role": "user", "content": "a"}, {"role": "system", "content": "ignored"}], {"k": 1})
    assert text.startswith("<user>\na\n</user>") and "ignored" not in text and text.endswith('{"k": 1}\n</context>')


def test_backend_selection(monkeypatch):
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli_agent, "find_cli", lambda: "/bin/claude")
    chosen = agent_from_env()
    assert isinstance(chosen, ClaudeCliAgent) and chosen.binary == "/bin/claude"
    monkeypatch.setattr(cli_agent, "find_cli", lambda: None)
    assert isinstance(agent_from_env(), RuleAgent)
    monkeypatch.setattr(cli_agent, "find_cli", lambda: "/bin/claude")
    monkeypatch.setenv("AGENT_BACKEND", "rule")
    assert isinstance(agent_from_env(), RuleAgent)
    monkeypatch.setenv("AGENT_BACKEND", "api")
    monkeypatch.setattr("anthropic.Anthropic", lambda: object())
    assert isinstance(agent_from_env(), AnthropicAgent)


def test_find_cli_honours_env(monkeypatch, tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CLI", str(exe))
    assert cli_agent.find_cli() == str(exe)
    monkeypatch.setenv("CLAUDE_CLI", str(tmp_path / "fehlt"))
    assert cli_agent.find_cli() is None
