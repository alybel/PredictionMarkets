"""Agent mode "Metriken finden" (1016-2013-ki-agent-schritt-1): catalog-only search, key validation, warning marks,
gaps, endpoints with a fake finder, bookmarks in the index builder, tab wiring. No network, no live LLM."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.assistant import AgentError
from backend.app.agent.finder import FINDER_MODEL, MIN_HISTORY_DAYS, ApiFinder, RuleFinder, mark, validate
from backend.app.agent.finder_api import get_finder
from backend.app.api import app, get_markets, get_session
from backend.app.fed.api import get_fed_source
from backend.app.metrics.service import block, create_basket
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_catalog import EmptySource, raw_points, store

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FED, SECOND, BASKET = "fed_front_linked", "fed_second_linked", "mf_basket"


def entry(key="m1", days=MIN_HISTORY_DAYS, status="validated"):
    return {"key": key, "name": key, "unit": "bp", "source": "s", "calculation": "Fed rate", "status": status, "status_reason": None,
            "history_days": days, "first_date": None, "last_date": None, "checks": [], "checks_ok": False}


def test_unknown_metric_key_is_dropped_and_logged(caplog):
    caplog.set_level(logging.WARNING)
    result = validate({"reply": "r", "matches": [{"key": "m1", "reason": "passt"}, {"key": "invented", "reason": "x"},
                                                  {"key": "m1", "reason": "doppelt"}], "gaps": []}, [entry()], "fake")
    assert [m["key"] for m in result.matches] == ["m1"] and result.matches[0]["reason"] == "passt"
    assert result.dropped == ["invented"]
    assert "dropped unknown metric keys" in caplog.text and "invented" in caplog.text


def test_short_history_warning_starts_below_180_days():
    assert MIN_HISTORY_DAYS == 180
    short, enough = mark(entry(days=179)), mark(entry(days=180))
    assert short["short_history"] and "179 Handelstage" in short["warnings"][0]
    assert not enough["short_history"] and enough["warnings"] == []


def test_blocked_metric_stays_in_the_list_with_mark():
    catalog = [entry("ok"), entry("bad", status="blocked") | {"status_reason": "Automatisch gesperrt: Lücken"}]
    result = validate({"reply": "", "matches": [{"key": "ok", "reason": ""}, {"key": "bad", "reason": ""}], "gaps": []}, catalog, "fake")
    bad = next(m for m in result.matches if m["key"] == "bad")
    assert len(result.matches) == 2 and bad["blocked"] and "Gesperrt: Automatisch gesperrt: Lücken" in bad["warnings"]


def test_gaps_are_taken_from_the_structured_answer():
    result = validate({"reply": "", "matches": [], "gaps": ["Inflation Eurozone", " ", "Ölpreis"]}, [entry()], "fake")
    assert result.gaps == ["Inflation Eurozone", "Ölpreis"]
    with pytest.raises(AgentError):
        validate("kein json", [entry()], "fake")


def test_api_finder_uses_haiku_with_structured_output_and_catalog_only():
    answer = {"reply": "ok", "matches": [{"key": "m1", "reason": "r"}], "gaps": []}
    calls = []
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: calls.append(kw) or SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))])))
    assert ApiFinder(client).propose("Fed", [entry()]) == answer
    assert calls[0]["model"] == FINDER_MODEL == "claude-haiku-5-5"
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"
    catalog = json.loads(calls[0]["messages"][0]["content"].split("<catalog>\n")[1].split("\n</catalog>")[0])
    assert catalog == [{"key": "m1", "name": "m1", "unit": "bp", "calculation": "Fed rate", "status": "validated", "history_days": 180}]


def test_rule_finder_matches_keywords_and_reports_uncovered_words():
    raw = RuleFinder().propose("Fed Inflation", [entry()])
    assert [m["key"] for m in raw["matches"]] == ["m1"] and raw["gaps"] == ["inflation"]


class FakeFinder:
    name = "fake-haiku"

    def __init__(self, answer):
        self.answer, self.ideas = answer, []

    def propose(self, idea, catalog):
        self.ideas.append(idea)
        return self.answer


@pytest.fixture()
def env():
    session = memory_session()
    store(session, raw_points())
    create_basket(session, BASKET, "Rezessions-Korb", "", [{"source": "polymarket", "external_id": "pm-x", "title": "Recession?"}], None)
    finder = FakeFinder({"reply": "Zwei passen.", "gaps": ["Inflation Eurozone"],
                         "matches": [{"key": FED, "reason": "Leitzins"}, {"key": BASKET, "reason": "Rezession"}, {"key": "ghost", "reason": "?"}]})
    app.dependency_overrides.update({get_session: lambda: session, get_fed_source: EmptySource, get_markets: lambda: [],
                                     get_finder: lambda: finder})
    client = TestClient(app)
    yield client, session, finder
    app.dependency_overrides.clear()


def test_find_endpoint_returns_hits_status_and_gaps(env, caplog):
    client, session, finder = env
    block(session, BASKET, "Test")
    caplog.set_level(logging.INFO)
    body = client.post("/agent/find", json={"idea": "Geldpolitik und Rezession"}).json()
    assert finder.ideas == ["Geldpolitik und Rezession"]
    hits = {m["key"]: m for m in body["matches"]}
    assert list(hits) == [FED, BASKET] and body["dropped"] == ["ghost"] and body["gaps"] == ["Inflation Eurozone"]
    assert hits[BASKET]["status"] == "blocked" and hits[BASKET]["blocked"]
    assert hits[FED]["reason"] == "Leitzins" and hits[FED]["history_days"] > 0 and hits[FED]["checks"]
    assert hits[FED]["short_history"] is (hits[FED]["history_days"] < 180)
    assert body["min_history_days"] == 180 and body["catalog_size"] == 3 and body["model"] == "fake-haiku"
    assert "metric finder request" in caplog.text and "2 of 3 catalog metrics matched" in caplog.text and "ghost" in caplog.text
    assert client.post("/agent/find", json={"idea": "  "}).status_code == 422


def test_find_endpoint_maps_agent_errors_to_502(env):
    client, _, finder = env
    finder.answer = "kaputt"
    assert client.post("/agent/find", json={"idea": "x"}).status_code == 502


def test_bookmarked_metric_is_selectable_in_the_index_builder(env, caplog):
    client, _, _ = env
    for key in (FED, SECOND):
        assert client.post(f"/metrics/{key}/approve").status_code == 200
    caplog.set_level(logging.INFO)
    assert client.post("/agent/bookmarks", json={"key": SECOND}).json()["keys"] == [SECOND]
    assert client.post("/agent/bookmarks", json={"key": BASKET}).status_code == 201
    assert client.post("/agent/bookmarks", json={"key": "ghost"}).status_code == 404
    assert "metric fed_second_linked bookmarked" in caplog.text
    sel = client.get("/builder/metrics").json()
    assert sel["metrics"][0]["key"] == SECOND and sel["metrics"][0]["bookmarked"]
    assert not next(m for m in sel["metrics"] if m["key"] == FED)["bookmarked"]
    assert [p["key"] for p in sel["bookmarked_pending"]] == [BASKET]  # bookmarked, not validated: shown, not selectable
    assert client.post("/builder/indices", json={"name": "Vorgemerkt", "components": [{"key": SECOND, "weight": 1}]}).status_code == 201
    assert client.post("/agent/find", json={"idea": "x"}).json()["matches"][0]["bookmarked"] is False
    assert client.delete(f"/agent/bookmarks/{SECOND}").status_code == 204
    assert client.get("/agent/bookmarks").json()["keys"] == [BASKET]


def test_agent_tab_has_both_modes_and_keeps_the_blueprint_chat():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for needle in ('data-ag-mode="find"', ">Metriken finden<", 'data-ag-mode="create"', ">Metriken anlegen<", 'id="ag-create-panel"',
                   'id="ag-send"', 'id="af-handover"', 'id="mi-pending"', '<script src="/static/finder.js"></script>'):
        assert needle in html
    assert html.index('id="ag-create-panel"') < html.index('id="ag-chat"')  # blueprint chat lives in "Metriken anlegen"
    src = (STATIC / "finder.js").read_text(encoding="utf-8")
    for event in ("agent.mode", "agent.find.send", "agent.find.reply", "agent.find.failed", "agent.bookmark.click", "agent.find.handover"):
        assert f'"{event}"' in src
    assert '$("ag-input").value' in src  # gaps are handed over to the blueprint chat input
    if shutil.which("node"):
        for name in ("finder.js", "metricindex.js"):
            subprocess.run(["node", "--check", str(STATIC / name)], check=True)
