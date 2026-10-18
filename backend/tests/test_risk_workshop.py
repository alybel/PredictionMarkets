"""Risk index workshop (risk index 7/7, 1017-1107): status per step, open tasks, AI proposals only effective after a human
click, end to end topic -> index -> forecast report. Fake classifier/creator/weigher and fake sources: no LLM, no network."""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.agent import creator_api
from backend.app.api import app, get_session
from backend.app.builder.api import get_weigher
from backend.app.builder.models import MetricIndex
from backend.app.directory.api import get_directory_getter, get_directory_staging
from backend.app.eui.models import MarketSnapshot, SnapshotRun
from backend.app.fed.api import get_fed_source
from backend.app.quality.pit import BACKFILL
from backend.app.risk.api import get_risk_classifier
from backend.app.risk.models import RiskTopic
from backend.tests.eui_helpers import memory_session
from backend.tests.test_market_directory import K_CLOSED, K_OPEN, PM_OPEN, PM_RESOLVED, FakeSources
from backend.tests.test_metric_catalog import EmptySource
from backend.tests.test_metric_creator import FakeCreator
from backend.tests.test_index_weighting import FakeWeigher
from backend.tests.test_risk_topics import FakeHaiku

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TODAY = datetime.now(timezone.utc).date()
END = (TODAY + timedelta(200)).isoformat() + "T00:00:00Z"
PM = {**PM_OPEN, "endDate": END}  # end date relative to today: the test never ages


def single_answer(ctx):
    return {"reply": "Ein Vorschlag.", "proposals": [{"name": "S&P-Einbruch", "kind": "single", "refs": ["polymarket:pm-1"], "rules": [],
                                                       "free_rules": [], "reason": "Direkte Crash-Frage"}]}


@pytest.fixture()
def env(tmp_path):
    session = memory_session()
    fakes = {"sources": FakeSources(poly_open=(PM,), poly_closed=(PM_RESOLVED,), kalshi=(K_OPEN, K_CLOSED)), "haiku": FakeHaiku(),
             "creator": FakeCreator(single_answer), "weigher": None}
    app.dependency_overrides.update({
        get_session: lambda: session, get_directory_getter: lambda: fakes["sources"], get_directory_staging: lambda: tmp_path / "staging",
        get_risk_classifier: lambda: fakes["haiku"], creator_api.get_creator: lambda: fakes["creator"], get_fed_source: EmptySource,
        get_weigher: lambda: fakes["weigher"]})
    client = TestClient(app)
    pilot = next(t for t in client.get("/risk/topics").json() if t["slug"] == "marktcrash")
    yield client, session, fakes, pilot["id"]
    app.dependency_overrides.clear()


def status(client, topic_id):
    body = client.get(f"/workshop/topics/{topic_id}/status").json()
    return {s["step"]: s for s in body["steps"]}, body


def tasks(step):
    return {t["key"]: t["count"] for t in step["tasks"]}


def seed_history(session, days=300):
    run = SnapshotRun(snapshot_date=TODAY, status="ok")
    session.add(run)
    session.flush()
    session.add_all(MarketSnapshot(run_id=run.id, snapshot_date=TODAY - timedelta(n), source="polymarket", external_id="pm-1", title=PM["question"],
                                   tags=["Finance"], price=0.1 + 0.001 * (n % 7), volume_24h_usd=5000.0, exclusion_reason=BACKFILL, fetched_at=None,
                                   end_date=datetime.fromisoformat(END.replace("Z", "+00:00"))) for n in range(1, days + 1))
    session.commit()


def review_all(client, topic_id, accept=("pm-1",)):
    for a in client.get(f"/risk/topics/{topic_id}/assessments", params={"limit": 500}).json()["assessments"]:
        verdict = "accepted" if a["external_id"] in accept else "rejected"
        assert client.patch(f"/risk/assessments/{a['id']}", json={"status": verdict}).status_code == 200


# ---- status endpoint --------------------------------------------------------------------------

def test_fresh_topic_has_every_step_open_and_only_step_1_ready(env):
    client, _, _, pilot = env
    steps, body = status(client, pilot)
    assert body["topic"]["name"] == "Marktcrash-Risiko" and body["current_step"] == 1
    assert [steps[n]["status"] for n in range(1, 6)] == ["open"] * 5
    assert [steps[n]["ready"] for n in range(1, 6)] == [True, False, False, False, False]
    assert [steps[n]["title"] for n in range(1, 6)] == ["Rohdaten", "Themen-Einordnung & Prüfliste", "Ketten-Metriken", "Gewichtung", "Prognosekraft"]
    assert all(steps[n]["ai"] for n in steps)


def test_step_2_is_open_without_reviewed_markets_and_done_after_acceptance(env, caplog):
    client, _, _, pilot = env
    client.post("/directory/load")
    client.post(f"/risk/topics/{pilot}/classify")
    steps, _ = status(client, pilot)
    assert steps[1]["status"] == "done" and steps[2]["ready"] and steps[2]["status"] == "open"
    assert steps[2]["counts"]["accepted"] == 0 and tasks(steps[2])["unreviewed"] == 5 and not steps[3]["ready"]
    review_all(client, pilot)
    with caplog.at_level(logging.INFO):
        steps, body = status(client, pilot)
    assert steps[2]["status"] == "done" and steps[2]["tasks"] == [] and steps[2]["counts"]["accepted"] == 1
    assert steps[3]["ready"] and body["current_step"] == 3
    assert "workshop status of topic 'Marktcrash-Risiko'" in caplog.text


def test_open_tasks_count_unreviewed_unclassified_and_new_markets(env):
    client, _, fakes, pilot = env
    client.post("/directory/load")
    client.post(f"/risk/topics/{pilot}/classify")
    rows = client.get(f"/risk/topics/{pilot}/assessments").json()["assessments"]
    client.patch(f"/risk/assessments/{rows[0]['id']}", json={"status": "rejected"})
    fakes["sources"].kalshi.append({"ticker": "KXNEW", "title": "New crash market", "status": "active"})
    fakes["sources"].kalshi.append({"ticker": "KXOTHER", "title": "Who wins the World Cup?", "status": "active"})
    client.post("/directory/load")
    steps, _ = status(client, pilot)
    assert steps[1]["status"] == "in_progress" and tasks(steps[1]) == {"inbox": 2}  # both new markets in the inbox
    assert tasks(steps[2]) == {"unreviewed": 4, "unclassified": 1}                  # only the crash market is a candidate
    assert steps[2]["status"] == "in_progress"                                       # a rejection is progress, nothing accepted yet
    client.post("/directory/inbox/reviewed", json={})
    assert status(client, pilot)[0][1]["status"] == "done"


def test_unknown_topic_is_404_and_a_new_topic_starts_open(env):
    client, session, _, _ = env
    assert client.get("/workshop/topics/999/status").status_code == 404
    t = client.post("/risk/topics", json={"name": "Rezessions-Risiko", "question": "Kommt eine Rezession?", "search_terms": ["recession"]}).json()
    steps, body = status(client, t["id"])
    assert body["topic"]["name"] == "Rezessions-Risiko" and steps[2]["status"] == "open"
    assert session.query(RiskTopic).count() == 2


# ---- end to end ---------------------------------------------------------------------------------

def test_end_to_end_topic_to_index_to_forecast_report(env, caplog):
    client, session, fakes, pilot = env
    assert client.post("/directory/load").status_code == 200
    assert client.post(f"/risk/topics/{pilot}/classify").json()["stored"] == 5
    review_all(client, pilot)
    seed_history(session)

    proposals = client.post("/agent/propose", json={"topic_id": pilot, "idea": "Crash"}).json()["proposals"]
    assert [p["name"] for p in proposals] == ["S&P-Einbruch"]
    steps, _ = status(client, pilot)
    assert steps[3]["status"] == "open" and steps[3]["counts"]["metrics"] == 0  # a proposal alone changes nothing
    key = client.post("/agent/proposals/accept", json={"proposal": proposals[0]}).json()["key"]
    steps, _ = status(client, pilot)
    assert steps[3]["status"] == "in_progress" and tasks(steps[3]) == {"drafts": 1} and not steps[4]["ready"]
    assert client.post(f"/metrics/{key}/approve").status_code == 200
    steps, _ = status(client, pilot)
    assert steps[3]["status"] == "done" and steps[4]["ready"] and steps[4]["status"] == "open"

    fakes["weigher"] = FakeWeigher({"method": "equal", "weights": [{"key": key, "weight": 1}], "rationale": "Nur eine Metrik."})
    p = client.post("/builder/weighting/propose", json={"keys": [key]}).json()
    assert p["method"] == "equal" and p["weights_pct"] == {key: 100.0}
    steps, _ = status(client, pilot)
    assert steps[4]["status"] == "open" and session.query(MetricIndex).count() == 0  # not effective before confirmation

    with caplog.at_level(logging.INFO):
        r = client.post("/builder/indices", json={"name": "Marktcrash-Risiko Index", "components": [{"key": key, "weight": 1}],
                                                  "weighting": p["method"], "proposal": "confirmed"})
    assert r.status_code == 201, r.text
    steps, body = status(client, pilot)
    assert [steps[n]["status"] for n in range(1, 6)] == ["done"] * 5 and body["current_step"] == 5
    assert steps[4]["counts"]["indices"][0] == {"name": "Marktcrash-Risiko Index", "weighting": "equal", "weighting_label": "Gleichgewichtet",
                                                "components": [key]}
    assert steps[5]["counts"]["reports"][0]["index"] == "Marktcrash-Risiko Index" and steps[5]["counts"]["missing"] == []
    report = client.get("/builder/indices/Marktcrash-Risiko Index/forecast").json()
    assert report["index_name"] == "Marktcrash-Risiko Index" and report["trigger"] == "save" and len(report["targets"]) == 4
    assert "weighting equal, AI proposal confirmed" in caplog.text


# ---- static frontend --------------------------------------------------------------------------

def test_tab_is_wired_and_uses_only_existing_endpoints():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "workshop.js").read_text(encoding="utf-8")
    assert 'data-view="workshop">Risiko-Index-Werkstatt</button>' in html and 'id="ws-view"' in html
    assert html.index("/static/forecast.js") < html.index("/static/risk.js") < html.index("/static/workshop.js")
    for view in ("indices", "metrics", "directory", "risk", "agent"):
        assert f'data-view="{view}"' in html  # existing tabs stay
    routes = set(app.openapi()["paths"])
    used = set(re.findall(r'"(/(?:directory|risk|agent|metrics|builder|workshop)/[a-z/]*)', js))
    known = {"/directory/load", "/directory/inbox", "/directory/inbox/reviewed", "/risk/topics", "/risk/topics/", "/risk/assessments/",
             "/agent/propose", "/agent/proposals/accept", "/metrics/", "/builder/metrics", "/builder/weighting/propose", "/builder/indices",
             "/builder/indices/", "/workshop/topics/"}
    assert used <= known, used - known
    for path in ("/directory/load", "/directory/inbox/reviewed", "/risk/topics/{topic_id}/classify", "/risk/assessments/{assessment_id}",
                 "/agent/propose", "/agent/proposals/accept", "/metrics/{key}/approve", "/builder/weighting/propose", "/builder/indices",
                 "/builder/indices/{name}/forecast", "/workshop/topics/{topic_id}/status"):
        assert path in routes
    for event in ("workshop.step", "workshop.click", "workshop.review", "workshop.proposal.accepted", "workshop.metric.approved",
                  "workshop.index.saved", "workshop.weighting.proposal"):
        assert f'"{event}"' in js
