"""Domain endpoints (tab "Domänen").

GET    /domains            -> all domains with member counts (active/expired), history reach, gaps, last load run
POST   /domains            -> create (name, topic, subtopic, terms); 400 on empty name or unknown topic, 409 on a duplicate name
GET    /domains/{id}       -> one domain
PUT    /domains/{id}       -> change the definition (same validation)
DELETE /domains/{id}       -> remove definition and member list (204); stored history stays
POST   /domains/{id}/load  -> load run now: find markets (open and expired) at both sources, store their full history

Topics come from the topic tree of the cached population; scanner and history source are dependencies,
so tests never reach the network.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from backend.app.backfill.api import get_history_fetcher
from backend.app.deps import get_session
from backend.app.domains import service
from backend.app.events.tree import build_tree

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/domains", tags=["domains"])


class DomainBody(BaseModel):
    name: str = Field("", max_length=200)
    topic: str = Field("", max_length=200)
    subtopic: str | None = Field(None, max_length=200)
    terms: list[str] = Field(default_factory=list, max_length=50)


def get_topic_tree() -> list[dict]:
    """Topic tree of the dashboard's cached population (imported lazily: the main module includes this router)."""
    from backend.app import api as main_api

    return build_tree(main_api.market_cache.get())


def get_topic_source():
    """Callable delivering the topic tree; only invoked when a topic must be checked."""
    return get_topic_tree


def get_domain_scanner():
    """None: the load run asks the raw data layer (tests inject a fake scanner)."""
    return None


def _found(session, domain_id: int):
    domain = service.get_domain(session, domain_id)
    if domain is None:
        logger.warning("domain %d requested (404): unknown", domain_id)
        raise HTTPException(404, f"unknown domain {domain_id}")
    return domain


def _save(session, body: DomainBody, topic_tree, domain=None) -> dict:
    try:
        definition = service.clean_definition(body.name, body.topic, body.subtopic, body.terms)
        unchanged = domain is not None and (domain.topic, domain.subtopic) == (definition["topic"], definition["subtopic"])
        if not unchanged:
            service.check_topic(topic_tree(), definition["topic"], definition["subtopic"])
        saved = service.save_domain(session, definition, domain)
    except service.DomainError as exc:
        logger.warning("domain save refused (400/409): %s", exc)
        raise HTTPException(409 if "existiert bereits" in str(exc) else 400, str(exc)) from exc
    return service.payload(session, saved)


@router.get("")
def domains(session=Depends(get_session)) -> list[dict]:
    return service.list_domains(session)


@router.post("", status_code=201)
def create(body: DomainBody, session=Depends(get_session), tree=Depends(get_topic_source)) -> dict:
    logger.info("domain create requested: %r", body.model_dump())
    return _save(session, body, tree)


@router.get("/{domain_id}")
def one(domain_id: int, session=Depends(get_session)) -> dict:
    return service.payload(session, _found(session, domain_id))


@router.put("/{domain_id}")
def update(domain_id: int, body: DomainBody, session=Depends(get_session), tree=Depends(get_topic_source)) -> dict:
    logger.info("domain %d update requested: %r", domain_id, body.model_dump())
    return _save(session, body, tree, _found(session, domain_id))


@router.delete("/{domain_id}", status_code=204)
def remove(domain_id: int, session=Depends(get_session)) -> Response:
    service.delete_domain(session, _found(session, domain_id))
    return Response(status_code=204)


@router.post("/{domain_id}/load")
def load(domain_id: int, session=Depends(get_session), scanner=Depends(get_domain_scanner), fetcher=Depends(get_history_fetcher)) -> dict:
    domain = _found(session, domain_id)
    summary = service.load_domain(session, domain, scanner, fetcher)
    return {**service.payload(session, domain), "run": summary}
