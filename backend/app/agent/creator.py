"""Agent mode "Metriken anlegen": chain and single-market proposals only from the accepted markets of a risk topic.

Since 1017-1107 (Risiko-Index 4/7) the pool is the review list of one topic (``topic_pool``): only markets accepted
there, each with its reading from the list. The former direct route from the domain raw data (1016-2013 step 2) is gone.
Sonnet answers as structured JSON (single market or chain, refs, rule keys, free-text rules); the reading is never taken
from the model but always from the review list. ``validate`` drops every ref outside the pool (rejected, not yet
accepted or unknown markets; logged with the reason), keeps only catalog rules (anything else becomes a note that is
shown but never executed) and computes the real history of each proposal with the chain engine (trading days, gaps,
liquidity).
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy.orm import Session

from backend.app.agent.assistant import AgentError, _text_of
from backend.app.agent.pool import Candidate, score
from backend.app.agent.topic_pool import TopicPool, ref_reason, topic_pool
from backend.app.manufacture.catalog import tokenize
from backend.app.metrics import chain_rules
from backend.app.metrics.catalog import ChainLink
from backend.app.metrics.chain import history_summary, link_series, load_prices
from backend.app.risk.models import RiskTopic

logger = logging.getLogger(__name__)

CREATOR_MODEL = "claude-sonnet-5-5"  # override with AGENT_CREATOR_MODEL
MAX_PROPOSALS = 8
MAX_LINKS = 40
CONTEXT_CHAINS = 40
CONTEXT_MARKETS = 80
MAX_TEXT = 600

SYSTEM_PROMPT = """You are the metric builder of a prediction-market risk index platform.
You see only the markets a human accepted in the review list of one risk topic (question given), each with its reading,
plus detected chains among them (successive markets of one recurring question, ids "chain:..."). Propose metrics:
- kind "single" (one market id "source:external_id") or "chain" (a chain id or several market ids of the same question).
- The reading of every market is fixed by the review list; you cannot change it.
- rules: only keys of the rule catalog. Anything else (other thresholds, weights, smoothing ...) goes to free_rules;
  the app shows it as a note and never executes it.
- Prefer long histories (at least 180 trading days) and liquid markets; only chain what is really comparable.
- If the accepted markets do not fit the idea, propose nothing and say so.
Use only ids from the context. Answer in the user's language, concisely."""

_STRINGS = {"type": "array", "items": {"type": "string"}}
PROPOSAL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "kind", "refs", "rules", "free_rules", "reason"],
                   "properties": {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["single", "chain"]}, "refs": _STRINGS,
                                  "rules": _STRINGS, "free_rules": _STRINGS, "reason": {"type": "string"}}}
OUTPUT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reply", "proposals"],
                 "properties": {"reply": {"type": "string"}, "proposals": {"type": "array", "items": PROPOSAL_SCHEMA}}}


@dataclass(frozen=True)
class CreatorResult:
    reply: str
    proposals: list[dict]
    dropped: list[dict]
    model: str


class Creator(Protocol):
    name: str

    def propose(self, idea: str, context: dict) -> dict: ...


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _text(value, limit: int = MAX_TEXT) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _label(reading: str) -> str:
    return chain_rules.READINGS[chain_rules.reading(reading)]["label"]


def context(pool: TopicPool, idea: str) -> dict:
    """Compact model context: topic, chains and unchained accepted markets ranked by idea match and history, rule catalog."""
    terms = tokenize(idea)
    chained = {l.market.key for ch in pool.chains for l in ch.links}
    chains = sorted(pool.chains, key=lambda ch: (-score(ch.label, (), terms), -(ch.rolls > 0), -ch.history_days))[:CONTEXT_CHAINS]
    singles = sorted((c for c in pool.candidates if c.key not in chained), key=lambda c: (-score(c.title, (), terms), -c.days, -c.avg_volume))
    return {
        "topic": {"name": pool.topic["name"], "question": pool.topic["question"]},
        "chains": [{"id": ch.key, "question": ch.label, "links": len(ch.links), "rolls": ch.rolls, "history_days": ch.history_days,
                    "gap_days": sum(g["days"] for g in ch.gaps), "readings": sorted({pool.readings[l.market.key] for l in ch.links}),
                    "example_titles": [l.market.title for l in ch.links[-2:]]} for ch in chains],
        "markets": [{"id": c.key, "title": c.title, "reading": pool.readings[c.key], "days": c.days, "first_date": _iso(c.first_date),
                     "last_date": _iso(c.last_date), "end_date": _iso(c.end_date), "avg_volume_24h_usd": round(c.avg_volume)}
                    for c in singles[:CONTEXT_MARKETS]],
        "rule_catalog": [{"key": r["key"], "rule": r["text"]} for r in chain_rules.payload()],
        "readings": [{"reading": r["key"], "meaning": r["text"]} for r in chain_rules.readings_payload()],
    }


def prompt(idea: str, ctx: dict) -> str:
    return f"<idea>\n{idea}\n</idea>\n\n<context>\n{json.dumps(ctx, ensure_ascii=False)}\n</context>"


# ---- validation -------------------------------------------------------------------------------

def roll_day(c: Candidate, kind: str) -> date | None:
    """End date of a link (chains fall back to the last stored day, like ``chaining``; a single market without end date has none)."""
    return c.end_date or (c.last_date if kind == "chain" else None)


def build_links(cands: list[Candidate], pool: TopicPool, kind: str) -> list[ChainLink]:
    """Links with the reading of the review list (never the model's or the page's)."""
    return [ChainLink(c.source, c.external_id, c.title, roll_day(c, kind), pool.readings[c.key]) for c in cands]


def assess(session: Session, links: list[ChainLink], rules: list[str], today: date) -> dict:
    """History check of a proposal on the raw data: trading days, gaps, liquidity (warning below 180 trading days)."""
    prices, violations = load_prices(session, links, today)
    summary = history_summary(link_series(links, rules, prices))
    return summary | {"pit_violations": len(violations)}


def link_payload(link: ChainLink) -> dict:
    return {"source": link.source, "external_id": link.external_id, "title": link.title, "roll_on": _iso(link.roll_on),
            "direction": link.direction, "reading_label": _label(link.direction)}


def proposal_payload(session: Session, pool: TopicPool, name: str, kind: str, links: list[ChainLink], rules: list[str], notes: list[str],
                     reason: str, today: date) -> dict:
    return {"name": name, "kind": kind, "topic_id": pool.topic["id"], "topic": pool.topic["name"], "direction": links[0].direction,
            "reading_label": ", ".join(dict.fromkeys(_label(l.direction) for l in links)), "rules": rules, "notes": notes, "reason": reason,
            "links": [link_payload(l) for l in links], "rule_labels": [chain_rules.RULES[k]["label"] for k in rules],
            "assessment": assess(session, links, rules, today)}


def _dropped(session: Session, pool: TopicPool, name: str, refs: list[str]) -> list[dict]:
    return [{"proposal": name, "ref": r, "reason": ref_reason(session, pool.topic["id"], r)} for r in refs]


def resolve_proposal(session: Session, raw: dict, pool: TopicPool, today: date, context_label: str = "") -> tuple[dict | None, list[str]]:
    """(validated proposal or None, refs outside the pool); refs are chain ids or market keys of the topic pool."""
    chains, by_key = {c.key: c for c in pool.chains}, pool.by_key
    cands, unknown = [], []
    for ref in raw.get("refs") or []:
        ref = str(ref).strip()
        found = [l.market for l in chains[ref].links] if ref in chains else [by_key[ref]] if ref in by_key else None
        if found is None:
            unknown.append(ref[:120])
        cands += [c for c in found or [] if c.key not in {x.key for x in cands}]
    if not cands:
        return None, unknown
    cands = cands[:MAX_LINKS]
    kind = "chain" if len(cands) > 1 else "single"
    rules, notes = chain_rules.split(raw.get("rules"), raw.get("free_rules"), kind, context_label)
    name = _text(raw.get("name"), 128) or cands[0].title[:128]
    return proposal_payload(session, pool, name, kind, build_links(cands, pool, kind), rules, notes, _text(raw.get("reason")), today), unknown


def revalidate(session: Session, topic: RiskTopic, raw: dict, today: date) -> tuple[dict | None, list[dict]]:
    """Proposal sent back by the page for "Als Metrik-Entwurf übernehmen": every link must still be accepted in the topic's review
    list and have stored prices; reading and end date come from the server, rules are split again."""
    wanted = [(str(l.get("source") or ""), str(l.get("external_id") or "")) for l in raw.get("links") or [] if isinstance(l, dict)]
    pool = topic_pool(session, topic, set(wanted))
    by_key = pool.by_key
    cands, unknown = [], []
    for source, eid in wanted[:MAX_LINKS]:
        c = by_key.get(f"{source}:{eid}")
        if c is None or not c.days:
            unknown.append(f"{source}:{eid}"[:120])
        elif c.key not in {x.key for x in cands}:
            cands.append(c)
    name = _text(raw.get("name"), 128)
    dropped = _dropped(session, pool, name, unknown)
    if dropped:
        logger.warning("creator accept %r (topic %r): links outside the accepted review list dropped: %s", name, topic.name,
                       [(d["ref"], d["reason"]) for d in dropped])
    if not cands:
        return None, dropped
    kind = "chain" if len(cands) > 1 else "single"
    rules, notes = chain_rules.split(raw.get("rules"), raw.get("notes"), kind, f"accept {name!r}")
    proposal = proposal_payload(session, pool, name or cands[0].title[:128], kind, build_links(cands, pool, kind), rules, notes,
                                _text(raw.get("reason")), today)
    return proposal, dropped


def validate(session: Session, parsed, pool: TopicPool, model: str, today: date) -> CreatorResult:
    """Only accepted markets of the topic and catalog rules survive; every other ref is dropped with its reason."""
    if not isinstance(parsed, dict):
        raise AgentError("Antwort des Modells hatte ein unerwartetes Format.")
    reply = _text(parsed.get("reply"), 2000)
    proposals, dropped = [], []
    for i, raw in enumerate([p for p in parsed.get("proposals") or [] if isinstance(p, dict)][:MAX_PROPOSALS]):
        name = _text(raw.get("name"), 128)
        proposal, unknown = resolve_proposal(session, raw, pool, today, f"proposal {i + 1} ({model})")
        if unknown:
            gone = _dropped(session, pool, name, unknown)
            logger.warning("creator (%s) proposal %r: refs outside the accepted markets of topic %r dropped: %s", model, name,
                           pool.topic["name"], [(d["ref"], d["reason"]) for d in gone])
            dropped += gone
        if proposal is None:
            logger.warning("creator (%s) proposal %r discarded: no accepted market", model, name)
            continue
        proposals.append(proposal)
    logger.info("creator (%s) topic %r: %d proposal(s) %s, %d dropped ref(s)", model, pool.topic["name"], len(proposals),
                [(p["name"], p["kind"], [l["direction"] for l in p["links"]], p["assessment"]["trading_days"], p["rules"], len(p["notes"]))
                 for p in proposals], len(dropped))
    return CreatorResult(reply, proposals, dropped, model)


def create(session: Session, creator: Creator, topic: RiskTopic, idea: str, today: date) -> tuple[CreatorResult, TopicPool]:
    pool = topic_pool(session, topic)
    if not pool.candidates:
        logger.warning("creator request for topic %r refused: no accepted market in the review list", topic.name)
        return CreatorResult("Im Thema „" + topic.name + "“ ist noch kein Markt angenommen – nimm zuerst Märkte in der Prüfliste an.",
                             [], [], creator.name), pool
    ctx = context(pool, idea)
    logger.info("creator request (%s) topic %r: %r with %d chains, %d markets in context", creator.name, topic.name, idea[:200],
                len(ctx["chains"]), len(ctx["markets"]))
    return validate(session, creator.propose(idea, ctx), pool, creator.name, today), pool


# ---- model backends ----------------------------------------------------------------------------

class ApiCreator:
    def __init__(self, client=None, model: str = CREATOR_MODEL):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client, self.name = client, model

    def propose(self, idea: str, ctx: dict) -> dict:
        try:
            response = self._client.messages.create(
                model=self.name, max_tokens=8192, system=SYSTEM_PROMPT, messages=[{"role": "user", "content": prompt(idea, ctx)}],
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}, "effort": "medium"})
        except Exception as exc:
            logger.warning("creator call failed: %s", exc)
            raise AgentError(f"Modellaufruf fehlgeschlagen: {exc}") from exc
        if response.stop_reason != "end_turn":
            raise AgentError(f"Antwort unvollständig (stop_reason={response.stop_reason}).")
        try:
            return json.loads(_text_of(response))
        except ValueError as exc:
            raise AgentError("Antwort des Modells war kein gültiges JSON.") from exc


class CliCreator:
    def __init__(self, cli_agent, model: str = CREATOR_MODEL):
        self._cli, self._model, self.name = cli_agent, model, model + " (Konsole)"

    def propose(self, idea: str, ctx: dict) -> dict:
        return self._cli.structured(self._model, SYSTEM_PROMPT, prompt(idea, ctx), OUTPUT_SCHEMA, effort="medium")


class RuleCreator:
    """Keyword stand-in: matching chains (with rolls first) and long single markets among the accepted markets."""

    name = "rule-fallback"

    def propose(self, idea: str, ctx: dict) -> dict:
        terms = tokenize(idea)
        chains = [c for c in ctx["chains"] if score(c["question"], (), terms)][:3]
        singles = [m for m in ctx["markets"] if score(m["title"], (), terms)][:2]
        proposals = [{"name": c["question"][:128], "kind": "chain", "refs": [c["id"]],
                      "rules": [chain_rules.FRONT_CHAIN] if not c["rolls"] else [], "free_rules": [], "reason": "Stichwort-Treffer"} for c in chains]
        proposals += [{"name": m["title"][:128], "kind": "single", "refs": [m["id"]], "rules": [], "free_rules": [],
                       "reason": "Stichwort-Treffer"} for m in singles]
        return {"reply": f"Kein KI-Modell verfügbar: Stichwortsuche in den angenommenen Märkten, {len(proposals)} Vorschläge." if proposals else
                "Kein KI-Modell verfügbar, und kein angenommener Markt passt zur Idee.", "proposals": proposals}


def creator_from_env() -> Creator:
    """Same backend choice as the index agent (AGENT_BACKEND=api|cli|rule; default API key -> console -> rules)."""
    from backend.app.agent.cli_agent import ClaudeCliAgent, find_cli

    backend = os.environ.get("AGENT_BACKEND", "").strip().lower()
    model = os.environ.get("AGENT_CREATOR_MODEL", CREATOR_MODEL)
    if backend == "api" or (not backend and os.environ.get("ANTHROPIC_API_KEY")):
        return ApiCreator(model=model)
    cli = find_cli() if backend in ("", "cli") else None
    if cli:
        return CliCreator(ClaudeCliAgent(cli, model=model, terms_model=model), model)
    logger.warning("metric creator without language model (backend=%r): keyword fallback", backend or "auto")
    return RuleCreator()
