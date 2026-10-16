"""Staging folder and idempotent raw layer (1015-2120, part 2): fetch to files, staging -> raw, load log.
Fixture payloads only, no network."""

import json
import logging
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from backend.app.db import Base, make_session_factory
from backend.app.fed.fetch import dataset_of, fetch_fed
from backend.app.schema_state import alembic_config
from backend.app.staging import raw_loader
from backend.app.staging.files import Stager, _publish, list_staged, meta_path, read_staged
from backend.app.staging.models import FAILED, LOADED, RawLoadLog, RawResponse
from backend.app.staging.raw_loader import insert_ignore, load_staging, raw_values
from backend.tests.fed_fixtures import NOW, POLY_PRICES, FakeApi


def raw_getter(api=None):
    api = api or FakeApi()
    return lambda url, params=None: json.dumps(api(url, params)).encode()


def ticking(start=NOW, step=timedelta(seconds=1)):
    state = {"t": start}

    def now():
        state["t"] += step
        return state["t"]
    return now


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def factory_for(url: str | None = None, **kw):
    """Session factory on ``url``; without one an in-memory database with all tables."""
    if url:
        return make_session_factory(create_engine(url, **kw))
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def count(factory, model) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model))


@pytest.fixture
def staged(tmp_path):
    root = tmp_path / "staging"
    fetch_fed(root, raw_getter(), ticking())
    return root


# ---- fetch -> staging ------------------------------------------------------------------------

def test_fetch_writes_answers_unchanged_with_url_and_fetch_time(staged):
    files = list_staged(staged)
    assert {f.relative_to(staged).parts[:2] for f in files} == {
        ("kalshi", "events"), ("kalshi", "markets"), ("kalshi", "candlesticks"), ("polymarket", "events"), ("polymarket", "prices_history")}
    api = FakeApi()
    for body in files:
        meta = json.loads(meta_path(body).read_bytes())
        assert body.read_bytes() == json.dumps(api(meta["url"], meta["params"])).encode()  # byte for byte
        assert body.parent.name == meta["fetched_at"][:10] and meta["url"].startswith("https://")


def test_identical_fetch_writes_no_second_file(staged):
    before = snapshot(staged)
    summary = fetch_fed(staged, raw_getter(), ticking(NOW + timedelta(days=1)))  # next day, end_ts differs
    assert summary["written"] == 0 and summary["unchanged"] == summary["requests"] > 0
    assert snapshot(staged) == before


def test_changed_content_creates_a_new_file_and_keeps_the_old(staged, monkeypatch):
    before = snapshot(staged)
    monkeypatch.setitem(POLY_PRICES, "tok-0", (0.64, 0.71))
    summary = fetch_fed(staged, raw_getter(), ticking(NOW + timedelta(days=1)))
    after = snapshot(staged)
    assert summary["written"] == 1
    assert all(after[k] == v for k, v in before.items())  # nothing overwritten
    new = [k for k in after if k not in before and not k.endswith(".meta.json")]
    assert len(new) == 1 and new[0].startswith("polymarket/prices_history/2025-12-13/") and b"0.71" in after[new[0]]


def test_staging_files_are_never_overwritten(tmp_path):
    target = tmp_path / "a.json"
    assert _publish(target, b"first") and not _publish(target, b"second")
    assert target.read_bytes() == b"first" and [p.name for p in tmp_path.iterdir()] == ["a.json"]
    stager = Stager(tmp_path / "s")
    one = stager.write("kalshi", "events", "https://x", None, b"{}", NOW)
    two = Stager(tmp_path / "s").write("kalshi", "events", "https://x", None, b"{}", NOW + timedelta(1))  # fresh index
    assert one.written and not two.written and two.path == one.path


def test_dataset_of_maps_every_fed_url():
    assert dataset_of("https://api.elections.kalshi.com/trade-api/v2/historical/markets/X/candlesticks") == ("kalshi", "historical_candlesticks")
    assert dataset_of("https://api.elections.kalshi.com/trade-api/v2/historical/markets") == ("kalshi", "historical_markets")
    with pytest.raises(ValueError):
        dataset_of("https://example.com/other")


# ---- staging -> raw --------------------------------------------------------------------------

def test_load_brings_files_1_to_1_into_raw_and_second_run_loads_nothing(staged, caplog):
    factory = factory_for()
    with caplog.at_level(logging.INFO):
        first = load_staging(factory, staged)
    files = list_staged(staged)
    assert first["found"] == first["new"] == len(files) and first["failed"] == 0
    assert "found %d, new %d, skipped 0, failed 0" % (len(files), len(files)) in caplog.text
    with factory() as s:
        row = s.scalar(select(RawResponse).where(RawResponse.dataset == "events", RawResponse.source == "kalshi"))
        assert row.content == (staged / row.staging_file).read_text() and row.url.endswith("/events")
        assert {e.status for e in s.scalars(select(RawLoadLog))} == {LOADED}
    second = load_staging(factory, staged)
    assert second["new"] == 0 and second["skipped"] == len(files)
    assert count(factory, RawResponse) == count(factory, RawLoadLog) == len(files)


def test_duplicate_checksum_is_rejected_by_the_db_and_skipped_by_the_loader(staged):
    factory = factory_for()
    load_staging(factory, staged)
    values = raw_values(read_staged(staged, list_staged(staged)[0]))
    with factory() as s:
        assert insert_ignore(s, RawResponse, values) == 0  # silent skip
        with pytest.raises(IntegrityError):  # same request, time and checksum under another file name
            s.add(RawResponse(**{**values, "staging_file": "kalshi/events/x/copy.json"}))
            s.flush()


def test_abort_inside_a_file_leaves_no_half_rows_and_next_run_continues(staged, monkeypatch):
    factory = factory_for()
    calls = {"n": 0}
    original = raw_loader.log_entry

    def crash_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt  # raw row of file 2 already inserted in the open transaction
        return original(*args, **kwargs)

    monkeypatch.setattr(raw_loader, "log_entry", crash_on_second)
    with pytest.raises(KeyboardInterrupt):
        load_staging(factory, staged)
    assert count(factory, RawResponse) == count(factory, RawLoadLog) == 1
    monkeypatch.setattr(raw_loader, "log_entry", original)
    rerun = load_staging(factory, staged)
    assert rerun["new"] == len(list_staged(staged)) - 1 and rerun["skipped"] == 1
    assert count(factory, RawResponse) == len(list_staged(staged))


def test_parallel_loads_give_the_same_data_without_duplicates(staged, tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'raw.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(alembic_config(), "head")  # uniqueness rules from the migration
    factories = [factory_for(db_url, connect_args={"timeout": 30}) for _ in range(2)]
    results, errors = [], []

    def run(f):
        try:
            results.append(load_staging(f, staged))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f,)) for f in factories]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    files = len(list_staged(staged))
    assert not errors and sum(r["new"] for r in results) == files and all(r["failed"] == 0 for r in results)
    assert count(factories[0], RawResponse) == count(factories[0], RawLoadLog) == files
    with factories[0]() as s:
        assert s.scalar(select(func.count(func.distinct(RawResponse.staging_file)))) == files


def test_broken_staging_file_is_logged_and_skipped_the_rest_is_loaded(staged, caplog):
    day = staged / "kalshi" / "events" / "2025-12-12"
    good = list_staged(staged)
    (day / "zzbroken_000000000000_deadbeef.json").write_text("{not json")
    meta = json.loads(meta_path(good[0]).read_bytes())
    meta_path(day / "zzbroken_000000000000_deadbeef.json").write_text(json.dumps(meta))
    (day / "zzorphan_000000000000_deadbeef.json").write_text("{}")  # no meta file
    factory = factory_for()
    with caplog.at_level(logging.WARNING):
        summary = load_staging(factory, staged)
    assert summary["failed"] == 2 and summary["new"] == len(good)
    assert "broken staging file kalshi/events/2025-12-12/zzbroken" in caplog.text
    with factory() as s:
        failed = {e.staging_file: e.error for e in s.scalars(select(RawLoadLog).where(RawLoadLog.status == FAILED))}
    assert {Path(k).name[:8] for k in failed} == {"zzbroken", "zzorphan"}
    assert any("checksum" in e for e in failed.values()) and any("missing file" in e for e in failed.values())
    assert count(factory, RawResponse) == len(good)


def test_migration_creates_raw_tables_with_unique_rules(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(alembic_config(), "head")
    inspector = inspect(create_engine(db_url))
    raw = {u["name"]: u["column_names"] for u in inspector.get_unique_constraints("raw_responses")}
    assert raw == {"uq_raw_responses_staging_file": ["staging_file"],
                   "uq_raw_responses_request_time_checksum": ["request_id", "fetched_at", "checksum"]}
    assert [u["column_names"] for u in inspector.get_unique_constraints("raw_load_log")] == [["staging_file"]]
    assert {c["name"] for c in inspector.get_columns("raw_responses")} >= {"content", "source", "dataset", "fetched_at", "checksum"}
    assert "fed_prices" in inspector.get_table_names()  # existing tables stay
