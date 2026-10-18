"""Risk topic endpoints (tab "Risiko-Themen").

GET   /risk/topics                          -> topics (the pilot "Marktcrash-Risiko" is created on first call)
POST  /risk/topics                          -> new topic {name, question, search_terms, tags}
POST  /risk/topics/{id}/classify            -> classify pre-filtered markets not classified yet for this topic
GET   /risk/topics/{id}/assessments         -> review list (?status, relevant, limit, offset)
PATCH /risk/assessments/{id}                -> accept/reject and/or change the reading {status?, reading?}
GET   /risk/topics/{id}/accepted            -> hand-over: accepted markets with their reading
GET   /risk/topics/{id}/timeline            -> months over >= 3 years with the relevant markets existing then

The classifier is a dependency, so tests never call a language model.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.app.deps import get_session
from backend.app.risk import service
from backend.app.risk.classify import classifier_from_env
from backend.app.risk.models import STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/risk", tags=["risk"])


class TopicBody(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    question: str = Field(min_length=5, max_length=512)
    search_terms: list[str] = Field(default_factory=list, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=50)


class ReviewBody(BaseModel):
    status: str | None = None
    reading: str | None = None


def get_risk_classifier():
    return classifier_from_env()


def _topic(session, topic_id: int):
    try:
        return service.get_topic(session, topic_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/topics")
def topics(session=Depends(get_session)) -> list[dict]:
    return service.list_topics(session)


@router.post("/topics")
def create_topic(body: TopicBody, session=Depends(get_session)) -> dict:
    logger.info("risk topic create requested: %r", body.name)
    try:
        return service.topic_payload(service.create_topic(session, body.name, body.question, body.search_terms, body.tags))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/topics/{topic_id}/classify")
def classify(topic_id: int, session=Depends(get_session), classifier=Depends(get_risk_classifier)) -> dict:
    topic = _topic(session, topic_id)
    logger.info("risk classification requested via API for topic %r", topic.name)
    return service.classify_topic(session, topic, classifier)


@router.get("/topics/{topic_id}/assessments")
def assessments(topic_id: int, status: str = Query("", pattern="^(|" + "|".join(STATUSES) + ")$"), relevant: bool | None = None,
                limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), session=Depends(get_session)) -> dict:
    return service.list_assessments(session, _topic(session, topic_id), status, relevant, limit, offset)


@router.patch("/assessments/{assessment_id}")
def review(assessment_id: int, body: ReviewBody, session=Depends(get_session)) -> dict:
    try:
        return service.review(session, assessment_id, body.status, body.reading)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/topics/{topic_id}/accepted")
def accepted(topic_id: int, session=Depends(get_session)) -> list[dict]:
    return service.accepted_markets(session, _topic(session, topic_id))


@router.get("/topics/{topic_id}/timeline")
def timeline(topic_id: int, session=Depends(get_session)) -> dict:
    return service.timeline(session, _topic(session, topic_id))
