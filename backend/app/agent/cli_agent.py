"""Index agent via the local Claude console (Claude Code CLI, ``claude -p``) instead of the API.

Uses the console's own login, so no ANTHROPIC_API_KEY is needed. Same models, prompts and JSON schemas as the API
agent; tools, MCP servers and project settings are switched off and the call runs outside the repo, so the model
only sees the prompt. ``run`` is injectable so tests never start a real process.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time

from backend.app.agent.assistant import (MAX_TERMS, OUTPUT_SCHEMA, SYSTEM_PROMPT, TERMS_PROMPT, TERMS_SCHEMA, AgentError, AgentResult,
                                         _context, answer_from, clean_terms)
from backend.app.agent.chaining import Chain
from backend.app.agent.pool import Candidate
from backend.app.manufacture.catalog import tokenize

logger = logging.getLogger(__name__)

CLI_TIMEOUT = 300  # seconds, override with AGENT_CLI_TIMEOUT
TERMS_TIMEOUT = 90
CLI_SUFFIX = " (Konsole)"


def find_cli() -> str | None:
    """Path of the Claude console: CLAUDE_CLI if set, else ``claude`` on PATH or in ~/.local/bin."""
    configured = os.environ.get("CLAUDE_CLI")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    local = os.path.expanduser("~/.local/bin/claude")
    return shutil.which("claude") or (local if os.access(local, os.X_OK) else None)


def transcript(messages: list[dict], context: dict) -> str:
    """The chat as one prompt: earlier turns tagged by role, context appended to the last user message."""
    turns = [f"<{m['role']}>\n{m.get('content', '')}\n</{m['role']}>" for m in messages if m.get("role") in ("user", "assistant")]
    return "\n\n".join(turns) + "\n\n<context>\n" + json.dumps(context, ensure_ascii=False) + "\n</context>"


class ClaudeCliAgent:
    """Haiku for the search terms, Sonnet for the blueprint, each one ``claude -p`` call with structured output."""

    def __init__(self, binary: str, model: str, terms_model: str, run=subprocess.run, timeout: int | None = None):
        self.binary, self.model, self.terms_model, self._run = binary, model, terms_model, run
        self.name = model + CLI_SUFFIX
        self.timeout = timeout or int(os.environ.get("AGENT_CLI_TIMEOUT", CLI_TIMEOUT))

    def _call(self, model: str, system: str, prompt: str, schema: dict, effort: str, timeout: int) -> dict:
        cmd = [self.binary, "-p", "--model", model, "--effort", effort, "--output-format", "json", "--json-schema", json.dumps(schema),
               "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]
        started = time.monotonic()
        logger.info("claude console call: model=%s effort=%s prompt=%d chars", model, effort, len(prompt))
        try:
            proc = self._run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"Claude-Konsole hat nicht innerhalb von {timeout} s geantwortet.") from exc
        except OSError as exc:
            raise AgentError(f"Claude-Konsole nicht startbar: {exc}") from exc
        try:
            out = json.loads(proc.stdout or "")
        except ValueError:
            out = None
        if proc.returncode != 0 or not isinstance(out, dict) or out.get("is_error"):
            detail = (out or {}).get("result") if isinstance(out, dict) else (proc.stderr or proc.stdout or "").strip()
            logger.warning("claude console failed (rc=%s): %s", proc.returncode, str(detail)[:500])
            raise AgentError(f"Claude-Konsole meldet einen Fehler: {str(detail)[:300] or 'keine Ausgabe'}")
        logger.info("claude console answered in %.1fs (cost %.4f USD)", time.monotonic() - started, out.get("total_cost_usd") or 0)
        structured = out.get("structured_output")
        if isinstance(structured, dict):
            return structured
        try:
            return json.loads(out.get("result") or "")
        except ValueError as exc:
            raise AgentError("Antwort der Claude-Konsole war kein gültiges JSON.") from exc

    def structured(self, model: str, system: str, prompt: str, schema: dict, effort: str = "low") -> dict:
        """One structured console call (used by the metric finder)."""
        return self._call(model, system, prompt, schema, effort, self.timeout)

    def terms(self, idea: str) -> list[str]:
        try:
            parsed = self._call(self.terms_model, "Answer only with the requested JSON, in English.", TERMS_PROMPT + idea[:2000],
                                TERMS_SCHEMA, "low", min(TERMS_TIMEOUT, self.timeout))
        except AgentError as exc:  # the keyword fallback keeps the turn alive
            logger.warning("agent terms via console failed, using keywords: %s", exc)
            return tokenize(idea)[:MAX_TERMS]
        return clean_terms(parsed.get("terms"), idea, self.terms_model + CLI_SUFFIX)

    def respond(self, messages: list[dict], blueprint: dict, chains: list[Chain], pool: list[Candidate]) -> AgentResult:
        if not messages or messages[-1].get("role") != "user":
            raise AgentError("the last message must come from the user")
        prompt = transcript(messages, _context(blueprint, chains, pool))
        return answer_from(self._call(self.model, SYSTEM_PROMPT, prompt, OUTPUT_SCHEMA, "medium", self.timeout), chains, pool, self.name)
