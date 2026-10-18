"""Market groups as group markets in the directory (1018-2043): group per source event, grouped listing, members on demand.

Prepared raw rows only: no network, no LLM.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api import app, get_session
from backend.app.directory.build import build_directory
from backend.app.directory.groups import list_groups
from backend.app.directory.models import DirectoryMarket, DirectoryRun
from backend.app.directory.service import list_markets
from backend.tests.eui_helpers import memory_session
from backend.tests.test_market_directory import NOW
from backend.tests.test_raw_layer_directory import raw

APP = Path(__file__).resolve().parents[1] / "app"
NOW_FN = lambda: NOW  # noqa: E731


def bracket(i: int) -> dict:
    return {"id": f"spx-{i}", "question": f"S&P 500 between {5000 + 100 * i} and {5100 + 100 * i} on Dec 31?", "startDate": "2026-01-02T00:00:00Z",
            "endDate": "2026-12-31T00:00:00Z", "closed": i == 0, "outcomePrices": "[\"0.1\", \"0.9\"]"}


def prepared(session):
    raw(session, "polymarket", "directory_events", [
        {"id": "e-spx", "title": "S&P 500 on Dec 31", "tags": [{"label": "Finance"}], "markets": [bracket(i) for i in range(30)]},
        {"id": "e-solo", "title": "Recession in 2026?", "markets": [{"id": "rec", "question": "Recession in 2026?", "startDate": "2025-12-01T00:00:00Z"}]},
    ], 1)
    raw(session, "kalshi", "directory_events", {"events": [{"event_ticker": "KXINXD-26DEC31", "title": "S&P 500 close Dec 31", "markets": [
        {"ticker": "KXINXD-26DEC31-B5000", "title": "5000-5100", "status": "active"},
        {"ticker": "KXINXD-26DEC31-B5100", "title": "5100-5200", "status": "active"}]}]}, 2)
    raw(session, "kalshi", "directory_markets", {"markets": [{"ticker": "KXINXD-26DEC31-B5200", "event_ticker": "KXINXD-26DEC31", "title": "5200-5300"},
                                                             {"ticker": "LONE", "title": "Lone market"}]}, 3)


@pytest.fixture()
def session():
    s = memory_session()
    prepared(s)
    build_directory(s, "test", now=NOW_FN)
    return s


def test_markets_carry_their_market_group_from_the_source_event(session):
    r = {m.external_id: m for m in session.scalars(select(DirectoryMarket))}
    assert (r["spx-3"].group_key, r["spx-3"].group_title) == ("e-spx", "S&P 500 on Dec 31")
    assert (r["KXINXD-26DEC31-B5000"].group_key, r["KXINXD-26DEC31-B5000"].group_title) == ("KXINXD-26DEC31", "S&P 500 close Dec 31")
    assert r["KXINXD-26DEC31-B5200"].group_key == "KXINXD-26DEC31"  # /markets answer: ticker of the event, no title
    assert r["LONE"].group_key is None


def test_hundreds_of_brackets_become_one_group_market(session):
    res = list_groups(session)
    by_key = {(g["source"], g["key"]): g for g in res["groups"]}
    spx = by_key[("polymarket", "e-spx")]
    assert res["total"] == 4 and len(by_key) == 4
    assert spx["title"] == "S&P 500 on Dec 31" and spx["markets"] == 30 and not spx["single"]
    assert spx["statuses"] == {"active": 29, "expired": 1, "resolved": 0} and spx["end_date"].startswith("2026-12-31")
    assert by_key[("kalshi", "KXINXD-26DEC31")]["markets"] == 3
    assert by_key[("kalshi", "market:LONE")] | {"title": "Lone market", "markets": 1, "single": True} == by_key[("kalshi", "market:LONE")]


def test_group_filters_and_paging(session):
    assert [g["key"] for g in list_groups(session, q="dec 31", source="kalshi")["groups"]] == ["KXINXD-26DEC31"]
    assert list_groups(session, status="expired")["groups"][0]["markets"] == 1
    page = list_groups(session, limit=1, offset=1)
    assert page["total"] == 4 and len(page["groups"]) == 1


def test_members_of_one_group_market_are_listed_on_demand(session):
    spx = list_markets(session, source="polymarket", group="e-spx", limit=500)
    assert spx["total"] == 30 and {m["group_key"] for m in spx["markets"]} == {"e-spx"}
    assert [m["external_id"] for m in list_markets(session, source="kalshi", group="market:LONE")["markets"]] == ["LONE"]
    assert list_markets(session)["total"] == 35  # the plain listing stays unchanged


def test_markets_of_an_older_build_get_their_group_on_the_next_build(session):
    old = session.scalar(select(DirectoryMarket).where(DirectoryMarket.external_id == "rec"))
    old.group_key = old.group_title = None
    for run in session.scalars(select(DirectoryRun)):
        run.summary = {k: v for k, v in run.summary.items() if k != "build_version"}  # build before market groups
    session.commit()
    summary = build_directory(session, "test", now=lambda: NOW + timedelta(hours=1))
    assert summary["build_version"] == 2 and summary["sources"]["polymarket"]["answers"] == 1
    assert session.get(DirectoryMarket, old.id).group_key == "e-solo"
    again = build_directory(session, "test", now=lambda: NOW + timedelta(hours=2))
    assert again["sources"]["polymarket"]["answers"] == 0  # re-read only once


def test_api_lists_group_markets_and_their_members(session, caplog):
    app.dependency_overrides[get_session] = lambda: session
    try:
        client = TestClient(app)
        with caplog.at_level("INFO"):
            res = client.get("/directory/groups", params={"source": "polymarket"}).json()
        members = client.get("/directory/markets", params={"source": "polymarket", "group": "e-spx", "limit": 500}).json()
        assert client.get("/directory/groups", params={"status": "nope"}).status_code == 422
    finally:
        app.dependency_overrides.clear()
    assert res["total"] == 2 and {g["key"] for g in res["groups"]} == {"e-spx", "e-solo"}
    assert members["total"] == 30 and "directory groups listed" in caplog.text


def test_migration_adds_group_columns(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    db_url = f"sqlite:///{tmp_path / 'g.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    assert {"group_key", "group_title"} <= {c["name"] for c in inspect(create_engine(db_url)).get_columns("market_directory")}


def test_directory_tab_shows_group_markets_by_default_with_logged_actions():
    js = (APP / "static" / "directory.js").read_text(encoding="utf-8")
    assert 'view: "groups"' in js and "/directory/groups?" in js and "Gruppenmärkte" in js
    assert '"directory.group." + (open ? "close" : "open")' in js and "view: mdState.view" in js
