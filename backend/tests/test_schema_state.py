"""Schema auto-migration on start, schema state in /health, logging of unhandled errors."""

import logging

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend.app.api import app
from backend.app.deps import get_session
from backend.app.logging_setup import LOG_FORMAT
from backend.app.schema_state import alembic_config, head_revision


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture()
def log_file(tmp_path):
    """Same format as logs/app.log, written to a temp file."""
    path = tmp_path / "app.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.INFO)
    yield path
    root.removeHandler(handler)
    root.setLevel(previous)
    handler.close()


def _session_for(url):
    from backend.app.db import make_session_factory

    return make_session_factory(create_engine(url, connect_args={"check_same_thread": False}))()


def test_start_migrates_0008_to_head_and_keeps_rows(db_url, log_file):
    command.upgrade(alembic_config(), "0008")
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO index_rules (index_name, categories, keywords, min_liquidity, min_price, max_price, created_at) "
            "VALUES ('legacy', '[]', '[]', 10000.0, 0.03, 0.97, '2026-10-01 00:00:00')"
        ))

    with TestClient(app):  # runs the lifespan
        pass

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head_revision()
        assert conn.execute(text("SELECT index_name FROM index_rules")).scalars().all() == ["legacy"]
        conn.execute(text("SELECT COUNT(*) FROM fed_markets")).scalar()  # table of head exists
    log = log_file.read_text()
    assert "Schema migration started: 0008 -> " in log
    assert f"Schema state: revision {head_revision()}, head {head_revision()}, up_to_date=True" in log


def test_start_logs_schema_state_when_current(db_url, log_file):
    command.upgrade(alembic_config(), "head")
    with TestClient(app):
        pass
    log = log_file.read_text()
    assert "Schema state: revision" in log and "up_to_date=True" in log
    assert "Schema migration started" not in log


def test_health_warns_on_outdated_schema(db_url):
    command.upgrade(alembic_config(), "0008")
    session = _session_for(db_url)
    app.dependency_overrides[get_session] = lambda: session
    try:
        schema = TestClient(app).get("/health").json()["schema"]
    finally:
        app.dependency_overrides.clear()
        session.close()
    assert schema["current"] == "0008" and schema["head"] == head_revision()
    assert schema["up_to_date"] is False
    assert "Schema veraltet" in schema["warning"]


def test_health_has_no_warning_on_head(db_url):
    command.upgrade(alembic_config(), "head")
    session = _session_for(db_url)
    app.dependency_overrides[get_session] = lambda: session
    try:
        schema = TestClient(app).get("/health").json()["schema"]
    finally:
        app.dependency_overrides.clear()
        session.close()
    assert schema["up_to_date"] is True and schema["warning"] is None


def test_unhandled_exception_returns_500_and_logs_traceback(log_file):
    def broken_session():
        raise RuntimeError("boom-for-test")

    app.dependency_overrides[get_session] = broken_session
    try:
        res = TestClient(app).get("/health?probe=1")
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 500
    log = log_file.read_text()
    assert "GET /health?probe=1 -> 500 unhandled exception" in log
    assert "Traceback (most recent call last)" in log and "RuntimeError: boom-for-test" in log
