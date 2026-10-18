"""Mode "Metriken anlegen" (1017-1107, Risiko-Index 4/7): proposals only from the accepted markets of a topic's review list,
with their reading; rejected/unknown refs dropped and logged, structured acceptance as draft with checks, the #58 chain metrics
keep their values. Fake models and seeded rows: no network, no live LLM."""

import json
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.agent import creator_api
from backend.app.agent.creator import CREATOR_MODEL, ApiCreator, CliCreator, RuleCreator, create
from backend.app.agent.topic_pool import topic_pool
from backend.app.api import app, get_session
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.fed.api import get_fed_source
from backend.app.metrics.models import CustomMetric
from backend.app.metrics.service import create_structured
from backend.app.quality.pit import BACKFILL
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_catalog import EmptySource
from backend.tests.topic_helpers import review_list

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TODAY = datetime.now(timezone.utc).date()
# (source, id, title, end: days ago (negative = future), base price, review reading, review status)
MAR = ("kalshi", "FED-MAR", "Will the Fed cut rates in March 2026?", 100, 0.3, "low", "accepted")
MAY = ("kalshi", "FED-MAY", "Will the Fed cut rates in May 2026?", -60, 0.6, "low", "accepted")
REC = ("polymarket", "pm-rec", "US recession this year?", -200, 0.2, "uncertainty", "accepted")
REJ = ("polymarket", "pm-rej", "S&P 500 crash this year?", -200, 0.4, "high", "rejected")
NEW = ("polymarket", "pm-new", "Nasdaq crash this year?", -200, 0.4, "high", "proposed")
SPANS = {"FED-MAR": range(100, 301), "FED-MAY": range(1, 151), "pm-rec": range(1, 31), "pm-rej": range(1, 400), "pm-new": range(1, 400)}


def _end(days_ago: int) -> datetime:
    return datetime.combine(TODAY - timedelta(days_ago), datetime.min.time(), tzinfo=timezone.utc)


def price(base: float, n: int) -> float:
    return base + 0.001 * (n % 7)


def seed(session, review: bool = True):
    """Stored history of five markets; topic review list: two Fed links + recession accepted, one rejected, one undecided."""
    run = SnapshotRun(snapshot_date=TODAY, status="ok")
    session.add(run)
    session.flush()
    for source, eid, title, end, base, *_ in (MAR, MAY, REC, REJ, NEW):
        for n in SPANS[eid]:
            session.add(MarketSnapshot(run_id=run.id, snapshot_date=TODAY - timedelta(n), source=source, external_id=eid, title=title,
                                       tags=["Economics"], price=price(base, n), volume_24h_usd=1000.0, exclusion_reason=BACKFILL,
                                       fetched_at=None, end_date=_end(end)))
    session.commit()
    rows = [(src, eid, title, reading, status, _end(end)) for src, eid, title, end, _, reading, status in (MAR, MAY, REC, REJ, NEW)]
    return review_list(session, rows if review else [])


class FakeCreator:
    name = "fake-sonnet"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def propose(self, idea, ctx):
        self.calls.append((idea, ctx))
        return self.answer(ctx) if callable(self.answer) else self.answer


def chain_answer(ctx):
    return {"reply": "Drei Vorschläge.",
            "proposals": [{"name": "Fed-Senkung Front", "kind": "chain", "refs": [ctx["chains"][0]["id"], "kalshi:invented"],
                           "rules": ["roll_end_date", "smooth_7d"], "free_rules": ["Kalshi doppelt gewichten"], "reason": "Lange Kette"},
                          {"name": "Rezession", "kind": "single", "refs": ["polymarket:pm-rec"], "rules": [], "free_rules": [], "reason": "Kurz"},
                          {"name": "Crash", "kind": "chain", "refs": ["polymarket:pm-rej", "polymarket:pm-new"], "rules": [], "free_rules": [],
                           "reason": "abgelehnt"}]}


@pytest.fixture()
def env():
    session = memory_session()
    topic = seed(session)
    fake = FakeCreator(chain_answer)
    app.dependency_overrides.update({get_session: lambda: session, creator_api.get_creator: lambda: fake, get_fed_source: EmptySource})
    yield TestClient(app), session, fake, topic
    app.dependency_overrides.clear()


def test_pool_holds_only_accepted_markets_with_their_reading():
    session = memory_session()
    pool = topic_pool(session, seed(session))
    assert {c.key for c in pool.candidates} == {"kalshi:FED-MAR", "kalshi:FED-MAY", "polymarket:pm-rec"}
    assert pool.readings == {"kalshi:FED-MAR": "down", "kalshi:FED-MAY": "down", "polymarket:pm-rec": "uncertainty"}
    assert len(pool.chains) == 1 and pool.chains[0].rolls == 1


def test_proposals_use_only_accepted_markets_with_the_review_reading(env, caplog):
    client, _, fake, topic = env
    with caplog.at_level(logging.INFO):
        body = client.post("/agent/propose", json={"topic_id": topic.id, "idea": "Fed-Zinssenkungen", "gaps": ["Rezession"]}).json()
    idea, ctx = fake.calls[0]
    assert "Nicht abgedeckt:\n- Rezession" in idea and ctx["topic"]["name"] == topic.name
    assert {m["id"] for m in ctx["markets"]} | {ch["id"] for ch in ctx["chains"]} == {"polymarket:pm-rec", ctx["chains"][0]["id"]}
    assert "pm-rej" not in json.dumps(ctx) and "pm-new" not in json.dumps(ctx)
    assert [r["key"] for r in ctx["rule_catalog"]] == ["direction", "roll_end_date", "front_chain", "no_gap_fill", "no_forward_fill"]
    assert [p["name"] for p in body["proposals"]] == ["Fed-Senkung Front", "Rezession"] and body["topic"]["id"] == topic.id
    chain, single = body["proposals"]
    assert chain["kind"] == "chain" and [(l["external_id"], l["direction"]) for l in chain["links"]] == [("FED-MAR", "down"), ("FED-MAY", "down")]
    assert chain["links"][0]["roll_on"] == (TODAY - timedelta(100)).isoformat() and chain["topic_id"] == topic.id
    assert chain["rules"] == ["direction", "roll_end_date", "no_gap_fill", "no_forward_fill"]
    assert chain["notes"] == ["Unbekannte Regel „smooth_7d“", "Kalshi doppelt gewichten"]
    a = chain["assessment"]
    assert a["trading_days"] == 300 and a["rolls"] == 1 and a["gap_days"] == 0 and a["liquidity_usd"] == 1000.0 and not a["short_history"]
    assert single["links"][0]["direction"] == "uncertainty" and single["reading_label"] == "Unsicherheit"
    assert single["assessment"]["short_history"] and "Nur 30 Handelstage" in single["assessment"]["warnings"][0]
    reasons = {d["ref"]: d["reason"] for d in body["dropped"]}
    assert reasons == {"kalshi:invented": "unbekannter Markt", "polymarket:pm-rej": "abgelehnt", "polymarket:pm-new": "noch nicht angenommen"}
    assert "outside the accepted markets" in caplog.text and "no accepted market" in caplog.text and "outside the catalog" in caplog.text
    assert "metric proposal request" in caplog.text and "2 proposal(s)" in caplog.text


def test_without_accepted_markets_nothing_is_proposed(caplog):
    session = memory_session()
    topic = seed(session, review=False)
    fake = FakeCreator(chain_answer)
    with caplog.at_level(logging.WARNING):
        result, pool = create(session, fake, topic, "Fed", TODAY)
    assert not pool.candidates and not result.proposals and "Prüfliste" in result.reply and fake.calls == []
    assert "no accepted market in the review list" in caplog.text


def test_rule_fallback_proposes_from_accepted_markets():
    session = memory_session()
    result, _ = create(session, RuleCreator(), seed(session), "fed cut rates", TODAY)
    assert result.proposals[0]["kind"] == "chain" and result.model == "rule-fallback"
    assert {l["external_id"] for p in result.proposals for l in p["links"]} <= {"FED-MAR", "FED-MAY", "pm-rec"}


def test_accept_stores_structured_draft_and_runs_checks(env, caplog):
    client, session, _, topic = env
    proposal = client.post("/agent/propose", json={"topic_id": topic.id, "idea": "Fed"}).json()["proposals"][0]
    proposal["links"][0]["roll_on"] = "1999-01-01"  # end dates come from the server, never from the page
    proposal["links"][0]["direction"] = "up"         # the reading comes from the review list, never from the page
    proposal["links"].append({"source": "polymarket", "external_id": "pm-rej", "direction": "up"})  # rejected: dropped
    with caplog.at_level(logging.INFO):
        r = client.post("/agent/proposals/accept", json={"proposal": proposal})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft" and body["kind"] == "chain" and {c["key"] for c in body["checks"]} == {"lookahead", "coverage", "roll_jumps"}
    assert all(c["status"] == "ok" for c in body["checks"]) and [d["reason"] for d in body["dropped"]] == ["abgelehnt"]
    row = session.get(CustomMetric, body["key"])
    assert row.kind == "chain" and row.spec["rules"] == ["direction", "roll_end_date", "no_gap_fill", "no_forward_fill"]
    assert row.spec["topic"] == {"id": topic.id, "name": topic.name}
    assert [(l["external_id"], l["roll_on"], l["direction"]) for l in row.spec["links"]] == [
        ("FED-MAR", (TODAY - timedelta(100)).isoformat(), "down"), ("FED-MAY", (TODAY + timedelta(60)).isoformat(), "down")]
    m = {x["key"]: x for x in client.get("/metrics").json()["metrics"]}[body["key"]]
    assert m["status"] == "draft" and m["kind"] == "chain" and len(m["links"]) == 2 and len(m["series"]) == 300
    assert "nicht automatisch umgesetzt" in m["calculation"]
    assert "metric proposal accept clicked" in caplog.text and "accepted as " + body["key"] in caplog.text
    assert "links outside the accepted review list dropped" in caplog.text


def test_accept_refuses_links_outside_the_review_list(env, caplog):
    client, session, _, topic = env
    bad = {"name": "X", "topic_id": topic.id, "links": [{"source": "polymarket", "external_id": "pm-rej"},
                                                        {"source": "kalshi", "external_id": "invented"}], "rules": [], "notes": []}
    with caplog.at_level(logging.WARNING):
        assert client.post("/agent/proposals/accept", json={"proposal": bad}).status_code == 400
        assert client.post("/agent/proposals/accept", json={"proposal": {**bad, "topic_id": 999}}).status_code == 404
    assert session.query(CustomMetric).count() == 0 and "no link accepted in the review list" in caplog.text


def test_propose_needs_a_known_topic(env):
    client, *_ = env
    assert client.post("/agent/propose", json={"idea": "Fed"}).status_code == 422
    assert client.post("/agent/propose", json={"topic_id": 999, "idea": "Fed"}).status_code == 404


def test_chain_metric_from_58_keeps_its_values(env):
    """Regression: a chain metric stored by the former domain route (spec with "domains", reading "up") computes as before."""
    client, session, _, _ = env
    spec = {"links": [{"source": "kalshi", "external_id": "FED-MAR", "title": MAR[2], "roll_on": (TODAY - timedelta(100)).isoformat(), "direction": "up"},
                      {"source": "kalshi", "external_id": "FED-MAY", "title": MAY[2], "roll_on": (TODAY + timedelta(60)).isoformat(), "direction": "up"}],
            "rules": ["direction", "roll_end_date", "no_gap_fill", "no_forward_fill"], "notes": [], "domains": ["Fed"], "assessment": {}}
    key = create_structured(session, "Fed alt", "chain", spec, "", TODAY)[0]
    series = {x["key"]: x for x in client.get("/metrics").json()["metrics"]}[key]["series"]
    expected = {}
    for n in range(1, 301):
        base = MAR[4] if n >= 100 else MAY[4]  # FED-MAR up to its roll day, then FED-MAY
        expected[(TODAY - timedelta(n)).isoformat()] = round(price(base, n), 4)
    assert len(series) == 300 and {p["date"]: p["level"] for p in series} == expected


def test_model_failure_is_502(env):
    client, _, fake, topic = env

    def boom(ctx):
        from backend.app.agent.assistant import AgentError
        raise AgentError("down")
    fake.answer = boom
    assert client.post("/agent/propose", json={"topic_id": topic.id, "idea": "Fed"}).status_code == 502


def test_rule_catalog_endpoint(env):
    body = env[0].get("/agent/rules").json()
    assert [r["key"] for r in body["rules"]][:2] == ["direction", "roll_end_date"] and "no_gap_fill" in body["always"]


EMPTY = {"reply": "Nichts passt.", "proposals": []}


def test_api_creator_uses_sonnet_with_structured_output():
    calls = []
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: calls.append(kw) or SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(EMPTY))])))
    assert ApiCreator(client).propose("idee", {"topic": {}}) == EMPTY
    assert calls[0]["model"] == CREATOR_MODEL == "claude-sonnet-5-5" and calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert "direction" not in json.dumps(calls[0]["output_config"]["format"]["schema"])  # the model cannot set a reading


def test_cli_creator_uses_sonnet_via_console():
    seen = []
    cli = SimpleNamespace(structured=lambda model, system, prompt, schema, effort: seen.append((model, effort)) or EMPTY)
    creator = CliCreator(cli)
    assert creator.propose("idee", {}) is EMPTY and seen == [("claude-sonnet-5-5", "medium")] and creator.name.endswith("(Konsole)")


def test_create_mode_lives_below_the_review_list_and_logs():
    html = TestClient(app).get("/").text
    risk = html[html.index('data-view-panel="risk"'):]
    panel = risk[risk.index('id="rk-panel"'):]
    assert 'id="ag-create-panel"' in panel and panel.index('id="mc-panel"') < panel.index('id="ag-chat"')  # blueprint moved along
    for el in ('id="mc-input"', 'id="mc-send"', 'id="mc-proposals"', 'id="ag-send"', 'id="ag-metric"'):
        assert el in panel
    agent = html[html.index('data-view-panel="agent"'):html.index('data-view-panel="domains"')]
    assert 'data-ag-mode="create"' in agent and 'id="ag-create-panel"' not in agent and 'id="mc-domain"' not in html
    src = (STATIC / "creator.js").read_text(encoding="utf-8")
    assert "Hinweis, nicht automatisch umgesetzt" in src and "Als Metrik-Entwurf übernehmen" in src and "topic_id" in src
    for event in ("agent.propose.send", "agent.propose.reply", "agent.propose.accept.click", "agent.propose.accepted", "agent.propose.no_topic"):
        assert event in src
    assert "topic_id" in (STATIC / "agent.js").read_text(encoding="utf-8")
    finder = (STATIC / "finder.js").read_text(encoding="utf-8")
    assert "mcHandover(" in finder and 'data-view="risk"' in finder
    if shutil.which("node"):
        for name in ("creator.js", "finder.js", "agent.js", "risk.js"):
            subprocess.run(["node", "--check", str(STATIC / name)], check=True)
