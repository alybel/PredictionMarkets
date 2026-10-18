"""Forecast report endpoints (risk index 6/7).

GET  /builder/indices/{name}/forecast -> stored report (computed and stored on first request); 404 unknown index
POST /builder/indices/{name}/forecast -> recompute now (?load_prices=true first loads the target closes); 404 unknown
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.app.deps import get_session
from backend.app.forecast.report import refresh_benchmarks, store_report, stored_report
from backend.app.stocks.api import get_fetcher
from backend.app.stocks.metric_data import find_index

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/builder/indices", tags=["forecast"])


def _index(session, name: str):
    row = find_index(session, name)
    if row is None:
        logger.warning("forecast report refused (404): unknown metric index %r", name)
        raise HTTPException(404, f"Unbekannter Index: {name}")
    return row


@router.get("/{name}/forecast")
def read_forecast(name: str, session=Depends(get_session)) -> dict:
    row = _index(session, name)
    report = stored_report(session, name)
    logger.info("forecast report of %r requested (%s)", name, "stored" if report else "computing on demand")
    return report or store_report(session, row, "on_demand")


@router.post("/{name}/forecast")
def recompute_forecast(name: str, load_prices: bool = False, session=Depends(get_session), fetcher=Depends(get_fetcher)) -> dict:
    row = _index(session, name)
    logger.info("forecast report of %r recompute requested (load_prices=%s)", name, load_prices)
    if load_prices:
        refresh_benchmarks(session, fetcher)
    return store_report(session, row, "manual")
