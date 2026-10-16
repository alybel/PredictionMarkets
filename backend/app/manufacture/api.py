"""REST endpoints of the Index Manufacture.

GET    /manufacture/catalog                 -> searchable catalog page (?q, source, tag (repeatable), min_liquidity,
                                               min_price/max_price (default 0.03/0.97), sort, desc, limit, offset,
                                               refresh=1 bypasses the market cache)
GET    /manufacture/catalog/{source}/{id}   -> one market with its full description
POST   /manufacture/assist                  -> chat turn: transcript + draft in, reply + updated draft out
GET    /manufacture/drafts                  -> stored drafts (newest first)
POST   /manufacture/drafts                  -> save a draft (201)
GET    /manufacture/drafts/{id}             -> one draft with transcript
PUT    /manufacture/drafts/{id}             -> replace a draft
DELETE /manufacture/drafts/{id}             -> remove a draft
POST   /manufacture/drafts/{id}/metric      -> create a metric draft (status draft) from the draft's markets
                                               (400 without markets, 409 if already created)

Markets come from the same server-side cache as the dashboard; the
assistant and the session are dependencies so tests run without network.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.app.deps import get_session
from backend.app.manufacture.assistant import CANDIDATE_LIMIT, AssistantError, assistant_from_env, empty_draft, normalize_draft
from backend.app.manufacture.catalog import build_catalog, by_key, entry_payload, search, shortlist
from backend.app.manufacture.models import DRAFT_STATUS_DRAFT, DRAFT_STATUS_METRIC, IndexDraft
from backend.app.metrics.service import MetricExists, create_basket
from backend.app.models import DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE, MIN_LIQUIDITY_USD

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/manufacture", tags=["manufacture"])

MAX_MESSAGES = 40


def get_catalog_markets(refresh: bool = False) -> list:
    """The dashboard's cached market population (imported lazily: the main module includes this router)."""
    from backend.app import api as main_api

    return main_api.market_cache.get(force=refresh)


def get_assistant():
    return assistant_from_env()


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class AssistRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    draft: dict | None = None


class DraftBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    intent: str = Field(default="", max_length=2000)
    description: str = Field(default="", max_length=4000)
    methodology: str = Field(default="", max_length=4000)
    keywords: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    min_liquidity: float = MIN_LIQUIDITY_USD
    markets: list[dict] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=MAX_MESSAGES)
    model: str | None = Field(default=None, max_length=64)


@router.get("/catalog")
def catalog(q: str = "", source: str = "", tag: list[str] = Query(default=[]), min_liquidity: float = 0.0,
            min_price: float = DEFAULT_MIN_PRICE, max_price: float = DEFAULT_MAX_PRICE,
            sort: str = "", desc: bool = True, limit: int = 100, offset: int = 0,
            markets=Depends(get_catalog_markets)) -> dict:
    if not 0 <= min_price < max_price <= 1:
        raise HTTPException(400, "price corridor must satisfy 0 <= min_price < max_price <= 1")
    entries = build_catalog(markets)
    result = search(entries, q=q, source=source, tags=tag, min_liquidity=max(min_liquidity, 0.0),
                    sort=sort, descending=desc, limit=limit, offset=max(offset, 0),
                    min_price=min_price, max_price=max_price)
    logger.info("catalog search q=%r source=%r tags=%s price=%.2f-%.2f sort=%s desc=%s -> %d of %d, %d tag facets",
                q, source, tag, min_price, max_price, result["sort"], desc,
                len(result["items"]), result["total"], len(result["facets"]["tags"]))
    return {**result, "population": len(entries), "min_liquidity_floor": MIN_LIQUIDITY_USD,
            "min_price": min_price, "max_price": max_price,
            "default_min_price": DEFAULT_MIN_PRICE, "default_max_price": DEFAULT_MAX_PRICE}


@router.get("/catalog/{source}/{external_id}")
def catalog_entry(source: str, external_id: str, markets=Depends(get_catalog_markets)) -> dict:
    entry = by_key(build_catalog(markets)).get(f"{source}:{external_id}")
    if entry is None:
        raise HTTPException(404, f"unknown market {source}:{external_id}")
    return entry_payload(entry)


@router.post("/assist")
def assist(body: AssistRequest, markets=Depends(get_catalog_markets), assistant=Depends(get_assistant)) -> dict:
    if body.messages[-1].role != "user":
        raise HTTPException(400, "the last message must come from the user")
    entries = build_catalog(markets)
    draft = normalize_draft(body.draft or {}, entries, body.draft or {}) if body.draft else empty_draft()
    intent = " ".join(m.content for m in body.messages if m.role == "user") + " " + " ".join(draft["keywords"])
    candidates = shortlist(entries, intent, limit=CANDIDATE_LIMIT, pinned=[m["id"] for m in draft["markets"]])
    logger.info("assist turn %d with %s: %d candidates, %d markets in draft", len(body.messages), assistant.name, len(candidates), len(draft["markets"]))
    try:
        result = assistant.respond([m.model_dump() for m in body.messages], draft, candidates)
    except AssistantError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"reply": result.reply, "draft": result.draft, "model": result.model, "candidates": len(candidates)}


def draft_payload(row: IndexDraft, with_messages: bool = True) -> dict:
    payload = {
        "id": row.id, "name": row.name, "intent": row.intent, "description": row.description, "methodology": row.methodology,
        "keywords": row.keywords, "tags": row.tags, "min_liquidity": row.min_liquidity, "markets": row.markets,
        "market_count": len(row.markets or []), "status": row.status, "published_index": row.published_index,
        "model": row.model, "version": row.version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if with_messages:
        payload["messages"] = row.messages
    return payload


def _apply(row: IndexDraft, body: DraftBody) -> None:
    fields = normalize_draft(body.model_dump(), [], {"markets": body.markets, "min_liquidity": body.min_liquidity})
    row.name = body.name.strip()
    row.intent = body.intent.strip()
    row.description, row.methodology = fields["description"], fields["methodology"]
    row.keywords, row.tags, row.min_liquidity, row.markets = fields["keywords"], fields["tags"], fields["min_liquidity"], fields["markets"]
    row.messages = [m.model_dump() for m in body.messages]
    row.model = body.model


def stored_draft(session, draft_id: int) -> IndexDraft:
    row = session.get(IndexDraft, draft_id)
    if row is None:
        raise HTTPException(404, f"unknown draft {draft_id}")
    return row


@router.get("/drafts")
def drafts(session=Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(IndexDraft).order_by(IndexDraft.updated_at.desc(), IndexDraft.id.desc()))
    return [draft_payload(r, with_messages=False) for r in rows]


@router.post("/drafts", status_code=201)
def create_draft(body: DraftBody, session=Depends(get_session)) -> dict:
    row = IndexDraft(status=DRAFT_STATUS_DRAFT)
    _apply(row, body)
    session.add(row)
    session.commit()
    logger.info("draft %d %r saved with %d markets", row.id, row.name, len(row.markets))
    return draft_payload(row)


@router.get("/drafts/{draft_id}")
def read_draft(draft_id: int, session=Depends(get_session)) -> dict:
    return draft_payload(stored_draft(session, draft_id))


@router.put("/drafts/{draft_id}")
def update_draft(draft_id: int, body: DraftBody, session=Depends(get_session)) -> dict:
    row = stored_draft(session, draft_id)
    _apply(row, body)
    row.version += 1
    session.commit()
    logger.info("draft %d %r updated to version %d", row.id, row.name, row.version)
    return draft_payload(row)


@router.delete("/drafts/{draft_id}", status_code=204)
def delete_draft(draft_id: int, session=Depends(get_session)) -> Response:
    session.delete(stored_draft(session, draft_id))
    session.commit()
    logger.info("draft %d deleted", draft_id)
    return Response(status_code=204)


@router.post("/drafts/{draft_id}/metric", status_code=201)
def create_metric_draft(draft_id: int, session=Depends(get_session)) -> dict:
    """Register the draft's markets as market-basket metric in the catalog (status draft, release in tab "Metriken")."""
    row = stored_draft(session, draft_id)
    logger.info("draft %d %r: metric draft requested (%d markets)", row.id, row.name, len(row.markets or []))
    if not row.markets:
        logger.warning("draft %d metric draft refused (400): no markets", row.id)
        raise HTTPException(400, "Der Entwurf braucht mindestens einen Markt, um eine Metrik zu werden.")
    key = f"mf_{row.id}"
    try:
        state = create_basket(session, key, row.name, row.methodology or row.description, row.markets, row.id)
    except MetricExists:
        logger.warning("draft %d metric draft refused (409): metric %s exists", row.id, key)
        raise HTTPException(409, f"Aus diesem Entwurf wurde bereits die Metrik {key} angelegt.") from None
    row.status, row.published_index = DRAFT_STATUS_METRIC, key
    session.commit()
    return {"metric_key": key, "status": state.status, "draft": draft_payload(row, with_messages=False)}
