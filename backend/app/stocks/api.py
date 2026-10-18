"""Stock correlation endpoints.

GET  /stocks/status                         -> stored closes: rows, tickers, date range
POST /stocks/refresh                        -> load missing closes from Yahoo Finance (slow on first call;
                                               ?backfill=true also loads older closes back to 5 years)
GET  /indices/{name}/correlation            -> stored rule vs. top-100 US stocks (?days, ?min_change, ?min_stock_move)
POST /indices/{name}/correlation/preview    -> same for the rule in the body; DB untouched
GET  /indices/{name}/correlation/{ticker}   -> signal, trades and performance against one stock
POST /indices/{name}/correlation/{ticker}/preview -> same for the rule in the body; DB untouched
GET  /indices/{name}/correlation/{ticker}/attribution -> share of each index component in correlation and beta
POST /indices/{name}/correlation/{ticker}/attribution/preview -> same for the rule in the body; DB untouched
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.deps import get_session
from backend.app.engine import build_rule, get_rule
from backend.app.history import DEFAULT_DAYS, MAX_DAYS
from backend.app.schemas import RuleSpec
from backend.app.stocks.attribution import stock_attribution
from backend.app.stocks.correlation import DEFAULT_MIN_STOCK_MOVE, index_correlation
from backend.app.stocks.prices import price_status, refresh_prices
from backend.app.stocks.trades import stock_trades
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stocks"])


def get_fetcher():
    """Price source as a dependency (None: the raw data layer) so tests never reach the network."""
    return None


@router.get("/stocks/status")
def stocks_status(session=Depends(get_session)) -> dict:
    return price_status(session)


@router.post("/stocks/refresh")
def stocks_refresh(backfill: bool = False, session=Depends(get_session), fetcher=Depends(get_fetcher)) -> dict:
    logger.info("stock price refresh requested (backfill=%s)", backfill)
    return {**refresh_prices(session, fetcher, backfill=backfill, trigger="stocks"), "status": price_status(session)}


MinChange = Query(0.0, ge=0, le=1, description="index change (0..1 scale) that counts as an up/down event")

MinStockMove = Query(DEFAULT_MIN_STOCK_MOVE, ge=0, le=0.5,
                     description="next-day return (0.01 = 1 %) a signal must exceed in the expected direction to count as a hit")


@router.get("/indices/{index_name}/correlation")
def stored_correlation(index_name: str, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS), min_change: float = MinChange,
                       min_stock_move: float = MinStockMove, session=Depends(get_session)) -> dict:
    rule = get_rule(session, index_name)
    if rule is None:
        raise HTTPException(404, f"unknown index {index_name!r}")
    return index_correlation(session, rule, days, min_change, min_stock_move)


@router.post("/indices/{index_name}/correlation/preview")
def preview_correlation(index_name: str, body: RuleSpec, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
                        min_change: float = MinChange, min_stock_move: float = MinStockMove,
                        session=Depends(get_session)) -> dict:
    return index_correlation(session, build_rule(index_name, **body.validated()), days, min_change, min_stock_move)


def _known_ticker(ticker: str) -> str:
    if ticker not in TOP_US_STOCKS:
        raise HTTPException(404, f"unknown ticker {ticker!r}")
    return ticker


@router.get("/indices/{index_name}/correlation/{ticker}")
def stored_trades(index_name: str, ticker: str, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
                  min_change: float = MinChange, session=Depends(get_session)) -> dict:
    rule = get_rule(session, index_name)
    if rule is None:
        raise HTTPException(404, f"unknown index {index_name!r}")
    return stock_trades(session, rule, _known_ticker(ticker), days, min_change)


@router.post("/indices/{index_name}/correlation/{ticker}/preview")
def preview_trades(index_name: str, ticker: str, body: RuleSpec, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
                   min_change: float = MinChange, session=Depends(get_session)) -> dict:
    return stock_trades(session, build_rule(index_name, **body.validated()), _known_ticker(ticker), days, min_change)


@router.get("/indices/{index_name}/correlation/{ticker}/attribution")
def stored_attribution(index_name: str, ticker: str, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
                       min_change: float = MinChange, session=Depends(get_session)) -> dict:
    rule = get_rule(session, index_name)
    if rule is None:
        raise HTTPException(404, f"unknown index {index_name!r}")
    return stock_attribution(session, rule, _known_ticker(ticker), days, min_change)


@router.post("/indices/{index_name}/correlation/{ticker}/attribution/preview")
def preview_attribution(index_name: str, ticker: str, body: RuleSpec, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
                        min_change: float = MinChange, session=Depends(get_session)) -> dict:
    return stock_attribution(session, build_rule(index_name, **body.validated()), _known_ticker(ticker), days, min_change)
