"""Stock evaluation endpoints of metric indices (1015-2231); the archive endpoints of ``stocks.api`` stay unchanged.

GET /builder/indices/{name}/correlation           -> market row + top-100 table, simple and multiple regression
                                                     (?days, ?min_change in z-points, ?min_stock_move, ?target=excess|raw)
GET /builder/indices/{name}/correlation/{ticker}  -> trades, robustness and both regressions against one stock
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.deps import get_session
from backend.app.history import DEFAULT_DAYS, MAX_DAYS
from backend.app.stocks.correlation import DEFAULT_MIN_STOCK_MOVE
from backend.app.stocks.metric_data import EXCESS, find_index
from backend.app.stocks.metric_regression import metric_correlation, metric_stock_detail
from backend.app.stocks.universe import TOP_US_STOCKS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/builder/indices", tags=["stocks"])

Days = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS)
MinChangeZ = Query(0.0, ge=0, le=5, description="index change in z-points that counts as an up/down event")
MinStockMove = Query(DEFAULT_MIN_STOCK_MOVE, ge=0, le=0.5, description="next-day return a signal must exceed to count as a hit")
Target = Query(EXCESS, description="excess: return minus equal-weighted top-100 market (default); raw: plain return")


def _index(session, name: str):
    row = find_index(session, name)
    if row is None:
        logger.warning("metric correlation refused (404): unknown metric index %r", name)
        raise HTTPException(404, f"Unbekannter Index: {name}")
    return row


@router.get("/{name}/correlation")
def correlation(name: str, days: int = Days, min_change: float = MinChangeZ, min_stock_move: float = MinStockMove,
                target: Literal["excess", "raw"] = Target, session=Depends(get_session)) -> dict:
    logger.info("metric correlation requested: index=%r days=%d target=%s min_change=%s min_stock_move=%s",
                name, days, target, min_change, min_stock_move)
    return metric_correlation(session, _index(session, name), days, min_change, min_stock_move, target)


@router.get("/{name}/correlation/{ticker}")
def stock_detail(name: str, ticker: str, days: int = Days, min_change: float = MinChangeZ,
                 target: Literal["excess", "raw"] = Target, session=Depends(get_session)) -> dict:
    logger.info("metric stock detail requested: index=%r ticker=%s days=%d target=%s min_change=%s",
                name, ticker, days, target, min_change)
    row = _index(session, name)
    if ticker not in TOP_US_STOCKS:
        raise HTTPException(404, f"unknown ticker {ticker!r}")
    return metric_stock_detail(session, row, ticker, days, min_change, target)
