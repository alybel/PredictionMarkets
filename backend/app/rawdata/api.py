"""Raw data layer endpoints: the one place where refresh requests enter.

POST /raw/requests                  -> request a refresh ({"job": "directory", "trigger": "workshop"}; kind "markets" refreshes the
                                       dashboard population; the other process kinds come from their processes); returns at once (202)
                                       with the job; while one runs: that job with ``duplicate: true``, no second fetch
GET  /raw/requests/latest?job=...   -> latest job of that kind (null before the first), for the progress display
GET  /raw/requests/{id}             -> one job with status, progress and summary; 404 unknown

Getter, staging folder and background mode are dependencies, so tests never reach the network.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import sessionmaker

from backend.app.deps import get_session
from backend.app.rawdata import jobs, providers  # noqa: F401  (providers registers the process kinds)
from backend.app.rawdata.streams import http_get_raw

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/raw", tags=["raw"])


class RefreshBody(BaseModel):
    job: str = Field(..., min_length=1, max_length=32)
    trigger: str = Field("manual", pattern=r"^[a-z0-9_-]{1,16}$")


def get_raw_getter():
    return http_get_raw


def get_raw_staging():
    return None  # default staging folder (DATA_DIR)


def get_raw_background() -> bool:
    return True


@router.post("/requests", status_code=202)
def request_refresh(body: RefreshBody, session=Depends(get_session), get_raw=Depends(get_raw_getter), staging=Depends(get_raw_staging),
                    background: bool = Depends(get_raw_background)) -> dict:
    logger.info("raw refresh requested via API: job %s, trigger %s", body.job, body.trigger)
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    try:
        return jobs.request_refresh(factory, body.job, body.trigger, get_raw=get_raw, staging=staging, background=background)
    except jobs.UnknownJob as exc:
        logger.warning("raw refresh refused (404): %s", exc)
        raise HTTPException(404, str(exc)) from exc


@router.get("/requests/latest")
def latest(job: str = Query(..., min_length=1, max_length=32), session=Depends(get_session)) -> dict | None:
    return jobs.latest_job(session, job)


@router.get("/requests/{job_id}")
def get_request(job_id: int, session=Depends(get_session)) -> dict:
    payload = jobs.get_job(session, job_id)
    if payload is None:
        raise HTTPException(404, f"Rohdaten-Auftrag {job_id} unbekannt")
    return payload
