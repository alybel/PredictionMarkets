"""Liquidity in every metric (1015-2355): daily turnover per metric (Fed chains and market baskets), 30-day average,
liquidity filter on /metrics and /builder/metrics, frontend filter in tab "Metriken" and in the builder. No network."""

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app
from backend.app.metrics.catalog import MetricPoint
from backend.app.metrics.liquidity import average_liquidity, fed_volumes, passes, with_chain_liquidity
from backend.tests.test_metric_catalog import FRONT, A, computed, evaluated, raw_points
from backend.tests.test_metric_index_builder import BASKET, FED, env  # noqa: F401  (fixture)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def with_kalshi_volume(points, usd=1_000.0):
    return [replace(p, volume_usd=usd) if p.source == "kalshi" else p for p in points]


def test_fed_chain_liquidity_sums_kalshi_volume_of_the_front_meeting():
    points = with_kalshi_volume(raw_points())
    data, front = evaluated(points)
    liquid = with_chain_liquidity(FRONT, fed_volumes(computed(points)), front)
    first = liquid[0]
    assert first.session_date == A and first.liquidity == pytest.approx(2_000.0)  # two Kalshi outcomes of meeting A
    assert [p.value for p in liquid] == [p.value for p in front]                 # values unchanged
    kalshi_only = replace(FRONT, series="polymarket")
    assert with_chain_liquidity(kalshi_only, fed_volumes(data), front)[0].liquidity is None  # Polymarket publishes none


def test_average_uses_the_last_30_days_with_data_and_filter_rules():
    day = date(2026, 1, 1)
    pts = [MetricPoint(day + timedelta(i), 0.5, day, None, False, 100.0 if i < 10 else 10.0) for i in range(40)]
    assert average_liquidity(pts) == pytest.approx(10.0)
    assert average_liquidity([replace(p, liquidity=None) for p in pts]) is None
    assert passes(None, None) and passes(None, 0) and passes(5.0, 5.0)
    assert not passes(None, 1.0) and not passes(4.9, 5.0)


def test_metrics_endpoint_carries_liquidity_and_filters(env):  # noqa: F811
    client, _ = env
    metrics = {m["key"]: m for m in client.get("/metrics").json()["metrics"]}
    assert metrics[BASKET]["liquidity_usd"] == 20_000.0 and metrics[BASKET]["series"][0]["liquidity"] == 20_000.0
    assert metrics[FED]["liquidity_usd"] is None            # fixture prices carry no volume
    filtered = client.get("/metrics", params={"min_liquidity": 10_000}).json()
    assert {m["key"] for m in filtered["metrics"]} == {BASKET, "mf_draft"} and filtered["min_liquidity"] == 10_000
    assert client.get("/metrics", params={"min_liquidity": 50_000}).json()["metrics"] == []
    assert client.get("/metrics", params={"min_liquidity": -1}).status_code == 422
    assert len(client.get("/metrics", params={"min_liquidity": 0}).json()["metrics"]) == len(metrics)


def test_builder_selection_carries_liquidity_and_filters(env):  # noqa: F811
    client, _ = env
    items = {m["key"]: m for m in client.get("/builder/metrics").json()["metrics"]}
    assert set(items) == {FED, BASKET} and items[BASKET]["liquidity_usd"] == 20_000.0
    assert [m["key"] for m in client.get("/builder/metrics", params={"min_liquidity": 1}).json()["metrics"]] == [BASKET]
    client.post("/builder/indices", json={"name": "Liq", "components": [{"key": BASKET, "weight": 1}]})
    assert client.get("/builder/indices/Liq").json()["components"][0]["liquidity_usd"] == 20_000.0


def test_filter_inputs_render():
    html = TestClient(app).get("/").text
    assert 'id="metrics-minliq"' in html and 'id="mi-minliq"' in html and 'id="metrics-check"' in html


# Loads metrics.js against a stub DOM, renders two metrics with a minimum liquidity, fires the filter change.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [file, metrics, min] = process.argv.slice(1);
const events = [];
const node = (tag) => ({ tag, children: [], dataset: {}, style: {}, attrs: {}, handlers: {}, textContent: "", value: "",
  classList: { contains: () => true, toggle() {} },
  append(...c) { this.children.push(...c); }, appendChild(c) { this.children.push(c); }, replaceChildren(...c) { this.children = c; },
  setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, fn) { this.handlers[t] = fn; },
  querySelector() { return this.tbody ??= node("tbody"); } });
const els = {};
const ctx = { console, JSON, Object, Math, Date, Number, encodeURIComponent,
  document: { createElement: node, querySelector: () => node("x") },
  $: (id) => els[id] ??= node(id), logEvent: (level, event, detail) => events.push([event, detail]),
  qualCheckRow: () => node("tr"), fedNode: (tag) => node(tag), fedAxisTicks: () => [], fedDay: (iso) => Date.parse(iso),
  fedDate: (iso) => iso, fedBp: (v) => String(v), fedServerError: async () => "" };
vm.runInNewContext(fs.readFileSync(file, "utf8"), ctx);
ctx.renderMetrics(JSON.parse(metrics));
const all = els["metrics-list"].children.map((c) => c.dataset.metric);
els["metrics-minliq"].value = min;
els["metrics-minliq"].handlers.change();
console.log(JSON.stringify({ all, shown: els["metrics-list"].children.map((c) => c.dataset.metric),
  status: els["metrics-status"].textContent, events }));
"""


def metric(key, liquidity):
    return {"key": key, "name": key, "unit": "probability", "source": "s", "calculation": "", "status": "draft",
            "status_reason": None, "validated_at": None, "blocked_at": None, "checked_at": None, "checks": [],
            "all_green": False, "can_approve": False, "liquidity_usd": liquidity, "series": []}


def test_metrics_tab_filters_by_liquidity_and_logs():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    data = [metric("liquid", 25_000.0), metric("thin", 500.0), metric("unknown", None)]
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "metrics.js"), json.dumps(data), "10000"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["all"] == ["liquid", "thin", "unknown"] and r["shown"] == ["liquid"]
    assert r["status"].startswith("1 von 3 Metriken ab")
    assert r["events"] == [["metrics.liquidity.filter", {"min": 10000, "shown": ["liquid"]}]]


def test_builder_script_wires_the_liquidity_filter():
    js = (STATIC / "metricindex.js").read_text()
    assert '$("mi-minliq").addEventListener("change"' in js and "builder.liquidity.filter" in js
    assert "label.hidden = !pick.checked" in js  # checked metrics never disappear from the saved components
