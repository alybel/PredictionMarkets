"""Data catalog endpoints (tab "Datenkatalog").

GET /catalog/summary                          -> markets, rows and period per source; stock overview
GET /catalog/markets                          -> paged market catalog (?q, source, min_days, current, sort, limit, offset)
GET /catalog/markets/{source}/{external_id}   -> catalog entry plus full daily history of one market
GET /catalog/stocks                           -> every ticker with period, returns and quality
GET /catalog/stocks/{ticker}                  -> catalog entry plus every stored close of one ticker
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.catalog.markets import PAGE_LIMIT, SORTS, catalog_summary, list_markets, market_detail
from backend.app.catalog.stocks import list_stocks, stock_detail
from backend.app.deps import get_session

router = APIRouter(tags=["catalog"])


@router.get("/catalog/summary")
def summary(session=Depends(get_session)) -> dict:
    stocks = list_stocks(session)
    return {"sources": catalog_summary(session),
            "stocks": {"tickers": len(stocks), "with_prices": sum(1 for s in stocks if s["days"]),
                       "rows": sum(s["days"] for s in stocks)}}


@router.get("/catalog/markets")
def markets(q: str = Query("", max_length=100), source: str = Query("", max_length=32), min_days: int = Query(1, ge=1, le=3650),
            current: bool = False, sort: str = Query("days", pattern="^(" + "|".join(SORTS) + ")$"),
            limit: int = Query(50, ge=1, le=PAGE_LIMIT), offset: int = Query(0, ge=0), session=Depends(get_session)) -> dict:
    return list_markets(session, q=q, source=source, min_days=min_days, current=current, sort=sort, limit=limit, offset=offset)


@router.get("/catalog/markets/{source}/{external_id:path}")
def market(source: str, external_id: str, session=Depends(get_session)) -> dict:
    detail = market_detail(session, source, external_id)
    if detail is None:
        raise HTTPException(404, f"no stored rows for {source}/{external_id}")
    return detail


@router.get("/catalog/stocks")
def stocks(session=Depends(get_session)) -> list[dict]:
    return list_stocks(session)


@router.get("/catalog/stocks/{ticker}")
def stock(ticker: str, session=Depends(get_session)) -> dict:
    detail = stock_detail(session, ticker)
    if detail is None:
        raise HTTPException(404, f"no stored closes for {ticker}")
    return detail
