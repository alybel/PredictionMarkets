"""Endpoints of the index agent (tab "KI-Agent").

POST /agent/blueprint  -> chat turn: topic + transcript + current blueprint in, reply + updated blueprint + detected chains out
                          (short stored histories are first extended from the source, see history_check)
POST /agent/metric     -> register the blueprint's markets (all chain links included) as metric draft in tab "Metriken"

Since 1017-1107 (Risiko-Index 4/7) the blueprint lives with the topic review list: its pool holds only the markets
accepted for the chosen topic (live state from the dashboard cache, history from the stored daily snapshots), and the
metric draft keeps only those. Agent and session are dependencies so tests run without network.
"""

import logging
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.agent import pool as agent_pool
from backend.app.agent.assistant import AgentError, agent_from_env, user_text
from backend.app.agent.blueprint import calculation_text, pinned_keys, resolve
from backend.app.agent.chaining import build_chains
from backend.app.agent.history_check import extend_history
from backend.app.agent.topic_pool import topic_pool
from backend.app.backfill.api import get_history_fetcher
from backend.app.deps import get_session
from backend.app.metrics.service import create_custom
from backend.app.risk.service import get_topic

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])

MAX_MESSAGES = 40
CHAIN_PAYLOAD_LIMIT = 30


def get_agent_markets() -> list:
    """The dashboard's cached market population (imported lazily: the main module includes this router)."""
    from backend.app import api as main_api

    return main_api.market_cache.get()


def get_agent():
    return agent_from_env()


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class BlueprintRequest(BaseModel):
    topic_id: int
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    blueprint: dict | None = None
    check_history: bool = True  # look up short histories at the source before answering


class MetricRequest(BaseModel):
    topic_id: int
    blueprint: dict


def _topic(session, topic_id: int):
    try:
        return get_topic(session, topic_id)
    except LookupError as exc:
        logger.warning("agent: topic %s not found", topic_id)
        raise HTTPException(404, str(exc)) from exc


def accepted_candidates(session, topic, markets) -> list:
    """Accepted markets of the topic's review list, merged with their live state (price, event) from the cache."""
    stored = {c.key: c for c in topic_pool(session, topic).candidates}
    for c in agent_pool.live(markets, [], set(stored)):
        old = stored[c.key]
        stored[c.key] = replace(old, price=c.price, live=True, event_key=c.event_key, end_date=old.end_date or c.end_date)
    return list(stored.values())


@router.post("/blueprint")
def blueprint(body: BlueprintRequest, session=Depends(get_session), markets=Depends(get_agent_markets), agent=Depends(get_agent),
              fetcher=Depends(get_history_fetcher)) -> dict:
    if body.messages[-1].role != "user":
        raise HTTPException(400, "the last message must come from the user")
    topic = _topic(session, body.topic_id)
    messages = [m.model_dump() for m in body.messages]
    previous = body.blueprint or {}
    terms = list(dict.fromkeys(agent.terms(user_text(messages)) + [t.lower() for t in previous.get("search_terms") or []]))
    candidates = accepted_candidates(session, topic, markets)
    accepted = [c.key for c in candidates]  # every accepted market is relevant for the history check
    check = extend_history(session, candidates, markets, terms, fetcher, accepted) if body.check_history and candidates else None
    if check and check["rows_added"]:
        candidates = accepted_candidates(session, topic, markets)
    chains = build_chains(candidates)
    current = resolve(previous, chains, candidates) if previous else {}
    outside = sorted(set(pinned_keys(previous)) - set(accepted))
    if outside:
        logger.warning("agent blueprint (topic %r): markets outside the accepted review list dropped: %s", topic.name, outside)
    logger.info("agent turn %d with %s, topic %r: %d terms, %d accepted candidates, %d chains", len(messages), agent.name, topic.name,
                len(terms), len(candidates), len(chains))
    try:
        result = agent.respond(messages, current, chains, candidates)
    except AgentError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"reply": result.reply, "blueprint": result.blueprint, "model": result.model, "terms": terms, "topic": {"id": topic.id, "name": topic.name},
            "candidates": len(candidates), "chains": [c.payload() for c in chains[:CHAIN_PAYLOAD_LIMIT]], "chain_total": len(chains),
            "history_check": check}


@router.post("/metric", status_code=201)
def metric(body: MetricRequest, session=Depends(get_session)) -> dict:
    bp = body.blueprint
    topic = _topic(session, body.topic_id)
    name = str(bp.get("name") or "").strip()[:128]
    markets = [m for c in bp.get("components") or [] if isinstance(c, dict) for m in c.get("markets") or []
               if isinstance(m, dict) and m.get("source") in ("polymarket", "kalshi") and m.get("external_id")]
    accepted = {c.key for c in topic_pool(session, topic).candidates}
    outside = [f"{m['source']}:{m['external_id']}" for m in markets if f"{m['source']}:{m['external_id']}" not in accepted]
    if outside:
        logger.warning("agent metric draft %r (topic %r): markets outside the accepted review list dropped: %s", name, topic.name, outside)
    markets = [m for m in markets if f"{m['source']}:{m['external_id']}" in accepted]
    logger.info("agent metric draft requested: %r (topic %r) with %d markets", name, topic.name, len(markets))
    if not name or not markets:
        logger.warning("agent metric draft refused (400): name=%r markets=%d", name, len(markets))
        raise HTTPException(400, "Der Entwurf braucht einen Namen und mindestens einen Markt.")
    refs = [{"source": m["source"], "external_id": str(m["external_id"]), "title": str(m.get("title") or "")[:500]} for m in markets]
    key, state = create_custom(session, name, calculation_text(bp), refs)
    return {"key": key, "name": name, "status": state.status, "markets": len({(m["source"], m["external_id"]) for m in refs})}
