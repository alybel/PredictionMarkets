"""Select markets in panel "Neue Metrik bauen" (1016-0005): opening lists the most liquid markets without typing, source
filter, "Mehr laden" paging, toggle per row synced with the chips, "Auswahl leeren". Node with a stub DOM; no network, no LLM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const events = [], calls = [], els = {};
const node = (tag) => ({ tag, children: [], dataset: {}, handlers: {}, textContent: "", value: "", className: "", disabled: false,
  open: false, scrollIntoView() {}, focus() {},
  append(...c) { this.children.push(...c); }, appendChild(c) { this.children.push(c); }, replaceChildren(...c) { this.children = c; },
  addEventListener(t, fn) { this.handlers[t] = fn; } });
const walk = (n, out = []) => { out.push(n); n.children.forEach((c) => walk(c, out)); return out; };
const market = (i) => ({ source: "kalshi", external_id: "k" + i, title: "Market " + i, price: 0.5, liquidity: 1000 - i });
const ctx = { console, JSON, Object, Math, Map, encodeURIComponent, document: { createElement: node },
  $: (id) => els[id] ??= node(id), logEvent: (level, event, detail) => events.push([event, detail]),
  fedServerError: async (res) => "HTTP " + res.status, loadMetrics: async () => {},
  fetch: async (url) => { calls.push(url); const offset = Number(new URL(url, "http://x").searchParams.get("offset"));
    return { ok: true, json: async () => ({ total: 25, items: Array.from({ length: Math.min(20, 25 - offset) }, (_, i) => market(offset + i)) }) }; } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const toggles = () => walk(els["mn-results"]).filter((n) => n.dataset.action === "add");
const flush = () => new Promise((r) => setTimeout(r, 0));
(async () => {
  const out = {};
  els["metric-new"].open = true;
  els["metric-new"].handlers.toggle();
  await flush();
  out.browseUrl = calls[0];
  out.rowsAfterOpen = toggles().length;
  out.moreEnabled = !els["mn-more"].disabled;
  els["metric-new"].handlers.toggle();  // reopening does not reload
  await flush();
  out.callsAfterReopen = calls.length;
  await els["mn-more"].handlers.click();
  out.moreUrl = calls[1];
  out.rowsAfterMore = toggles().length;
  out.moreDisabledAtEnd = els["mn-more"].disabled;
  const first = toggles()[0];
  first.handlers.click();
  out.afterAdd = [first.textContent, els["mn-selected"].children.length, els["mn-clear"].disabled];
  first.handlers.click();
  out.afterSecondClick = [first.textContent, els["mn-selected"].children.length];
  toggles()[1].handlers.click(); toggles()[2].handlers.click();
  walk(els["mn-selected"]).find((n) => n.dataset.action === "remove").handlers.click();  // chip × re-enables its row
  out.afterChipRemove = [toggles()[1].textContent, els["mn-selected"].children.length];
  els["mn-clear"].handlers.click();
  out.afterClear = [toggles()[2].textContent, els["mn-selected"].children.length, els["mn-clear"].disabled];
  els["mn-source"].value = "polymarket";
  await els["mn-source"].handlers.change();
  out.sourceUrl = calls[2];
  out.rowsAfterSource = toggles().length;
  out.events = events.map(([e]) => e);
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def flow() -> dict:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "metricnew.js")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_controls_render_in_panel():
    html = TestClient(app).get("/").text
    panel = html[html.index('id="metric-new"'):html.index('id="metrics-list"')]
    for el in ('id="mn-source"', 'id="mn-more"', 'id="mn-clear"', 'id="mn-results"', 'id="mn-selected"', 'id="mn-q"'):
        assert el in panel


def test_opening_lists_most_liquid_markets_without_typing(flow):
    assert "sort=liquidity" in flow["browseUrl"] and "offset=0" in flow["browseUrl"]
    assert flow["browseUrl"].endswith("&q=")
    assert flow["rowsAfterOpen"] == 20 and flow["moreEnabled"] is True
    assert flow["callsAfterReopen"] == 1


def test_more_appends_next_page(flow):
    assert "offset=20" in flow["moreUrl"]
    assert flow["rowsAfterMore"] == 25 and flow["moreDisabledAtEnd"] is True


def test_toggle_chips_and_clear_stay_in_sync(flow):
    assert flow["afterAdd"] == ["Entfernen", 1, False]
    assert flow["afterSecondClick"] == ["Hinzufügen", 0]
    assert flow["afterChipRemove"] == ["Hinzufügen", 1]
    assert flow["afterClear"] == ["Hinzufügen", 0, True]


def test_source_filter_reloads_list(flow):
    assert "source=polymarket" in flow["sourceUrl"] and "offset=0" in flow["sourceUrl"]
    assert flow["rowsAfterSource"] == 20


def test_every_click_is_logged(flow):
    for e in ("metrics.new.toggle", "metrics.new.browse.click", "metrics.new.more.click", "metrics.new.add.click",
              "metrics.new.remove.click", "metrics.new.clear.click", "metrics.new.source.click", "metrics.new.search.done"):
        assert e in flow["events"]
