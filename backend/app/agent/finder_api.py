"""Endpoints of the agent mode "Metriken finden" (tab "KI-Agent").

POST   /agent/find            -> {idea} in; catalog hits (reason, history, checks, status, warnings), gaps, dropped keys out
GET    /agent/bookmarks       -> bookmarked metric keys (shown in the index builder)
POST   /agent/bookmarks       -> {key}: bookmark a catalog metric (201, idempotent; 404 for an unknown key)
DELETE /agent/bookmarks/{key} -> remove the bookmark (204)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from backend.app.agent.assistant import AgentError
from backend.app.agent.finder import MIN_HISTORY_DAYS, catalog_entries, find, finder_from_env
from backend.app.deps import get_session
from backend.app.metrics.models import MetricBookmark
from backend.app.metrics.service import MetricNotFound, definition

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


def get_finder():
    return finder_from_env()


class FindRequest(BaseModel):
    idea: str = Field(min_length=1, max_length=4000)


class BookmarkRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)


def bookmarked_keys(session) -> list[str]:
    return [b.key for b in session.query(MetricBookmark).order_by(MetricBookmark.created_at, MetricBookmark.key)]


@router.post("/find")
def find_metrics(body: FindRequest, session=Depends(get_session), finder=Depends(get_finder)) -> dict:
    idea = body.idea.strip()
    if not idea:
        raise HTTPException(422, "Die Idee darf nicht leer sein.")
    logger.info("metric finder request (%s): %r", finder.name, idea[:200])
    catalog = catalog_entries(session)
    try:
        result = find(finder, idea, catalog)
    except AgentError as exc:
        logger.warning("metric finder failed: %s", exc)
        raise HTTPException(502, str(exc)) from exc
    marks = set(bookmarked_keys(session))
    return {"reply": result.reply, "matches": [m | {"bookmarked": m["key"] in marks} for m in result.matches], "gaps": result.gaps,
            "dropped": result.dropped, "model": result.model, "catalog_size": len(catalog), "min_history_days": MIN_HISTORY_DAYS}


@router.get("/bookmarks")
def list_bookmarks(session=Depends(get_session)) -> dict:
    return {"keys": bookmarked_keys(session)}


@router.post("/bookmarks", status_code=201)
def add_bookmark(body: BookmarkRequest, session=Depends(get_session)) -> dict:
    try:
        definition(session, body.key)
    except MetricNotFound:
        logger.warning("bookmark refused, unknown metric %r", body.key)
        raise HTTPException(404, f"Unbekannte Metrik: {body.key}") from None
    if session.get(MetricBookmark, body.key) is None:
        session.add(MetricBookmark(key=body.key))
        session.commit()
    logger.info("metric %s bookmarked for the index builder", body.key)
    return {"key": body.key, "keys": bookmarked_keys(session)}


@router.delete("/bookmarks/{key}", status_code=204)
def remove_bookmark(key: str, session=Depends(get_session)) -> Response:
    row = session.get(MetricBookmark, key)
    if row is not None:
        session.delete(row)
        session.commit()
    logger.info("metric %s bookmark removed (%s)", key, "existed" if row else "was not bookmarked")
    return Response(status_code=204)
