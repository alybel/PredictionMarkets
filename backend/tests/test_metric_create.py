"""Custom metrics creation in tab "Metriken" (1015-2351): POST /metrics creates a market-basket metric (status draft,
own series and checks), validation, unique keys, panel in the page and frontend flow (node, stub DOM). No network, no LLM."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_session
from backend.app.fed.api import get_fed_source
from backend.app.metrics.models import CustomMetric
from backend.tests.eui_helpers import memory_session
from backend.tests.test_metric_catalog import EmptySource
from backend.tests.test_metric_index_builder import snapshots

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PM_X = {"source": "polymarket", "external_id": "pm-x", "title": "Recession?"}


@pytest.fixture()
def env():
    session = memory_session()
    snapshots(session, lambda i: 0.2 + 0.01 * i)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_fed_source] = EmptySource
    yield TestClient(app), session
    app.dependency_overrides.clear()


def test_created_metric_appears_in_catalog_as_draft_with_series(env, caplog):
    client, _ = env
    with caplog.at_level(logging.INFO):
        r = client.post("/metrics", json={"name": "Rezessionsrisiko USA", "calculation": "Mein Korb", "markets": [PM_X, PM_X]})
    assert r.status_code == 201 and r.json() == {"key": "cm_rezessionsrisiko_usa", "name": "Rezessionsrisiko USA", "status": "draft"}
    assert "metric creation requested via API" in caplog.text
    m = {x["key"]: x for x in client.get("/metrics").json()["metrics"]}["cm_rezessionsrisiko_usa"]
    assert m["unit"] == "probability" and "Mein Korb" in m["calculation"] and m["series"]
    assert {c["key"] for c in m["checks"]} == {"lookahead", "coverage"}
    assert "fed_front_linked" in {x["key"] for x in client.get("/metrics").json()["metrics"]}  # existing metrics stay


def test_duplicate_markets_dropped_and_names_get_unique_keys(env):
    client, session = env
    keys = [client.post("/metrics", json={"name": "Korb!", "markets": [PM_X, PM_X]}).json()["key"] for _ in range(2)]
    assert keys == ["cm_korb", "cm_korb_2"]
    assert len(session.get(CustomMetric, "cm_korb").markets) == 1


def test_created_metric_can_be_released(env):
    client, _ = env
    key = client.post("/metrics", json={"name": "Korb", "markets": [PM_X]}).json()["key"]
    assert client.post(f"/metrics/{key}/approve").status_code == 200


@pytest.mark.parametrize("body", [
    {"name": "X", "markets": []},
    {"name": "", "markets": [PM_X]},
    {"name": "   ", "markets": [PM_X]},
    {"name": "X", "markets": [{"source": "nyse", "external_id": "a"}]},
    {"name": "X", "markets": [{"source": "kalshi", "external_id": ""}]},
])
def test_invalid_input_is_refused(env, body):
    client, session = env
    assert client.post("/metrics", json=body).status_code == 422
    assert session.query(CustomMetric).count() == 0


def test_panel_renders_in_metrics_tab():
    html = TestClient(app).get("/").text
    head = html[html.index('data-view-panel="metrics"'):html.index('id="metric-new"')]
    assert 'id="metrics-new"' in head and "+ Neue Metrik" in head  # visible button in the catalog head (feedback 1015-2359)
    for el in ('id="metric-new"', 'id="mn-name"', 'id="mn-q"', 'id="mn-create"', 'id="metrics-list"', 'id="metrics-check"'):
        assert el in html
    assert html.index("/static/metrics.js") < html.index("/static/metricnew.js")


# Loads metricnew.js against a stub DOM: search, add a market, type a name, create; prints calls, events, UI state.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const events = [], calls = [], els = {};
const node = (tag) => ({ tag, children: [], dataset: {}, handlers: {}, textContent: "", value: "", disabled: false, open: false,
  scrollIntoView() { this.scrolled = true; }, focus() { this.focused = true; },
  append(...c) { this.children.push(...c); }, appendChild(c) { this.children.push(c); }, replaceChildren(...c) { this.children = c; },
  addEventListener(t, fn) { this.handlers[t] = fn; } });
const walk = (n, out = []) => { out.push(n); n.children.forEach((c) => walk(c, out)); return out; };
const item = { source: "polymarket", external_id: "pm-x", title: "Recession?", price: 0.3, liquidity: 50000 };
const ctx = { console, JSON, Object, Math, Map, encodeURIComponent, document: { createElement: node },
  $: (id) => els[id] ??= node(id), logEvent: (level, event, detail) => events.push(event),
  fedServerError: async (res) => "HTTP " + res.status, loadMetrics: async (reason) => calls.push(["loadMetrics", reason]),
  fetch: async (url, opts) => { calls.push([url, opts?.method || "GET", opts?.body ? JSON.parse(opts.body) : null]);
    return { ok: true, json: async () => (url.startsWith("/manufacture") ? { total: 1, items: [item] } : { key: "cm_x", name: "X", status: "draft" }) }; } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
(async () => {
  els["metrics-new"].handlers.click();
  const opened = els["metric-new"].open && els["metric-new"].scrolled && els["mn-name"].focused;
  els["mn-q"].value = "recession";
  await els["mn-search"].handlers.click();
  walk(els["mn-results"]).find((n) => n.dataset.action === "add").handlers.click();
  const disabledWithoutName = els["mn-create"].disabled;
  els["mn-name"].value = "X";
  els["mn-name"].handlers.input();
  const disabledReady = els["mn-create"].disabled;
  await els["mn-create"].handlers.click();
  console.log(JSON.stringify({ opened, disabledWithoutName, disabledReady, calls, events, selectedAfter: els["mn-selected"].children.length,
                               status: els["mn-status"].textContent }));
})();
"""


def test_frontend_search_add_and_create_flow():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "metricnew.js")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["opened"] is True
    assert r["disabledWithoutName"] is True and r["disabledReady"] is False
    assert r["calls"][0][0].startswith("/manufacture/catalog?") and "q=recession" in r["calls"][0][0]
    assert r["calls"][1] == ["/metrics", "POST", {"name": "X", "calculation": "",
                                                  "markets": [{"source": "polymarket", "external_id": "pm-x", "title": "Recession?"}]}]
    assert r["calls"][2] == ["loadMetrics", "create"]
    assert r["events"] == ["metrics.new.open.click", "metrics.new.search.click", "metrics.new.search.done", "metrics.new.add.click",
                           "metrics.new.create.click", "metrics.new.create.done"]
    assert r["selectedAfter"] == 0 and "angelegt" in r["status"]
