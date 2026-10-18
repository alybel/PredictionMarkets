"""Two getters with the same contract ``get(url, params) -> parsed JSON | None`` for the business processes.

* ``RawRecorder`` (inside a raw job only): API -> staging file -> ``raw_responses``, then the parsed answer.
* ``RawReader`` (the processes): reads the stored answer of the same request from ``raw_responses`` - never the network.

A process runs the same request sequence against either getter, so building from the raw answers gives exactly
the result of the old live processing. Volatile parameters (e.g. the end time "now") are left out of the request
identity (``ignore``), so a later read finds the answer. With ``files`` (the staging files of one raw job) the reader
replays exactly that job: a request the job could not answer is None, as it was live. Without ``files`` the latest
stored answer counts; when not even the first request has a stored answer, ``MissingRawData`` says so clearly
instead of reaching for the network.
"""

import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.staging.files import Stager, read_staged, request_key, sha256
from backend.app.staging.models import LOADED, RawResponse
from backend.app.staging.raw_loader import insert_ignore, log_entry, raw_values

logger = logging.getLogger(__name__)

GetJson = Callable[[str, dict], object]
HOSTS = {"polymarket.com": "polymarket", "kalshi.com": "kalshi", "yahoo.com": "yahoo"}


class MissingRawData(LookupError):
    """No stored raw answer for a request: the process needs a refresh request, never a live fetch."""


def source_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    return next((s for h, s in HOSTS.items() if host == h or host.endswith("." + h)), "other")


class RawRecorder:
    """Getter of a raw job: every answer goes unchanged to staging and at once into ``raw_responses`` (thread-safe)."""

    def __init__(self, factory: sessionmaker, dataset: str, get_raw: Callable[[str, dict], bytes | None], now: Callable[[], datetime],
                 staging: Path | None = None, ignore: frozenset[str] = frozenset()):
        self.factory, self.dataset, self.get_raw, self.now = factory, dataset, get_raw, now
        self.stager = Stager(staging, ignore)
        self.run_id = uuid.uuid4().hex
        self.files: list[str] = []
        self.requests = self.errors = self.staged = 0
        self.error_details: list[str] = []
        self._lock = threading.Lock()

    def _fail(self, text: str) -> None:
        with self._lock:
            self.errors += 1
            self.error_details.append(text[:200])
        logger.warning("raw %s: %s", self.dataset, text)

    def __call__(self, url: str, params: dict | None = None) -> object:
        params = dict(params or {})
        body = self.get_raw(url, params)
        with self._lock:
            self.requests += 1
        if body is None:
            self._fail(f"request failed: {url} {params}")
            return None
        try:
            with self._lock:
                staged = self.stager.write(source_of(url), self.dataset, url, params, body, self.now())
                self.staged += staged.written
                rel = staged.path.relative_to(self.stager.root).as_posix()
                self.files.append(rel)
                with self.factory.begin() as session:  # staging -> raw at once: the process reads it right after the job
                    f = read_staged(self.stager.root, staged.path)
                    insert_ignore(session, RawResponse, raw_values(f))
                    log_entry(session, rel, LOADED, self.run_id, checksum=f.checksum)
        except (OSError, ValueError, SQLAlchemyError) as exc:  # the answer itself is still usable for this job
            self._fail(f"staging/raw store failed for {url}: {exc}")
        try:
            return json.loads(body)
        except ValueError as exc:
            self._fail(f"answer is no JSON ({url} {params}): {exc}")
            return None

    def summary(self) -> dict:
        return {"dataset": self.dataset, "requests": self.requests, "staged": self.staged, "errors": self.errors,
                "error_details": self.error_details[:10]}


class RawReader:
    """Getter of a process: the stored answer of the same request (``files``: only answers of that raw job)."""

    def __init__(self, factory: sessionmaker, dataset: str, files: list[str] | None = None, ignore: frozenset[str] = frozenset()):
        self.factory, self.dataset, self.ignore = factory, dataset, ignore
        self.files = set(files) if files is not None else None
        self.hits = self.misses = 0

    def __call__(self, url: str, params: dict | None = None) -> object:
        key = request_key(url, params, self.ignore)
        r = RawResponse
        with self.factory() as session:
            rows = session.execute(select(r.staging_file, r.content).where(r.dataset == self.dataset, r.request_id == sha256(key.encode()))
                                   .order_by(r.fetched_at.desc(), r.id.desc())).all()
        content = next((c for f, c in rows if self.files is None or f in self.files), None)
        if content is None:
            self.misses += 1
            if self.files is None and not self.hits:
                raise MissingRawData(f"Keine Rohdaten für {self.dataset} ({key}) – bitte zuerst „Rohdaten aktualisieren“ anfordern")
            logger.debug("raw read %s: no answer for %s", self.dataset, key)
            return None
        self.hits += 1
        try:
            return json.loads(content)
        except ValueError:
            logger.warning("raw read %s: stored answer of %s is no JSON", self.dataset, key)
            return None


def session_getter(session: requests.Session, timeout: float, label: str) -> GetJson:
    """Getter over an injected HTTP session (tests and tools with their own session); None on any error."""
    def get(url: str, params: dict | None = None) -> object:
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("%s: request to %s failed: %s", label, url, exc)
            return None
    return get
