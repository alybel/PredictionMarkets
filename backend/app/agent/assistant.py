"""The index agent: a companion that turns an idea into a blueprint (components, chaining, harmonization).

Two model steps per turn: Haiku turns the idea into English search terms (market titles are English), then
Sonnet assembles the blueprint from the real candidate pool and the detected chains (structured JSON).
``blueprint.resolve`` validates every reference, so the agent can never invent data. Without an API key the
Claude console (``cli_agent``) answers; without both a rule-based fallback picks the longest chains and histories (marked in ``model``).
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from backend.app.agent.blueprint import BLUEPRINT_SCHEMA, MAX_TEXT, resolve
from backend.app.agent.chaining import Chain
from backend.app.agent.pool import Candidate, score
from backend.app.manufacture.catalog import tokenize

logger = logging.getLogger(__name__)

AGENT_MODEL = "claude-sonnet-5-5"   # override with AGENT_MODEL
TERMS_MODEL = "claude-haiku-5-5"    # override with AGENT_TERMS_MODEL
MAX_TERMS = 12
CONTEXT_CHAINS = 40
CONTEXT_MARKETS = 80
FALLBACK_CHAINS = 4
FALLBACK_MARKETS = 4

SYSTEM_PROMPT = """You are the index agent of a prediction-market index platform (Polymarket + Kalshi), the user's companion
for designing an index from an idea. Assemble a starting point the user can refine:
1. Pick components that express the idea: chains (ids "chain:...", successive markets of one recurring question,
   rolled at each end date) or single markets ("source:external_id"). Use only ids from the context.
2. History matters: prefer chains and long stored histories over short-lived markets, say how far back the index can
   go and where gaps are. A market without stored history only helps from today on. A chain with rolls = 0 is a
   term structure (links trade in parallel): useful as front chain, but no longer than its longest link.
3. Only chain what is really comparable: same question, same threshold, only the period differs. Do not mix
   thresholds, different underlyings or opposite phrasings into one component.
4. Harmonize: set direction "down" where a rising price means the opposite of the idea; give weights (they are
   normalized); add harmonization rules beyond the given hints only where needed (e.g. different thresholds,
   different resolution sources, units). Name risks in caveats.
5. search_terms: English title terms that find more fitting markets next turn.
Answer in the user's language, concisely; ask one focused question when the idea is ambiguous, but always return
the complete blueprint."""

OUTPUT_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}, "blueprint": BLUEPRINT_SCHEMA},
                 "required": ["reply", "blueprint"], "additionalProperties": False}
TERMS_PROMPT = ("List up to 12 short English keywords (single words or short phrases, lower case) that "
                "appear in titles of prediction markets fitting this index idea:\n")
TERMS_SCHEMA = {"type": "object", "properties": {"terms": {"type": "array", "items": {"type": "string"}}},
                "required": ["terms"], "additionalProperties": False}


class AgentError(Exception):
    """The model could not produce a usable answer (API error, refusal, unparsable output)."""


@dataclass(frozen=True)
class AgentResult:
    reply: str
    blueprint: dict
    model: str


class Agent(Protocol):
    name: str

    def terms(self, idea: str) -> list[str]: ...

    def respond(self, messages: list[dict], blueprint: dict, chains: list[Chain], pool: list[Candidate]) -> AgentResult: ...


def user_text(messages: list[dict]) -> str:
    return " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")


class RuleAgent:
    """Stand-in without a language model: the longest matching chains and histories, equal weights."""

    name = "rule-fallback"

    def terms(self, idea: str) -> list[str]:
        return tokenize(idea)[:MAX_TERMS]

    def respond(self, messages: list[dict], blueprint: dict, chains: list[Chain], pool: list[Candidate]) -> AgentResult:
        terms = self.terms(user_text(messages)) + list(blueprint.get("search_terms") or [])
        chained = {l.market.key for ch in chains for l in ch.links}
        hits = sorted(((score(ch.label, (), terms), ch) for ch in chains), key=lambda r: (-r[0], -(r[1].rolls > 0), -r[1].history_days))
        picked = [ch.key for n, ch in hits if n][:FALLBACK_CHAINS]
        singles = sorted((c for c in pool if c.key not in chained and score(c.title, (), terms)),
                         key=lambda c: (-score(c.title, (), terms), -c.days, -c.avg_volume))
        picked += [c.key for c in singles[:FALLBACK_MARKETS]]
        raw = {"name": blueprint.get("name") or " ".join(terms[:3]).title(), "thesis": blueprint.get("thesis") or user_text(messages)[:600],
               "search_terms": terms, "components": [{"ref": r, "direction": "up", "weight": 1, "reason": "Stichwort-Treffer"} for r in picked],
               "harmonization": [], "caveats": ["Ohne KI-Modell zusammengestellt: Richtung und Auswahl bitte prüfen."]}
        reply = (f"Kein KI-Modell verfügbar (weder ANTHROPIC_API_KEY noch Claude-Konsole). Regelbasiert: {len(picked)} Bausteine aus "
                 f"{len(chains)} gefundenen Ketten und {len(pool)} Märkten, gleich gewichtet.")
        return AgentResult(reply, resolve(raw, chains, pool), self.name)


class AnthropicAgent:
    """Haiku for the search terms, Sonnet for the blueprint (one call each)."""

    def __init__(self, client=None, model: str = AGENT_MODEL, terms_model: str = TERMS_MODEL):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client
        self.name, self.terms_model = model, terms_model

    def terms(self, idea: str) -> list[str]:
        try:
            response = self._client.messages.create(
                model=self.terms_model, max_tokens=1024, output_config={"format": {"type": "json_schema", "schema": TERMS_SCHEMA}, "effort": "low"},
                messages=[{"role": "user", "content": TERMS_PROMPT + idea[:2000]}])
            terms = json.loads(_text_of(response)).get("terms", [])
        except Exception as exc:  # the keyword fallback keeps the turn alive
            logger.warning("agent terms call failed, using keywords: %s", exc)
            return tokenize(idea)[:MAX_TERMS]
        return clean_terms(terms, idea, self.terms_model)

    def respond(self, messages: list[dict], blueprint: dict, chains: list[Chain], pool: list[Candidate]) -> AgentResult:
        history = [{"role": m["role"], "content": str(m.get("content", ""))} for m in messages if m.get("role") in ("user", "assistant")]
        if not history or history[-1]["role"] != "user":
            raise AgentError("the last message must come from the user")
        context = _context(blueprint, chains, pool)
        history[-1] = {"role": "user", "content": history[-1]["content"] + "\n\n<context>\n" + json.dumps(context, ensure_ascii=False) + "\n</context>"}
        try:
            response = self._client.beta.messages.create(
                model=self.name, max_tokens=16000, system=SYSTEM_PROMPT, messages=history,
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}, "effort": "medium"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        except Exception as exc:
            logger.warning("agent call failed: %s", exc)
            raise AgentError(f"Modellaufruf fehlgeschlagen: {exc}") from exc
        if response.stop_reason == "refusal":
            raise AgentError("Das Modell hat die Anfrage abgelehnt.")
        if response.stop_reason != "end_turn":
            raise AgentError(f"Antwort unvollständig (stop_reason={response.stop_reason}).")
        try:
            parsed = json.loads(_text_of(response))
        except ValueError as exc:
            logger.warning("agent response unparsable: %s", exc)
            raise AgentError("Antwort des Modells war kein gültiges JSON.") from exc
        return answer_from(parsed, chains, pool, self.name)


def clean_terms(terms, idea: str, model: str) -> list[str]:
    """Normalized model terms; the idea's keywords when the model returned none."""
    cleaned = [str(t).strip().lower()[:40] for t in terms if str(t).strip()][:MAX_TERMS] if isinstance(terms, list) else []
    logger.info("agent terms (%s): %s", model, cleaned)
    return cleaned or tokenize(idea)[:MAX_TERMS]


def answer_from(parsed, chains: list[Chain], pool: list[Candidate], model: str) -> AgentResult:
    """Validated result from the model's structured answer ({reply, blueprint})."""
    if not isinstance(parsed, dict):
        raise AgentError("Antwort des Modells hatte ein unerwartetes Format.")
    reply = parsed.get("reply") if isinstance(parsed.get("reply"), str) else ""
    result = AgentResult(reply[:MAX_TEXT], resolve(parsed.get("blueprint"), chains, pool), model)
    logger.info("agent answered with %d components (%s)", len(result.blueprint["components"]), model)
    return result


def _text_of(response) -> str:
    return next((b.text for b in response.content if b.type == "text"), "")


def _context(blueprint: dict, chains: list[Chain], pool: list[Candidate]) -> dict:
    """Compact model context: chains (without link details), unchained markets with history, current blueprint."""
    chained = {l.market.key for ch in chains for l in ch.links}
    singles = sorted((c for c in pool if c.key not in chained), key=lambda c: (-c.days, -c.avg_volume))[:CONTEXT_MARKETS]
    return {
        "current_blueprint": {k: blueprint.get(k) for k in ("name", "thesis", "search_terms")} |
                             {"components": [{k: c.get(k) for k in ("ref", "direction", "weight", "reason")} for c in blueprint.get("components", [])]},
        "chains": [{"id": ch.key, "question": ch.label, "sources": ch.sources, "links": len(ch.links), "rolls": ch.rolls, "first_date": p["first_date"],
                    "last_date": p["last_date"], "history_days": ch.history_days, "gap_days": sum(g["days"] for g in ch.gaps),
                    "example_titles": [l.market.title for l in ch.links[-2:]]}
                   for ch in chains[:CONTEXT_CHAINS] for p in [ch.payload()]],
        "markets": [{"id": c.key, "title": c.title, "days": c.days, "first_date": c.first_date.isoformat() if c.first_date else None,
                     "last_date": c.last_date.isoformat() if c.last_date else None, "live": c.live, "price": c.price,
                     "avg_volume_24h_usd": round(c.avg_volume), "event": c.event_key} for c in singles],
    }


def agent_from_env() -> Agent:
    """AGENT_BACKEND=api|cli|rule forces a backend; default: API key -> API, else Claude console (CLI), else rules."""
    from backend.app.agent.cli_agent import ClaudeCliAgent, find_cli

    backend = os.environ.get("AGENT_BACKEND", "").strip().lower()
    models = {"model": os.environ.get("AGENT_MODEL", AGENT_MODEL), "terms_model": os.environ.get("AGENT_TERMS_MODEL", TERMS_MODEL)}
    if backend == "api" or (not backend and os.environ.get("ANTHROPIC_API_KEY")):
        return AnthropicAgent(**models)
    cli = find_cli() if backend in ("", "cli") else None
    if cli:
        logger.info("index agent uses the Claude console (%s)", cli)
        return ClaudeCliAgent(cli, **models)
    logger.warning("no ANTHROPIC_API_KEY and no Claude console (backend=%r): index agent uses the rule-based fallback", backend or "auto")
    return RuleAgent()
