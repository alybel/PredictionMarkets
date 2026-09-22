"""REST API: consolidated index values plus per-market details.

GET    /                         -> on-demand dashboard (static frontend)
GET    /indices                  -> stored index rules
POST   /indices                  -> create an index (409 if the name exists)
GET    /indices/{name}           -> consolidated value, markets, filter funnel, timestamp
                                    (?refresh=1 bypasses the market cache)
POST   /indices/{name}/preview   -> same payload for a rule sent in the body; DB untouched
PUT    /indices/{name}           -> replace the stored rule
DELETE /indices/{name}           -> remove the index
GET    /tags                     -> tags delivered by the market population, with counts
/eui/...                         -> Economic Uncertainty Index (see backend.app.eui.api)

Market data comes from a server-side cache of all sources (about 60 s);
both the DB session and the market provider are FastAPI dependencies so
tests can override them without touching network or a real database.
"""

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.aggregation import aggregate
from backend.app.clients.kalshi import KalshiClient
from backend.app.clients.polymarket import PolymarketClient
from backend.app.deps import get_session
from backend.app.engine import apply_funnel, build_rule, delete_rule, get_rule, list_rules, tag_counts, upsert_rule
from backend.app.eui.api import router as eui_router
from backend.app.logging_setup import configure_logging
from backend.app.market_cache import MarketCache
from backend.app.models import MIN_LIQUIDITY_USD, IndexRule, utcnow
from backend.app.schemas import IndexCreate, RuleSpec

configure_logging()
logger = logging.getLogger(__name__)



@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("API started")
    yield
    logger.info("API stopped")


app = FastAPI(title="PredictionMarketIndex API", lifespan=lifespan)
app.include_router(eui_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    if not request.url.path.startswith("/static"):
        logger.info("%s %s -> %d (%.0f ms)", request.method, request.url.path, response.status_code, (time.monotonic() - started) * 1000)
    return response


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


def fetch_all_sources() -> list:
    """Fresh normalized markets from all sources; empty on API failures."""
    markets: list = []
    for client in (PolymarketClient(), KalshiClient()):
        markets.extend(client.fetch_markets())
    return markets


market_cache = MarketCache(fetch_all_sources)


def get_markets(refresh: bool = False) -> list:
    """Cached market population; ``refresh`` forces a new fetch from the sources."""
    return market_cache.get(force=refresh)


def rule_payload(rule: IndexRule) -> dict:
    return {
        "index_name": rule.index_name,
        "categories": rule.categories,
        "keywords": rule.keywords,
        "min_liquidity": rule.min_liquidity,
        "min_price": rule.min_price,
        "max_price": rule.max_price,
    }


def index_payload(rule: IndexRule, markets) -> dict:
    selected, funnel = apply_funnel(rule, markets)
    result = aggregate(rule.index_name, selected)
    return {
        "as_of": utcnow().isoformat(),
        "funnel": funnel,
        "min_liquidity_floor": MIN_LIQUIDITY_USD,
        **asdict(result),
    }


def stored_rule(session, index_name: str) -> IndexRule:
    rule = get_rule(session, index_name)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"unknown index {index_name!r}")
    return rule


@app.get("/indices")
def indices(session=Depends(get_session)) -> list[dict]:
    return [rule_payload(r) for r in list_rules(session)]


@app.post("/indices", status_code=201)
def create_index(body: IndexCreate, session=Depends(get_session)) -> dict:
    name = body.validated_name()
    fields = body.validated()
    if get_rule(session, name) is not None:
        raise HTTPException(status_code=409, detail=f"index {name!r} already exists")
    return rule_payload(upsert_rule(session, name, **fields))


@app.get("/indices/{index_name}")
def index_value(index_name: str, session=Depends(get_session), markets=Depends(get_markets)) -> dict:
    return index_payload(stored_rule(session, index_name), markets)


@app.post("/indices/{index_name}/preview")
def preview_index(index_name: str, body: RuleSpec, markets=Depends(get_markets)) -> dict:
    """Evaluate a rule from the request body without touching the stored one."""
    return index_payload(build_rule(index_name, **body.validated()), markets)


@app.put("/indices/{index_name}")
def update_index(index_name: str, body: RuleSpec, session=Depends(get_session)) -> dict:
    fields = body.validated()
    stored_rule(session, index_name)
    return rule_payload(upsert_rule(session, index_name, **fields))


@app.delete("/indices/{index_name}", status_code=204)
def remove_index(index_name: str, session=Depends(get_session)) -> Response:
    if not delete_rule(session, index_name):
        raise HTTPException(status_code=404, detail=f"unknown index {index_name!r}")
    return Response(status_code=204)


@app.get("/tags")
def tags(markets=Depends(get_markets)) -> list[dict]:
    return tag_counts(markets)
