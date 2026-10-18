"""Raw data layer as the only API access, market directory built from raw_responses only (1017-1426).

Fake getters and prepared raw rows: no network, no LLM.
"""

import json
import logging
import re
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from backend.app.api import app, get_session
from backend.app.db import Base, make_session_factory
from backend.app.directory.build import build_directory
from backend.app.directory.models import DirectoryMarket, DirectoryRun
from backend.app.rawdata import jobs
from backend.app.rawdata.api import get_raw_background, get_raw_getter, get_raw_staging
from backend.app.rawdata.models import DONE, FAILED, RawFetchJob
from backend.app.risk.api import get_risk_classifier
from backend.app.staging.models import RawResponse
from backend.tests.eui_helpers import memory_session
from backend.tests.test_market_directory import K_CLOSED, K_HIST, K_OPEN, NOW, PM_OPEN, PM_RESOLVED, FakeSources
from backend.tests.test_risk_topics import FakeHaiku

APP = Path(__file__).resolve().parents[1] / "app"
NOW_FN = lambda: NOW  # noqa: E731


def raw(session, source, dataset, data, n, fetched=NOW):
    body = data if isinstance(data, str) else json.dumps(data)
    session.add(RawResponse(source=source, dataset=dataset, url="u", request_key=f"k{n}", request_id=f"r{n}", fetched_at=fetched,
                            checksum=f"c{n}", content=body, staging_file=f"{source}/{dataset}/2026-10-17/f{n}.json"))
    session.commit()


def prepared(session):
    raw(session, "polymarket", "directory_events", [{"id": "e1", "tags": [{"label": "Finance"}], "markets": [PM_OPEN, PM_RESOLVED]}], 1)
    raw(session, "kalshi", "directory_events", {"events": [{"title": "S&P", "category": "Financials", "markets": [K_OPEN, K_CLOSED]}]}, 2)
    raw(session, "kalshi", "directory_historical_markets", {"markets": [K_HIST]}, 3)


def snapshot(session):
    return sorted(session.execute(select(DirectoryMarket.source, DirectoryMarket.external_id, DirectoryMarket.title, DirectoryMarket.status,
                                         DirectoryMarket.listed_at, DirectoryMarket.end_date, DirectoryMarket.tags,
                                         DirectoryMarket.updated_at)).all())


@pytest.fixture()
def session():
    return memory_session()


@pytest.fixture()
def factory(tmp_path):
    """File database: a background thread gets its own connection."""
    engine = create_engine(f"sqlite:///{tmp_path / 'raw.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def test_build_from_prepared_raw_responses_gives_markets_status_and_dates(session):
    prepared(session)
    summary = build_directory(session, "test", now=NOW_FN)
    r = {m.external_id: m for m in session.scalars(select(DirectoryMarket))}
    assert {k: (v.source, v.status) for k, v in r.items()} == {
        "pm-1": ("polymarket", "active"), "pm-2": ("polymarket", "resolved"), "KXINXD-26DEC": ("kalshi", "active"),
        "KXINXD-25JUN": ("kalshi", "expired"), "KXINX-22DEC": ("kalshi", "resolved")}
    assert r["pm-2"].listed_at.date().isoformat() == "2023-06-01" and r["pm-2"].end_date.date().isoformat() == "2024-12-31"
    assert r["KXINXD-26DEC"].tags == ["Financials"] and r["pm-1"].tags == ["Finance"]
    assert summary["sources"]["kalshi"]["answers"] == 2 and summary["sources"]["kalshi"]["new"] == 3 and summary["raw_upto"] == 3


def test_repeated_build_from_the_same_raw_data_changes_nothing(session):
    prepared(session)
    build_directory(session, "test", now=NOW_FN)
    before = snapshot(session)
    again = build_directory(session, "test", now=lambda: NOW + timedelta(hours=1))
    full = build_directory(session, "test", now=lambda: NOW + timedelta(hours=2), from_start=True)
    assert snapshot(session) == before
    assert again["sources"]["kalshi"]["answers"] == 0  # nothing newer than the last build
    assert full["sources"]["kalshi"]["unchanged"] == 3 and full["sources"]["polymarket"]["new"] == full["sources"]["kalshi"]["changed"] == 0


def test_broken_raw_answer_is_counted_logged_and_the_build_goes_on(session, caplog):
    raw(session, "kalshi", "directory_events", "<html>502</html>", 8)
    raw(session, "polymarket", "directory_events", {"unexpected": True}, 9)
    prepared(session)
    with caplog.at_level(logging.WARNING):
        summary = build_directory(session, "test", now=NOW_FN)
    assert summary["sources"]["kalshi"]["broken"] == 1 and summary["sources"]["polymarket"]["broken"] == 1
    assert session.scalar(select(func.count()).select_from(DirectoryMarket)) == 5
    assert "unusable, skipped" in caplog.text


def test_refresh_request_writes_staging_and_raw_then_builds_the_directory(factory, tmp_path, caplog):
    sources = FakeSources()
    with caplog.at_level(logging.INFO):
        job = jobs.request_refresh(factory, "directory", "test", get_raw=sources, staging=tmp_path / "staging", now=NOW_FN, background=False)
    assert job["status"] == DONE and not job["running"] and not job["duplicate"]
    assert len(list((tmp_path / "staging").rglob("*.meta.json"))) == 7 and sources.calls
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(RawResponse)) == 7
        assert s.scalar(select(func.count()).select_from(DirectoryMarket)) == 5
        run = s.scalar(select(DirectoryRun))
    assert job["summary"]["after"]["directory"]["run_id"] == run.id and run.summary["job_id"] == job["id"]
    assert job["summary"]["raw"]["new"] == 7 and job["progress"]["label"] == "fertig"
    assert "trigger test" in caplog.text and f"raw job #{job['id']} directory finished" in caplog.text


def test_second_request_during_a_running_fetch_starts_no_second_fetch(factory, tmp_path):
    release, entered, calls = threading.Event(), threading.Event(), []

    def slow(url, params):
        calls.append(url)
        entered.set()
        release.wait(10)
        return None  # failed request: the job still finishes

    first = jobs.request_refresh(factory, "directory", "test", get_raw=slow, staging=tmp_path / "staging")
    assert first["running"] and entered.wait(5)  # returned at once, fetch runs in the background
    second = jobs.request_refresh(factory, "directory", "test", get_raw=slow, staging=tmp_path / "staging")
    assert second["duplicate"] and second["id"] == first["id"]
    release.set()
    jobs.wait(first["id"])
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(RawFetchJob)) == 1
        assert s.get(RawFetchJob, first["id"]).status == DONE
    assert len(calls) == 7  # one request per stream of the single job
    third = jobs.request_refresh(factory, "directory", "test", get_raw=FakeSources(), staging=tmp_path / "staging", background=False)
    assert not third["duplicate"] and third["id"] != first["id"]


def test_stale_running_job_is_closed_and_blocks_nothing(factory, tmp_path):
    with factory() as s:
        s.add(RawFetchJob(job="directory", trigger="test", status="running", active=True, started_at=NOW - timedelta(hours=2),
                          heartbeat_at=NOW - timedelta(hours=1)))
        s.commit()
    job = jobs.request_refresh(factory, "directory", "test", get_raw=FakeSources(), staging=tmp_path / "staging", now=NOW_FN, background=False)
    assert not job["duplicate"] and job["status"] == DONE
    with factory() as s:
        assert s.scalar(select(RawFetchJob.status).where(RawFetchJob.id != job["id"])) == FAILED


def test_directory_package_has_no_direct_http_call():
    for path in (APP / "directory").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(requests|httpx|urllib|aiohttp|http\.client)\b", text, re.M), path.name
        assert "http_get_raw" not in text, path.name


# ---- API ---------------------------------------------------------------------------------------

@pytest.fixture()
def api(session, tmp_path):
    sources = FakeSources()
    app.dependency_overrides.update({get_session: lambda: session, get_raw_getter: lambda: sources, get_raw_staging: lambda: tmp_path / "staging",
                                     get_raw_background: lambda: False, get_risk_classifier: FakeHaiku})
    yield TestClient(app), session
    app.dependency_overrides.clear()


def test_api_request_then_workshop_step_one_counts_the_rebuilt_directory(api, caplog):
    client, session = api
    pilot = next(t for t in client.get("/risk/topics").json() if t["slug"] == "marktcrash")["id"]
    assert client.get("/raw/requests/latest", params={"job": "directory"}).json() is None
    with caplog.at_level(logging.INFO):
        r = client.post("/raw/requests", json={"job": "directory", "trigger": "workshop"})
    assert r.status_code == 202 and r.json()["status"] == DONE
    assert "raw refresh requested via API: job directory, trigger workshop" in caplog.text
    job = client.get(f"/raw/requests/{r.json()['id']}").json()
    assert job["summary"]["after"]["directory"]["sources"]["kalshi"]["new"] == 3
    assert client.get("/raw/requests/latest", params={"job": "directory"}).json()["id"] == job["id"]
    step = client.get(f"/workshop/topics/{pilot}/status").json()["steps"][0]
    assert step["counts"]["markets"] == 5 and step["counts"]["last_run"] is not None
    assert client.post("/directory/rebuild").json()["sources"]["kalshi"]["answers"] == 0
    assert client.post("/raw/requests", json={"job": "unknown"}).status_code == 404
    assert client.get("/raw/requests/999").status_code == 404
    assert client.post("/raw/requests", json={"job": "directory", "trigger": "x" * 40}).status_code == 422


def test_workshop_button_only_requests_raw_data_and_logs_clicks():
    js = (APP / "static" / "workshop.js").read_text(encoding="utf-8")
    assert '"Rohdaten aktualisieren"' in js and '"/raw/requests"' in js and '"/directory/load"' not in js
    for event in ("workshop.raw.requested", "workshop.raw.done", "workshop.raw.failed", "workshop.click"):
        assert f'"{event}"' in js


def test_migration_carries_the_resume_positions_of_the_last_directory_run(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    db_url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(APP.parent / "alembic.ini"))
    command.upgrade(cfg, "0020")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO market_directory_runs (trigger, started_at, finished_at, summary) VALUES "
                          "('daily', '2026-10-16 08:00:00', '2026-10-16 08:10:00', :s)"), {"s": json.dumps({"positions": {"polymarket_all": "c9"}})})
    command.upgrade(cfg, "head")
    with make_session_factory(engine)() as s:
        job = s.scalar(select(RawFetchJob))
    assert job.job == "directory" and job.status == DONE and job.summary == {"positions": {"polymarket_all": "c9"}} and job.active is None
    engine.dispose()
