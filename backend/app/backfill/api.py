"""Backfill endpoints.

GET  /history/status                    -> how far the daily snapshots reach back
POST /indices/{name}/history/backfill   -> load past days of the index markets (?days, ?limit);
                                           an optional RuleSpec body uses unsaved rules
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.backfill.service import BACKFILL_DAYS, DEFAULT_LIMIT, MAX_LIMIT, backfill_index, history_status
from backend.app.deps import get_session
from backend.app.engine import build_rule, get_rule
from backend.app.history import MAX_DAYS
from backend.app.schemas import RuleSpec

logger = logging.getLogger(__name__)
router = APIRouter(tags=["backfill"])


def get_history_fetcher():
    """Price-history source as a dependency (None: the raw data layer) so tests never reach the network."""
    return None


@router.get("/history/status")
def status(session=Depends(get_session)) -> dict:
    return history_status(session)


@router.post("/indices/{index_name}/history/backfill")
def backfill(index_name: str, body: RuleSpec | None = None, days: int = Query(BACKFILL_DAYS, ge=1, le=MAX_DAYS),
             limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), session=Depends(get_session),
             fetcher=Depends(get_history_fetcher)) -> dict:
    rule = build_rule(index_name, **body.validated()) if body is not None else get_rule(session, index_name)
    if rule is None:
        raise HTTPException(404, f"unknown index {index_name!r}")
    logger.info("backfill requested for %r (days=%d, limit=%d, unsaved rule=%s)", index_name, days, limit, body is not None)
    return backfill_index(session, rule, days, limit, fetcher)
