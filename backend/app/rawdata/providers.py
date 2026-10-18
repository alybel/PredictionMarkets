"""Raw data access of the business processes: refresh request to the raw layer, then reading the answers back.

kind     dataset        request sequence (the process's own code, run against the ``RawRecorder``)
markets  index_markets  Polymarket/Kalshi client paging per profile (``clients.profiles``: "index", "snapshot")
domains  domain_scan    ``domains.discovery.scan_sources`` for one topic/subtopic/terms
history  price_history  ``backfill.sources.fetch_history`` per market
stocks   stock_chart    ``stocks.prices.fetch_closes`` per ticker

A provider asks the raw layer (``jobs.request_refresh``, waited for; equal requests share one job via ``scope``) and
then runs the same request sequence against a ``RawReader`` limited to that job's staging files, so the process gets
exactly what the old live call gave. While an equal job runs elsewhere, the provider waits for it; without a usable
job it reads the latest stored answers, and without any it reports ``MissingRawData`` - never a live fetch.
"""

import hashlib
import json
import logging
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from sqlalchemy.orm import Session, sessionmaker

from backend.app.backfill import sources as history_sources
from backend.app.clients.profiles import make_clients
from backend.app.domains.discovery import scan_sources
from backend.app.models import utcnow
from backend.app.rawdata import jobs
from backend.app.rawdata.access import MissingRawData, RawReader, RawRecorder
from backend.app.rawdata.context import Context, Fetched
from backend.app.rawdata.models import DONE, RUNNING

logger = logging.getLogger(__name__)

WAIT_S = 600       # longest wait for an equal job running elsewhere
WORKERS = 8        # parallel market histories inside the raw job "history"


def scope_of(options: dict) -> str:
    return hashlib.sha256(json.dumps(options, sort_keys=True, default=str).encode()).hexdigest()[:12]


class RawProvider:
    kind = dataset = ""
    ignore: frozenset[str] = frozenset()

    def __init__(self, factory: sessionmaker, trigger: str = "manual", get_raw=None, staging=None, now=utcnow):
        self.factory, self.trigger, self.get_raw, self.staging, self.now = factory, trigger, get_raw, staging, now
        self.files: list[str] | None = None
        self.job: dict | None = None

    @classmethod
    def for_session(cls, session: Session, trigger: str = "manual", **kw):
        session.commit()  # the raw job writes through its own connections
        return cls(sessionmaker(bind=session.get_bind(), expire_on_commit=False), trigger, **kw)

    def _await(self, job: dict) -> dict:
        deadline = clock.monotonic() + WAIT_S
        jobs.wait(job["id"], WAIT_S)
        while job["status"] == RUNNING and clock.monotonic() < deadline:
            clock.sleep(0.5)
            with self.factory() as session:
                job = jobs.get_job(session, job["id"])
        return job

    def refresh(self, scope: str, **options) -> dict:
        """Refresh request, waited for; afterwards ``reader`` replays exactly this job's answers."""
        name = f"{self.kind}:{scope}"[:32]
        job = jobs.request_refresh(self.factory, self.kind, self.trigger, scope=scope, get_raw=self.get_raw, staging=self.staging,
                                   now=self.now, background=False, **options)
        if job["duplicate"]:
            logger.info("raw %s (%s): equal job #%d running, waiting for it", name, self.trigger, job["id"])
            job = self._await(job)
        self.job = job
        self.files = (job.get("summary") or {}).get("files") if job["status"] == DONE else None
        if self.files is None:
            logger.warning("raw %s (%s): job #%d %s (%s) - reading the latest stored raw data", name, self.trigger, job["id"],
                           job["status"], job.get("error"))
        return job

    def reader(self) -> RawReader:
        return RawReader(self.factory, self.dataset, self.files, self.ignore)

    @classmethod
    def plan(cls, ctx: Context, get) -> dict | None:
        raise NotImplementedError

    @classmethod
    def fetch(cls, ctx: Context) -> Fetched:
        """The raw job of this kind: run the process's request sequence against the recorder."""
        rec = RawRecorder(ctx.factory, cls.dataset, ctx.get_raw, ctx.now, ctx.staging, cls.ignore)
        extra = cls.plan(ctx, rec) or {}
        logger.info("raw job #%d %s: %d requests, %d staging files written, %d errors", ctx.job_id, ctx.job, rec.requests, rec.staged,
                    rec.errors)
        return Fetched({**rec.summary(), **extra}, rec.files)


class RawMarkets(RawProvider):
    """Market population of a client profile (dashboard indices, daily uncertainty snapshot)."""
    kind, dataset = "markets", "index_markets"

    def clients(self, profile: str, refresh: bool = True) -> list:
        """Both clients of the profile, reading from raw data (after a refresh request unless ``refresh`` is False)."""
        if refresh:
            self.refresh(profile, profile=profile)
        return make_clients(profile, lambda source: self.reader())

    @classmethod
    def plan(cls, ctx: Context, get) -> dict:
        profile = ctx.options.get("profile") or "index"
        out = {}
        for n, client in enumerate(make_clients(profile, lambda source: get, fetching=True)):
            ctx.progress(n, 2, f"Abruf {client.source}")
            out[client.source] = len(client._fetch_raw())
        return {"profile": profile, "markets": out}


class RawScanner(RawProvider):
    """Domain scan: same call as ``scan_sources(topic, subtopic, terms)``."""
    kind, dataset = "domains", "domain_scan"

    def __call__(self, topic: str, subtopic: str | None, terms: list[str]) -> dict[str, dict]:
        options = {"topic": topic, "subtopic": subtopic, "terms": list(terms)}
        self.refresh(scope_of(options), **options)
        return scan_sources(topic, subtopic, list(terms), self.reader())

    @classmethod
    def plan(cls, ctx: Context, get) -> dict:
        o = ctx.options
        ctx.progress(0, 1, f"Domänen-Suche {o['topic']}")
        found = scan_sources(o["topic"], o.get("subtopic"), list(o.get("terms") or []), get)
        return {"markets": {s: len(v["markets"]) for s, v in found.items()}}


class RawHistory(RawProvider):
    """Price histories: ``prepare`` asks once for all markets, the call has the signature of ``fetch_history``."""
    kind, dataset, ignore = "history", "price_history", history_sources.VOLATILE

    def prepare(self, keys: list[tuple[str, str]], start: date) -> None:
        if keys:
            options = {"markets": sorted({(s, e) for s, e in keys}), "start": start.isoformat()}
            self.refresh(scope_of(options), **options)

    def __call__(self, source: str, external_id: str, start: date) -> dict:
        try:
            return history_sources.fetch_history(source, external_id, start, self.reader())
        except MissingRawData as exc:
            logger.warning("history of %s/%s: %s", source, external_id, exc)
            return {}

    @classmethod
    def plan(cls, ctx: Context, get) -> dict:
        markets, start = ctx.options["markets"], date.fromisoformat(ctx.options["start"])
        with ThreadPoolExecutor(min(WORKERS, len(markets) or 1)) as pool:
            found = 0
            for n, points in enumerate(pool.map(lambda m: history_sources.fetch_history(m[0], m[1], start, get), markets), 1):
                found += bool(points)
                if n % 20 == 0:
                    ctx.progress(n, len(markets), f"Preis-Historie: {n} von {len(markets)} Märkten")
        return {"markets": len(markets), "with_history": found}


class RawCloses(RawProvider):
    """Stock closes: ``prepare`` asks once for all tickers, the call has the signature of the price fetcher.
    ``stocks.prices`` imports this module, so it is imported lazily here."""
    kind, dataset, ignore = "stocks", "stock_chart", frozenset({"period1", "period2"})  # the latest chart of a ticker counts

    def prepare(self, starts: dict[str, date]) -> None:
        if starts:
            options = {"starts": {t: d.isoformat() for t, d in sorted(starts.items())}}
            self.refresh(scope_of(options), **options)

    def __call__(self, ticker: str, start: date) -> dict:
        from backend.app.stocks.prices import fetch_closes

        return fetch_closes(ticker, start, self.reader())

    @classmethod
    def plan(cls, ctx: Context, get) -> dict:
        from backend.app.stocks.prices import fetch_closes

        starts = ctx.options["starts"]
        failed = []
        for n, (ticker, start) in enumerate(starts.items()):
            ctx.progress(n, len(starts), f"Kurse {ticker}")
            try:
                fetch_closes(ticker, date.fromisoformat(start), get)
            except (ValueError, TypeError, AttributeError) as exc:
                failed.append(ticker)
                logger.warning("raw job #%d: closes of %s unusable: %s", ctx.job_id, ticker, exc)
        return {"tickers": len(starts), "failed": failed}


PROVIDERS: dict[str, type[RawProvider]] = {p.kind: p for p in (RawMarkets, RawScanner, RawHistory, RawCloses)}
for _kind, _provider in PROVIDERS.items():
    jobs.register(_kind, _provider.fetch)
