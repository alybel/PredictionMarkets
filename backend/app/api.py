"""REST API: consolidated index values plus per-market details.

GET    /                         -> on-demand dashboard (static frontend)
GET    /health                   -> version, index count, cache state, latest EUI value, schema state (no fetch)
POST   /client-log               -> dashboard events (clicks, filters, errors) into the server log
GET    /indices                  -> archive: stored rule-based indices (read only, flagged ``archived``)
POST   /indices                  -> 409: new indices are built from validated metrics (POST /builder/indices)
GET    /indices/{name}           -> consolidated value, markets, filter funnel, timestamp
                                    (?refresh=1 bypasses the market cache)
POST   /indices/{name}/preview   -> same payload for a rule sent in the body; DB untouched
PUT    /indices/{name}           -> 409: archive indices are read only (404 if unknown)
DELETE /indices/{name}           -> delete an archive index (204; 404 if unknown)
GET    /indices/{name}/history   -> index value per stored snapshot day (backtest, see backend.app.history)
GET    /indices/{name}/correlation -> index changes vs. next-day returns of the top-100 US stocks (see backend.app.stocks)
GET    /tags                     -> tags delivered by the market population, with counts
GET    /topics                   -> topic tree: topic -> subtopic with counts (from the tag list)
GET    /topics/nodes             -> events (with outcomes) and markets of one subtopic (?topic, subtopic)
/eui/...                         -> Economic Uncertainty Index (see backend.app.eui.api)
/quality/...                     -> data quality and look-ahead guards of the stored history (see backend.app.quality)
/fed/..., /quality/fed/...       -> Fed rate chains: staging, raw layer, front/second chain, checks (see backend.app.fed.api)
/manufacture/...                 -> Index Manufacture: catalog, assistant, drafts (see backend.app.manufacture.api)
/metrics/...                     -> metric catalog: status, checks, release, block (see backend.app.metrics.api)
/builder/...                     -> index builder from validated metrics (see backend.app.builder.api)
/catalog/...                     -> data catalog: stored markets and stock closes with history and quality (see backend.app.catalog.api)
/events/...                      -> events: outcome markets of one source event bundled (see backend.app.events.api)
/domains/...                     -> domains with full raw history incl. expired markets (see backend.app.domains.api)
/directory/...                   -> market directory of both sources, "new markets" inbox, coverage (see backend.app.directory.api)
/raw/...                         -> raw data layer: refresh requests with progress, no parallel fetch (see backend.app.rawdata.api)
/risk/...                        -> risk topics: classification per topic, review list, timeline (see backend.app.risk.api)

Market data comes from a server-side cache of all sources (about 60 s);
both the DB session and the market provider are FastAPI dependencies so
tests can override them without touching network or a real database.
"""

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.app.aggregation import aggregate
from backend.app.clients.base import EVENT_FIELDS
from backend.app.deps import get_session, session_factory
from backend.app.engine import apply_funnel, build_rule, delete_rule, get_rule, list_rules, tag_counts
from backend.app.field_docs import field_docs_payload
from backend.app.backfill.api import router as backfill_router
from backend.app.builder.api import router as builder_router
from backend.app.catalog.api import router as catalog_router
from backend.app.events.api import router as events_router
from backend.app.agent.api import router as agent_router
from backend.app.agent.creator_api import router as creator_router
from backend.app.agent.finder_api import router as finder_router
from backend.app.domains.api import router as domains_router
from backend.app.directory.api import router as directory_router
from backend.app.rawdata.access import MissingRawData
from backend.app.rawdata.api import router as raw_router
from backend.app.rawdata.providers import RawMarkets
from backend.app.risk.api import router as risk_router
from backend.app.forecast.api import router as forecast_router
from backend.app.workshop.api import router as workshop_router
from backend.app.events.service import sync_events
from backend.app.events.tree import build_tree, subtopic_nodes
from backend.app.history import router as history_router
from backend.app.eui.api import router as eui_router
from backend.app.eui.compute import latest_value
from backend.app.logging_setup import configure_logging
from backend.app.manufacture.api import router as manufacture_router
from backend.app.market_cache import MarketCache
from backend.app.stocks.api import router as stocks_router
from backend.app.stocks.metric_api import router as metric_stocks_router
from backend.app.quality.api import router as quality_router
from backend.app.fed.api import router as fed_router
from backend.app.metrics.api import router as metrics_router
from backend.app.models import MIN_LIQUIDITY_USD, IndexRule, utcnow
from backend.app.schemas import ClientLog, RuleSpec
from backend.app.schema_state import auto_migrate_enabled, migrate_to_head, schema_status

configure_logging()
logger = logging.getLogger(__name__)
client_logger = logging.getLogger("backend.app.client")

APP_VERSION = "0.9.0"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("API started")
    if auto_migrate_enabled():
        try:
            migrate_to_head()
        except Exception:
            logger.exception("Schema migration failed; /health reports the outdated schema")
    else:
        logger.info("Schema auto-migration disabled (AUTO_MIGRATE=0)")
    yield
    logger.info("API stopped")


app = FastAPI(title="PredictionMarketIndex API", lifespan=lifespan)
app.include_router(eui_router)
app.include_router(history_router)
app.include_router(backfill_router)
app.include_router(manufacture_router)
app.include_router(stocks_router)
app.include_router(quality_router)
app.include_router(fed_router)
app.include_router(metrics_router)
app.include_router(builder_router)
app.include_router(metric_stocks_router)
app.include_router(catalog_router)
app.include_router(events_router)
app.include_router(agent_router)
app.include_router(finder_router)
app.include_router(creator_router)
app.include_router(domains_router)
app.include_router(directory_router)
app.include_router(raw_router)
app.include_router(risk_router)
app.include_router(forecast_router)
app.include_router(workshop_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        query = f"?{request.url.query}" if request.url.query else ""
        logger.exception("%s %s%s -> 500 unhandled exception (%.0f ms)", request.method, request.url.path, query, (time.monotonic() - started) * 1000)
        return JSONResponse({"detail": f"Internal Server Error: {type(exc).__name__}: {exc}"[:500]}, status_code=500)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        # Browsers must revalidate the frontend (ETag -> 304) so new JS/HTML never mixes with stale cached files.
        response.headers["Cache-Control"] = "no-cache"
    if not request.url.path.startswith("/static"):
        logger.info("%s %s -> %d (%.0f ms)", request.method, request.url.path, response.status_code, (time.monotonic() - started) * 1000)
    return response


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


def fetch_all_sources() -> list:
    """Normalized markets of all sources: one refresh request to the raw data layer, then built from its raw answers;
    a source without raw data stays empty (logged clearly, never a live fetch)."""
    markets: list = []
    for client in RawMarkets(session_factory(), "index").clients("index"):
        try:
            markets.extend(client.fetch_markets())
        except MissingRawData as exc:
            logger.warning("index population: %s", exc)
    return markets


market_cache = MarketCache(fetch_all_sources)


def get_markets(refresh: bool = False) -> list:
    """Cached market population; ``refresh`` forces a new fetch from the sources."""
    return market_cache.get(force=refresh)


@app.get("/health")
def health(session=Depends(get_session)) -> dict:
    """Status line of the dashboard header; reads the cache without fetching markets."""
    markets, age = market_cache.peek()
    eui = latest_value(session)
    return {
        "version": APP_VERSION,
        "indices": len(list_rules(session)),
        "markets_cached": None if markets is None else len(markets),
        "cache_age_s": None if age is None else round(age, 1),
        "eui_value": eui.value if eui else None,
        "eui_value_date": eui.value_date.isoformat() if eui else None,
        "schema": schema_status(session.connection()),
    }


@app.post("/client-log", status_code=204)
def client_log(body: ClientLog) -> Response:
    level = logging.ERROR if body.level == "error" else logging.WARNING if body.level == "warning" else logging.INFO
    client_logger.log(level, "%s %s", body.event, body.detail or "")
    return Response(status_code=204)


def rule_payload(rule: IndexRule) -> dict:
    return {
        "index_name": rule.index_name,
        "categories": rule.categories,
        "keywords": rule.keywords,
        "min_liquidity": rule.min_liquidity,
        "min_price": rule.min_price,
        "max_price": rule.max_price,
        "archived": True,
    }


def index_payload(rule: IndexRule, markets) -> dict:
    selected, funnel = apply_funnel(rule, markets)
    payload = asdict(aggregate(rule.index_name, selected))
    payload["markets"] = [{k: v for k, v in m.items() if k not in EVENT_FIELDS} for m in payload["markets"]]
    return {
        "as_of": utcnow().isoformat(),
        "funnel": funnel,
        "min_liquidity_floor": MIN_LIQUIDITY_USD,
        **payload,
    }


def stored_rule(session, index_name: str) -> IndexRule:
    rule = get_rule(session, index_name)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"unknown index {index_name!r}")
    return rule


@app.get("/indices")
def indices(session=Depends(get_session)) -> list[dict]:
    return [rule_payload(r) for r in list_rules(session)]


ARCHIVE_READ_ONLY = "Regelbasierte Indizes sind archiviert und nur lesbar; neue Indizes entstehen aus validierten Metriken."


@app.post("/indices", status_code=201)
def create_index() -> dict:
    logger.warning("rule index create refused (409): rule-based indices are archived")
    raise HTTPException(status_code=409, detail=ARCHIVE_READ_ONLY)


@app.get("/indices/{index_name}")
def index_value(index_name: str, session=Depends(get_session), markets=Depends(get_markets)) -> dict:
    return index_payload(stored_rule(session, index_name), markets)


@app.post("/indices/{index_name}/preview")
def preview_index(index_name: str, body: RuleSpec, markets=Depends(get_markets)) -> dict:
    """Evaluate a rule from the request body without touching the stored one."""
    return index_payload(build_rule(index_name, **body.validated()), markets)


@app.put("/indices/{index_name}")
def update_index(index_name: str, session=Depends(get_session)) -> dict:
    stored_rule(session, index_name)
    logger.warning("archive index %r update refused (409): read only", index_name)
    raise HTTPException(status_code=409, detail=ARCHIVE_READ_ONLY)


@app.delete("/indices/{index_name}", status_code=204)
def remove_index(index_name: str, session=Depends(get_session)) -> Response:
    logger.info("archive index %r delete requested", index_name)
    if not delete_rule(session, index_name):
        logger.warning("archive index %r delete refused (404): unknown", index_name)
        raise HTTPException(status_code=404, detail=f"unknown index {index_name!r}")
    logger.info("archive index %r deleted", index_name)
    return Response(status_code=204)


@app.post("/events/sync")
def events_sync(session=Depends(get_session), markets=Depends(get_markets)) -> dict:
    """Bundle the cached market population into events now (otherwise done by the daily snapshot)."""
    logger.info("events sync requested for %d markets", len(markets))
    counts = sync_events(session, markets)
    session.commit()
    return counts


@app.get("/topics")
def topics(markets=Depends(get_markets)) -> dict:
    """Topic tree (topic -> subtopic with counts) of the cached population; nodes load per subtopic."""
    return {"as_of": utcnow().isoformat(), "topics": build_tree(markets)}


@app.get("/topics/nodes")
def topic_nodes(topic: str = Query(..., max_length=200), subtopic: str = Query(..., max_length=200),
                markets=Depends(get_markets)) -> dict:
    """Events (with all outcome markets) and standalone markets of one subtopic."""
    nodes = subtopic_nodes(markets, topic, subtopic)
    logger.info("topic nodes %r / %r: %d nodes", topic, subtopic, len(nodes))
    return {"topic": topic, "subtopic": subtopic, "nodes": nodes}


@app.get("/tags")
def tags(markets=Depends(get_markets)) -> list[dict]:
    return tag_counts(markets)


@app.get("/fields")
def fields() -> dict:
    """Explanations of the market fields and how both sources are normalized to USD."""
    return field_docs_payload()
