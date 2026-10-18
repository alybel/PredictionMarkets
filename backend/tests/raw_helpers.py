"""Helpers for the raw-layer process tests: file database, fake network for the raw layer, prepared raw rows."""

import json

import pytest
from sqlalchemy import create_engine

from backend.app.db import Base, make_session_factory
from backend.app.rawdata import jobs
from backend.app.rawdata.access import source_of
from backend.app.staging.files import request_key, sha256
from backend.app.staging.models import RawResponse


class FakeNet:
    """Stands in for the raw layer's only live GET: answers from ``answer(url, params) -> JSON | None``."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        data = self.answer(url, dict(params))
        return None if data is None else json.dumps(data).encode()


class Capture:
    """JSON getter for the old processing that remembers every answer (to prepare equal raw rows)."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def __call__(self, url, params=None):
        data = self.answer(url, dict(params or {}))
        self.seen.append((url, dict(params or {}), data))
        return data


def file_factory(path):
    """Session factory of a fresh file database (the raw job and the readers use their own connections)."""
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture()
def factory(tmp_path):
    f = file_factory(tmp_path / "raw.db")
    yield f
    f.kw["bind"].dispose()


@pytest.fixture()
def net(monkeypatch, tmp_path):
    """Install a fake network into the raw layer (``net.set(answer)``) and a staging folder under tmp."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    fake = FakeNet(lambda url, params: None)
    monkeypatch.setattr(jobs, "http_get_raw", fake)
    fake.set = lambda answer: setattr(fake, "answer", answer)
    return fake


def prepare_raw(factory, dataset: str, seen: list, ignore=frozenset(), fetched_at=None) -> int:
    """Store captured answers as ``raw_responses`` rows (as a raw job would); returns the number stored."""
    from backend.app.models import utcnow

    n, keys = 0, set()
    with factory.begin() as session:
        for url, params, data in seen:
            key, body = request_key(url, params, ignore), json.dumps(data)
            if data is None or key in keys:
                continue
            keys.add(key)
            n += 1
            session.add(RawResponse(source=source_of(url), dataset=dataset, url=url, request_key=key, request_id=sha256(key.encode()),
                                    fetched_at=fetched_at or utcnow(), checksum=sha256(body.encode()), content=body,
                                    staging_file=f"{source_of(url)}/{dataset}/prepared/{n}.json"))
    return n
