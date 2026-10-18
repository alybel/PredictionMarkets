"""Agent mode "Metriken finden": existing catalog metrics that fit an idea (step 1 of index development).

The agent searches only the metric catalog (definitions, status, check results, history), never the raw markets.
Haiku answers as structured JSON ({reply, matches: [{key, reason}], gaps}); ``validate`` keeps only keys that
exist in the catalog (unknown keys are dropped and logged), so the agent can never name an invented metric.
Blocked metrics and metrics below ``MIN_HISTORY_DAYS`` trading days stay in the list, marked with warnings.
Without an API key the Claude console answers; without both a keyword fallback (marked in ``model``).
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from backend.app.agent.assistant import AgentError, _text_of
from backend.app.manufacture.catalog import tokenize
from backend.app.metrics.models import BLOCKED, DRAFT, MetricState
from backend.app.metrics.service import evaluate_db

logger = logging.getLogger(__name__)

MIN_HISTORY_DAYS = 180              # "sufficient history": at least this many trading days (days with a value)
FINDER_MODEL = "claude-haiku-5-5"   # override with AGENT_FINDER_MODEL
MAX_IDEA = 4000
MAX_REASON = 600
MAX_GAPS = 12
CONTEXT_CALC = 300

SYSTEM_PROMPT = """You are the metric finder of a prediction-market index platform. A metric is a quality-checked single
market or chain of markets with defined rules. Given the user's index idea and the metric catalog, list every catalog
metric that expresses part of the idea, each with a short reason. Use only keys from the catalog. Also list blocked
metrics and metrics with short history when they fit; the app marks them. In gaps, name the aspects of the idea that
no catalog metric covers (short phrases). Answer in the user's language, concisely."""

FINDER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reply", "matches", "gaps"], "properties": {
    "reply": {"type": "string"},
    "matches": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["key", "reason"],
                                           "properties": {"key": {"type": "string"}, "reason": {"type": "string"}}}},
    "gaps": {"type": "array", "items": {"type": "string"}}}}


@dataclass(frozen=True)
class FinderResult:
    reply: str
    matches: list[dict]
    gaps: list[str]
    dropped: list[str]
    model: str


class Finder(Protocol):
    name: str

    def propose(self, idea: str, catalog: list[dict]) -> dict: ...


def catalog_entries(session: Session) -> list[dict]:
    """Every catalog metric with status, check results and history length (trading days with a value)."""
    out = []
    for key, ev in evaluate_db(session).items():
        state = session.get(MetricState, key)
        checks = (state.checks if state and state.checks else ev.checks) or []
        out.append({"key": key, "name": ev.defn.name, "unit": ev.defn.unit, "source": ev.defn.source, "calculation": ev.defn.calculation,
                    "status": state.status if state else DRAFT, "status_reason": state.status_reason if state else None,
                    "history_days": len(ev.points), "first_date": ev.points[0].day.isoformat() if ev.points else None,
                    "last_date": ev.points[-1].day.isoformat() if ev.points else None,
                    "checks": [{"label": c.get("label"), "status": c.get("status")} for c in checks],
                    "checks_ok": bool(checks) and all(c.get("status") == "ok" for c in checks)})
    return out


def mark(entry: dict) -> dict:
    """Warning flags of a hit: blocked status and history below MIN_HISTORY_DAYS."""
    short, blocked = entry["history_days"] < MIN_HISTORY_DAYS, entry["status"] == BLOCKED
    warnings = [f"Nur {entry['history_days']} Handelstage Historie (Mindestens {MIN_HISTORY_DAYS} empfohlen)."] if short else []
    if blocked:
        warnings.append("Gesperrt: " + (entry.get("status_reason") or "ohne Angabe"))
    return entry | {"short_history": short, "blocked": blocked, "warnings": warnings}


def validate(parsed, catalog: list[dict], model: str) -> FinderResult:
    """Only catalog keys survive (each once); unknown keys are dropped and logged as warning."""
    if not isinstance(parsed, dict):
        raise AgentError("Antwort des Modells hatte ein unerwartetes Format.")
    by_key = {e["key"]: e for e in catalog}
    matches, dropped, seen = [], [], set()
    for m in parsed.get("matches") or []:
        key = str(m.get("key") or "").strip() if isinstance(m, dict) else ""
        if key not in by_key:
            dropped.append(key or repr(m)[:80])
        elif key not in seen:
            seen.add(key)
            matches.append(mark(by_key[key]) | {"reason": str(m.get("reason") or "")[:MAX_REASON]})
    if dropped:
        logger.warning("metric finder (%s) dropped unknown metric keys: %s", model, dropped)
    gaps = [str(g).strip()[:200] for g in parsed.get("gaps") or [] if str(g).strip()][:MAX_GAPS]
    reply = parsed.get("reply") if isinstance(parsed.get("reply"), str) else ""
    return FinderResult(reply[:2000], matches, gaps, dropped, model)


def find(finder: Finder, idea: str, catalog: list[dict]) -> FinderResult:
    result = validate(finder.propose(idea[:MAX_IDEA], catalog), catalog, finder.name)
    logger.info("metric finder (%s): %d of %d catalog metrics matched, %d gaps, %d dropped, flagged %s", finder.name, len(result.matches),
                len(catalog), len(result.gaps), len(result.dropped), [m["key"] for m in result.matches if m["warnings"]])
    return result


def prompt(idea: str, catalog: list[dict]) -> str:
    context = [{"key": e["key"], "name": e["name"], "unit": e["unit"], "calculation": e["calculation"][:CONTEXT_CALC],
                "status": e["status"], "history_days": e["history_days"]} for e in catalog]
    return f"<idea>\n{idea}\n</idea>\n\n<catalog>\n{json.dumps(context, ensure_ascii=False)}\n</catalog>"


class ApiFinder:
    def __init__(self, client=None, model: str = FINDER_MODEL):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client, self.name = client, model

    def propose(self, idea: str, catalog: list[dict]) -> dict:
        try:
            response = self._client.messages.create(
                model=self.name, max_tokens=4096, system=SYSTEM_PROMPT, messages=[{"role": "user", "content": prompt(idea, catalog)}],
                output_config={"format": {"type": "json_schema", "schema": FINDER_SCHEMA}})
        except Exception as exc:
            logger.warning("metric finder call failed: %s", exc)
            raise AgentError(f"Modellaufruf fehlgeschlagen: {exc}") from exc
        if response.stop_reason != "end_turn":
            raise AgentError(f"Antwort unvollständig (stop_reason={response.stop_reason}).")
        try:
            return json.loads(_text_of(response))
        except ValueError as exc:
            raise AgentError("Antwort des Modells war kein gültiges JSON.") from exc


class CliFinder:
    def __init__(self, cli_agent, model: str = FINDER_MODEL):
        self._cli, self._model, self.name = cli_agent, model, model + " (Konsole)"

    def propose(self, idea: str, catalog: list[dict]) -> dict:
        return self._cli.structured(self._model, SYSTEM_PROMPT, prompt(idea, catalog), FINDER_SCHEMA)


class RuleFinder:
    """Keyword stand-in: metrics whose name or calculation contains an idea word; unmatched words become gaps."""

    name = "rule-fallback"

    def propose(self, idea: str, catalog: list[dict]) -> dict:
        terms = tokenize(idea)
        hits = {e["key"]: [t for t in terms if t in (e["name"] + " " + e["calculation"]).lower()] for e in catalog}
        matches = [{"key": k, "reason": "Stichwort-Treffer: " + ", ".join(ts)} for k, ts in hits.items() if ts]
        covered = {t for ts in hits.values() for t in ts}
        return {"reply": f"Kein KI-Modell verfügbar: Stichwortsuche im Katalog, {len(matches)} Treffer.", "matches": matches,
                "gaps": [t for t in terms if t not in covered]}


def finder_from_env() -> Finder:
    """Same backend choice as the index agent (AGENT_BACKEND=api|cli|rule; default API key -> console -> rules)."""
    from backend.app.agent.cli_agent import ClaudeCliAgent, find_cli

    backend = os.environ.get("AGENT_BACKEND", "").strip().lower()
    model = os.environ.get("AGENT_FINDER_MODEL", FINDER_MODEL)
    if backend == "api" or (not backend and os.environ.get("ANTHROPIC_API_KEY")):
        return ApiFinder(model=model)
    cli = find_cli() if backend in ("", "cli") else None
    if cli:
        return CliFinder(ClaudeCliAgent(cli, model=model, terms_model=model), model)
    logger.warning("metric finder without language model (backend=%r): keyword fallback", backend or "auto")
    return RuleFinder()
