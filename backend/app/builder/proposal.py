"""AI proposal for the weighting of a metric index (risk index 5/7).

Sonnet sees the chosen validated metrics (unit, history, liquidity, volatility of the z-scores) and the weights every
method would give today, and answers as structured JSON ({method, weights: [{key, weight}], rationale}).
``validate`` discards the whole proposal when it names an unknown metric or method, or (manual) lacks a valid
weight per metric; data-driven methods always show the weights the app computes itself. A proposal only takes
effect after the user confirms it in the builder and saves the index. Backend choice like the metric finder
(AGENT_BACKEND=api|cli|rule); without a model a rule fallback answers, marked in ``model``.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from backend.app.agent.assistant import AgentError, _text_of
from backend.app.builder.service import standardize
from backend.app.builder.weighting import EQUAL, INVERSE_VOL, LIQUIDITY, MANUAL, METHODS, Weighting, percent
from backend.app.metrics.service import Evaluation

logger = logging.getLogger(__name__)

WEIGHTING_MODEL = "claude-sonnet-5-5"  # override with AGENT_WEIGHTING_MODEL
MAX_RATIONALE = 2000

SYSTEM_PROMPT = """You advise on the weighting of a prediction-market risk index. Its components are quality-checked
metrics, each standardized to an expanding z-score. Choose one method: equal (1/n), liquidity (proportional to recent
daily turnover), inverse_vol (calmer metrics weigh more) or manual (your own weights). The context shows per metric its
unit, history, liquidity, z-score volatility and the weights each method gives today. Use only the given metric keys.
For manual, give a positive weight for every metric; for the other methods repeat the shown weights. Explain the
choice in two to four sentences in German."""

PROPOSAL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["method", "weights", "rationale"], "properties": {
    "method": {"type": "string", "enum": list(METHODS)},
    "weights": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["key", "weight"],
                                           "properties": {"key": {"type": "string"}, "weight": {"type": "number"}}}},
    "rationale": {"type": "string"}}}


class ProposalRejected(ValueError):
    pass


@dataclass(frozen=True)
class Proposal:
    method: str
    weights_pct: dict[str, float]
    rationale: str
    model: str


class Weigher(Protocol):
    name: str

    def propose(self, context: list[dict]) -> dict: ...


def method_weights(evaluations: dict[str, Evaluation], keys: list[str], method: str, manual: dict[str, float] | None = None,
                   day: date = date.max) -> dict[str, float]:
    """Weights in percent of ``method`` on ``day`` (default: with all data)."""
    z_by_key = {k: standardize(evaluations[k].points) for k in keys}
    return percent(Weighting(method, {k: evaluations[k].points for k in keys}, z_by_key, manual).at(day))


def context(evaluations: dict[str, Evaluation], keys: list[str]) -> list[dict]:
    z_by_key = {k: standardize(evaluations[k].points) for k in keys}
    weighting = Weighting(EQUAL, {k: evaluations[k].points for k in keys}, z_by_key)
    previews = {m: method_weights(evaluations, keys, m) for m in (EQUAL, LIQUIDITY, INVERSE_VOL)}
    out = []
    for k in keys:
        ev, vol = evaluations[k], weighting.volatility(k, date.max)
        out.append({"key": k, "name": ev.defn.name, "unit": ev.defn.unit, "history_days": len(ev.points),
                    "liquidity_usd": round(ev.liquidity, 2) if ev.liquidity is not None else None,
                    "z_volatility": round(vol, 4) if vol is not None else None,
                    "weights_pct_by_method": {m: p[k] for m, p in previews.items()}})
    return out


def validate(raw, ctx: list[dict], model: str) -> Proposal:
    """Checked proposal; ``ProposalRejected`` (logged) for unknown metrics/methods or invalid manual weights."""
    keys = [c["key"] for c in ctx]
    if not isinstance(raw, dict) or raw.get("method") not in METHODS:
        method = raw.get("method") if isinstance(raw, dict) else None
        logger.warning("weighting proposal (%s) discarded: unknown method %r", model, method)
        raise ProposalRejected(f"KI-Vorschlag verworfen: unbekanntes Verfahren {method!r}.")
    entries = [w for w in raw.get("weights") or [] if isinstance(w, dict)]
    unknown = sorted({str(w.get("key")) for w in entries} - set(keys))
    if unknown:
        logger.warning("weighting proposal (%s) discarded: unknown metrics %s (allowed %s)", model, unknown, keys)
        raise ProposalRejected("KI-Vorschlag verworfen: unbekannte Metrik(en) " + ", ".join(unknown) + ".")
    method = raw["method"]
    if method == MANUAL:
        manual = {w["key"]: w.get("weight") for w in entries}
        if set(manual) != set(keys) or not all(isinstance(v, (int, float)) and v > 0 for v in manual.values()):
            logger.warning("weighting proposal (%s) discarded: manual weights invalid %s", model, manual)
            raise ProposalRejected("KI-Vorschlag verworfen: manuelle Gewichte fehlen oder sind nicht positiv.")
        weights = percent({k: float(manual[k]) for k in keys})
    else:
        weights = {c["key"]: c["weights_pct_by_method"][method] for c in ctx}
    rationale = raw.get("rationale") if isinstance(raw.get("rationale"), str) else ""
    return Proposal(method, weights, rationale.strip()[:MAX_RATIONALE], model)


def propose(weigher: Weigher, evaluations: dict[str, Evaluation], keys: list[str]) -> Proposal:
    ctx = context(evaluations, keys)
    logger.info("weighting proposal requested (%s) for %s", weigher.name, keys)
    proposal = validate(weigher.propose(ctx), ctx, weigher.name)
    logger.info("weighting proposal (%s): method %s, weights %s, rationale %r", proposal.model, proposal.method,
                proposal.weights_pct, proposal.rationale[:300])
    return proposal


def prompt(ctx: list[dict]) -> str:
    return "<metrics>\n" + json.dumps(ctx, ensure_ascii=False) + "\n</metrics>"


class ApiWeigher:
    def __init__(self, client=None, model: str = WEIGHTING_MODEL):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client, self.name = client, model

    def propose(self, ctx: list[dict]) -> dict:
        try:
            response = self._client.messages.create(
                model=self.name, max_tokens=4096, system=SYSTEM_PROMPT, messages=[{"role": "user", "content": prompt(ctx)}],
                output_config={"format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}})
        except Exception as exc:
            logger.warning("weighting proposal call failed: %s", exc)
            raise AgentError(f"Modellaufruf fehlgeschlagen: {exc}") from exc
        if response.stop_reason != "end_turn":
            raise AgentError(f"Antwort unvollständig (stop_reason={response.stop_reason}).")
        try:
            return json.loads(_text_of(response))
        except ValueError as exc:
            raise AgentError("Antwort des Modells war kein gültiges JSON.") from exc


class CliWeigher:
    def __init__(self, cli_agent, model: str = WEIGHTING_MODEL):
        self._cli, self._model, self.name = cli_agent, model, model + " (Konsole)"

    def propose(self, ctx: list[dict]) -> dict:
        return self._cli.structured(self._model, SYSTEM_PROMPT, prompt(ctx), PROPOSAL_SCHEMA, effort="medium")


class RuleWeigher:
    """Stand-in without a model: liquidity when every metric has turnover, else inverse volatility, else equal."""

    name = "rule-fallback"

    def propose(self, ctx: list[dict]) -> dict:
        if all(c["liquidity_usd"] for c in ctx):
            method, why = LIQUIDITY, "alle Metriken Umsatzdaten haben – liquidere Märkte sind verlässlicher"
        elif all(c["z_volatility"] for c in ctx):
            method, why = INVERSE_VOL, "nicht alle Metriken Umsatzdaten haben, aber jede genug Historie für eine Schwankungsmessung"
        else:
            method, why = EQUAL, "Daten für Liquidität und Schwankung unvollständig sind"
        return {"method": method, "weights": [{"key": c["key"], "weight": c["weights_pct_by_method"][method]} for c in ctx],
                "rationale": f"Kein KI-Modell verfügbar (Regel): „{METHODS[method]}“, weil {why}."}


def weigher_from_env() -> Weigher:
    """Same backend choice as the index agent (AGENT_BACKEND=api|cli|rule; default API key -> console -> rules)."""
    from backend.app.agent.cli_agent import ClaudeCliAgent, find_cli

    backend = os.environ.get("AGENT_BACKEND", "").strip().lower()
    model = os.environ.get("AGENT_WEIGHTING_MODEL", WEIGHTING_MODEL)
    if backend == "api" or (not backend and os.environ.get("ANTHROPIC_API_KEY")):
        return ApiWeigher(model=model)
    cli = find_cli() if backend in ("", "cli") else None
    if cli:
        return CliWeigher(ClaudeCliAgent(cli, model=model, terms_model=model), model)
    logger.warning("weighting proposal without language model (backend=%r): rule fallback", backend or "auto")
    return RuleWeigher()
