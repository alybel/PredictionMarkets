"""Market directory endpoints (tab "Marktverzeichnis").

GET  /directory/markets          -> paged directory (?q, source, status, limit, offset, group = key of one group market)
GET  /directory/groups           -> paged group markets: every market group (source event) as one row (same filters)
GET  /directory/inbox            -> new markets not reviewed yet (?since=ISO date)
POST /directory/inbox/reviewed   -> mark markets as reviewed ({"ids": [...]}; without ids: all)
GET  /directory/coverage         -> markets existing per year and source, status counts
GET  /directory/runs/last        -> summary of the last load run (null before the first run)
POST /directory/load             -> refresh request to the raw data layer, waited for, then the rebuilt directory (run summary)
POST /directory/rebuild          -> build from the stored raw data only (?from_start=true: re-read every raw answer)

The directory never fetches by itself; getter and staging folder are the raw layer's dependencies (tests: no network).
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.app.deps import get_session
from backend.app.directory import groups, service
from backend.app.directory.build import build_directory
from backend.app.directory.models import STATUSES
from backend.app.quality.pit import as_utc
from backend.app.rawdata.api import get_raw_getter, get_raw_staging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/directory", tags=["directory"])


class ReviewBody(BaseModel):
    ids: list[int] | None = Field(None, max_length=service.PAGE_LIMIT)


get_directory_getter, get_directory_staging = get_raw_getter, get_raw_staging  # same objects: one override serves both routers


@router.get("/markets")
def markets(q: str = Query("", max_length=100), source: str = Query("", pattern="^(|polymarket|kalshi)$"),
            status: str = Query("", pattern="^(|" + "|".join(STATUSES) + ")$"), limit: int = Query(50, ge=1, le=service.PAGE_LIMIT),
            offset: int = Query(0, ge=0), group: str = Query("", max_length=140), session=Depends(get_session)) -> dict:
    return service.list_markets(session, q=q.strip(), source=source, status=status, limit=limit, offset=offset, group=group)


@router.get("/groups")
def market_groups(q: str = Query("", max_length=100), source: str = Query("", pattern="^(|polymarket|kalshi)$"),
                  status: str = Query("", pattern="^(|" + "|".join(STATUSES) + ")$"), limit: int = Query(50, ge=1, le=service.PAGE_LIMIT),
                  offset: int = Query(0, ge=0), session=Depends(get_session)) -> dict:
    result = groups.list_groups(session, q=q.strip(), source=source, status=status, limit=limit, offset=offset)
    logger.info("directory groups listed (q %r, source %s, status %s, offset %d): %d of %d group markets", q, source or "-", status or "-",
                offset, len(result["groups"]), result["total"])
    return result


@router.get("/inbox")
def inbox(since: str = Query("", max_length=40), session=Depends(get_session)) -> dict:
    try:
        moment = as_utc(datetime.fromisoformat(since)) if since else None
    except ValueError as exc:
        raise HTTPException(400, f"since ist kein Datum: {since!r}") from exc
    result = service.inbox(session, moment)
    logger.info("directory inbox opened (since %s): %d new markets", since or "-", result["count"])
    return result


@router.post("/inbox/reviewed")
def reviewed(body: ReviewBody, session=Depends(get_session)) -> dict:
    marked = service.mark_reviewed(session, body.ids)
    logger.info("directory inbox: %d markets marked as reviewed (%s)", marked, "all" if body.ids is None else body.ids)
    return {"marked": marked}


@router.get("/coverage")
def coverage(session=Depends(get_session)) -> dict:
    return service.coverage(session)


@router.get("/runs/last")
def last_run(session=Depends(get_session)) -> dict | None:
    return service.last_run(session)


@router.post("/load")
def load(session=Depends(get_session), get_raw=Depends(get_directory_getter), staging=Depends(get_directory_staging)) -> dict:
    logger.info("directory load requested via API")
    try:
        return service.run_directory(session, get_raw, staging)
    except RuntimeError as exc:
        logger.error("directory load failed: %s", exc)
        raise HTTPException(502, str(exc)) from exc


@router.post("/rebuild")
def rebuild(from_start: bool = False, session=Depends(get_session)) -> dict:
    logger.info("directory rebuild from raw data requested via API (from_start %s)", from_start)
    return build_directory(session, "rebuild", from_start=from_start)
