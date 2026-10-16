"""Data quality endpoints (tab "Datenqualität").

GET /quality/report                          -> coverage per day, look-ahead guards, cleanliness checks (?days)
GET /quality/markets?q=                      -> markets whose title contains q, with stored days
GET /quality/markets/{source}/{external_id}  -> every stored day of one market with origin and point-in-time verdict
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.deps import get_session
from backend.app.quality.checks import DEFAULT_DAYS, quality_report
from backend.app.quality.explore import SEARCH_LIMIT, market_rows, search_markets

logger = logging.getLogger(__name__)
router = APIRouter(tags=["quality"])


@router.get("/quality/report")
def report(days: int = Query(DEFAULT_DAYS, ge=1, le=730), session=Depends(get_session)) -> dict:
    return quality_report(session, days)


@router.get("/quality/markets")
def markets(q: str = Query(..., min_length=2, max_length=100), limit: int = Query(20, ge=1, le=SEARCH_LIMIT),
            session=Depends(get_session)) -> list[dict]:
    return search_markets(session, q, limit)


@router.get("/quality/markets/{source}/{external_id:path}")
def market(source: str, external_id: str, session=Depends(get_session)) -> dict:
    rows = market_rows(session, source, external_id)
    if not rows:
        raise HTTPException(404, f"no stored rows for {source}/{external_id}")
    return {"source": source, "external_id": external_id, "rows": rows}
