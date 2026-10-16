"""Tab "Metriken" (1015-2157): tab and section render, "Freigeben" only enabled with all checks green, clicks reach the
backend and the log. Frontend script in node with a stub DOM; no network."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

# Loads metrics.js against a stub DOM, renders one metric card, clicks one of its buttons, prints the UI state.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [file, metric, action] = process.argv.slice(1);
const events = [], calls = [];
const node = (tag) => ({ tag, children: [], dataset: {}, style: {}, attrs: {}, handlers: {}, textContent: "", disabled: false,
  classList: { contains: () => true, toggle() {} },
  append(...c) { this.children.push(...c); }, appendChild(c) { this.children.push(c); }, replaceChildren(...c) { this.children = c; },
  setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, fn) { this.handlers[t] = fn; },
  querySelector() { return this.tbody ??= node("tbody"); } });
const els = {};
const walk = (n, out = []) => { if (n && typeof n === "object") { out.push(n); (n.children || []).forEach((c) => walk(c, out)); } return out; };
const ctx = { console, JSON, Object, Math, Date, encodeURIComponent,
  document: { createElement: node, querySelector: () => node("x") },
  $: (id) => els[id] ??= node(id), logEvent: (level, event, detail) => events.push([event, detail]),
  qualCheckRow: (c) => node("tr"), fedNode: (tag) => node(tag), fedAxisTicks: () => [], fedDay: (iso) => Date.parse(iso),
  fedDate: (iso) => iso, fedBp: (v) => String(v), fedServerError: async (res) => "HTTP " + res.status,
  fetch: async (url, opts) => { calls.push([url, opts?.method || "GET"]);
    return { ok: true, status: 200, json: async () => (url === "/metrics" ? { metrics: [JSON.parse(metric)] } : { status: "validated" }) }; } };
vm.runInNewContext(fs.readFileSync(file, "utf8"), ctx);
(async () => {
  const card = ctx.metricCard(JSON.parse(metric));
  const buttons = Object.fromEntries(walk(card).filter((n) => n.dataset.action).map((b) => [b.dataset.action, b]));
  if (action) await buttons[action].handlers.click();
  console.log(JSON.stringify({ approveDisabled: buttons.approve.disabled, blockDisabled: buttons.block.disabled, calls, events }));
})();
"""


def metric(**over) -> dict:
    m = {"key": "fed_front_linked", "name": "Fed Front-Kette, verkettet (bp)", "unit": "bp", "source": "Kalshi + Polymarket",
         "calculation": "…", "status": "draft", "status_reason": None, "validated_at": None, "blocked_at": None,
         "checked_at": "2026-10-15T20:00:00+00:00", "checks": [{"key": "coverage", "status": "ok"}], "all_green": True,
         "can_approve": True, "series": [{"date": "2026-01-19", "value": 0.0, "roll": False}]}
    return m | over


def run(m: dict, action: str = "") -> dict:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "metrics.js"), json.dumps(m), action], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_metrics_tab_and_section_render():
    html = TestClient(app).get("/").text
    assert '<button type="button" class="tab" data-view="metrics">Metriken</button>' in html
    assert 'data-view-panel="metrics"' in html and 'id="metrics-list"' in html and 'id="metrics-check"' in html
    assert html.index("/static/fedchains.js") < html.index("/static/metrics.js")
    assert 'data-view="quality">Datenqualität</button>' in html and 'id="fed-card"' in html  # existing views stay


def test_approve_is_enabled_only_with_all_checks_green():
    assert run(metric())["approveDisabled"] is False
    red = run(metric(all_green=False, can_approve=False, status="blocked", checks=[{"key": "coverage", "status": "blocked"}]))
    assert red["approveDisabled"] is True and red["blockDisabled"] is True


def test_approve_click_calls_backend_and_logs():
    r = run(metric(), "approve")
    assert ["/metrics/fed_front_linked/approve", "POST"] in r["calls"]
    assert [e for e, _ in r["events"]][:2] == ["metrics.approve.click", "metrics.approve.done"]


def test_block_click_calls_backend_and_logs():
    r = run(metric(status="validated", can_approve=False), "block")
    assert ["/metrics/fed_front_linked/block", "POST"] in r["calls"] and r["events"][0] == ["metrics.block.click", {"key": "fed_front_linked"}]
