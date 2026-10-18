"""Event endpoints: markets of the same source event bundled with their outcome markets.

GET /events              -> paged events with outcomes (?q, source, min_outcomes, limit, offset)
GET /events/{event_id}   -> one event with all its outcome markets
POST /events/sync        -> bundle the current market population now (registered in backend.app.api)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.deps import get_session
from backend.app.events.service import PAGE_LIMIT, get_event, list_events

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


@router.get("/events")
def events(q: str = Query("", max_length=100), source: str = Query("", max_length=32), min_outcomes: int = Query(1, ge=1, le=1000),
           limit: int = Query(50, ge=1, le=PAGE_LIMIT), offset: int = Query(0, ge=0), session=Depends(get_session)) -> dict:
    return list_events(session, q=q, source=source, min_outcomes=min_outcomes, limit=limit, offset=offset)


@router.get("/events/{event_id}")
def event(event_id: int, session=Depends(get_session)) -> dict:
    detail = get_event(session, event_id)
    if detail is None:
        logger.warning("event %d requested (404): unknown", event_id)
        raise HTTPException(404, f"unknown event {event_id}")
    return detail
