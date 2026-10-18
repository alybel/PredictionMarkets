"""Endpoints of the mode "Metriken anlegen" (proposals from the topic review list, shown in tab "Risiko-Themen").

POST /agent/propose          -> {topic_id, idea, gaps} in; proposals only from the markets accepted in that topic's review
                                list, with their reading (single market or chain, catalog rules, notes that are not executed,
                                history check: trading days, gaps, liquidity) and the dropped refs with their reason
POST /agent/proposals/accept -> {proposal}: links checked again against the accepted review list, stored structured as metric
                                draft (kind chain/single, links with end date and reading, rules, notes); the checks run once
GET  /agent/rules            -> the fixed rule catalog

The former direct route from the domain raw data (1016-2013 step 2) is replaced by the review list (1017-1107, 4/7).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.agent.assistant import AgentError
from backend.app.agent.creator import create, creator_from_env, revalidate
from backend.app.deps import get_session
from backend.app.metrics import chain_rules
from backend.app.metrics.chain import MIN_HISTORY_DAYS
from backend.app.metrics.service import create_structured
from backend.app.models import utcnow
from backend.app.risk.service import get_topic

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


def get_creator():
    return creator_from_env()


class ProposeRequest(BaseModel):
    topic_id: int
    idea: str = Field(min_length=1, max_length=4000)
    gaps: list[str] = Field(default_factory=list, max_length=20)


class AcceptRequest(BaseModel):
    proposal: dict


def _topic(session, topic_id):
    try:
        return get_topic(session, int(topic_id))
    except (LookupError, TypeError, ValueError) as exc:
        logger.warning("metric proposal: topic %r not found", topic_id)
        raise HTTPException(404, f"Thema {topic_id} nicht gefunden") from exc


@router.get("/rules")
def rules() -> dict:
    return {"rules": chain_rules.payload(), "readings": chain_rules.readings_payload(), "always": list(chain_rules.ALWAYS), "chain_always": list(chain_rules.CHAIN_ALWAYS)}


@router.post("/propose")
def propose(body: ProposeRequest, session=Depends(get_session), creator=Depends(get_creator)) -> dict:
    idea = body.idea.strip()
    if not idea:
        raise HTTPException(422, "Die Idee darf nicht leer sein.")
    topic = _topic(session, body.topic_id)
    gaps = [g.strip()[:200] for g in body.gaps if g.strip()]
    text = idea + ("\nNicht abgedeckt:\n" + "\n".join("- " + g for g in gaps) if gaps else "")
    logger.info("metric proposal request (%s) topic %r: %r, %d gap(s)", creator.name, topic.name, idea[:200], len(gaps))
    try:
        result, pool = create(session, creator, topic, text, utcnow().date())
    except AgentError as exc:
        logger.warning("metric proposal failed: %s", exc)
        raise HTTPException(502, str(exc)) from exc
    return {"reply": result.reply, "model": result.model, "topic": pool.topic, "proposals": result.proposals, "dropped": result.dropped,
            "pool": {"markets": len(pool.candidates), "chains": len(pool.chains)}, "min_history_days": MIN_HISTORY_DAYS,
            "rule_catalog": chain_rules.payload(), "readings": chain_rules.readings_payload()}


@router.post("/proposals/accept", status_code=201)
def accept(body: AcceptRequest, session=Depends(get_session)) -> dict:
    raw = body.proposal
    logger.info("metric proposal accept clicked: %r (topic %r) with %d link(s)", raw.get("name"), raw.get("topic_id"), len(raw.get("links") or []))
    topic = _topic(session, raw.get("topic_id"))
    today = utcnow().date()
    proposal, dropped = revalidate(session, topic, raw, today)
    if proposal is None:
        logger.warning("metric proposal accept refused (400): no link accepted in the review list of %r (%s)", topic.name, dropped)
        raise HTTPException(400, "Kein Glied des Vorschlags ist in der Prüfliste des Themas angenommen.")
    spec = {"links": proposal["links"], "rules": proposal["rules"], "notes": proposal["notes"],
            "topic": {"id": topic.id, "name": topic.name}, "assessment": proposal["assessment"]}
    key, state, ev = create_structured(session, proposal["name"], proposal["kind"], spec, proposal["reason"], today)
    logger.info("metric proposal accepted as %s (%s, topic %r, %d links, readings %s, status %s, checks %s)", key, proposal["kind"], topic.name,
                len(proposal["links"]), [l["direction"] for l in proposal["links"]], state.status, {c["key"]: c["status"] for c in ev.checks})
    return {"key": key, "name": proposal["name"], "kind": proposal["kind"], "status": state.status, "checks": ev.checks,
            "rules": proposal["rules"], "notes": proposal["notes"], "links": proposal["links"], "dropped": dropped,
            "assessment": proposal["assessment"]}
