"""Median liquidity as reference point for the liquidity filter (1016-1007): backend median over the current
(30-day average) liquidity, shown with a hint that the filter acts on the current edge only. No network."""

import json
import shutil
import subprocess
from pathlib import Path
from statistics import median

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app
from backend.app.metrics.liquidity import median_liquidity
from backend.tests.test_metric_index_builder import BASKET, env  # noqa: F401  (fixture)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_median_skips_metrics_without_turnover_but_counts_them():
    assert median_liquidity([100.0, None, 300.0, 50.0]) == {"usd": 100.0, "metrics": 3, "of": 4}
    assert median_liquidity([10.0, 20.0]) == {"usd": 15.0, "metrics": 2, "of": 2}
    assert median_liquidity([None]) == {"usd": None, "metrics": 0, "of": 1}
    assert median_liquidity([]) == {"usd": None, "metrics": 0, "of": 0}


def test_metrics_endpoint_median_covers_all_metrics_regardless_of_filter(env):  # noqa: F811
    client, _ = env
    full = client.get("/metrics").json()
    known = [m["liquidity_usd"] for m in full["metrics"] if m["liquidity_usd"] is not None]
    expected = {"usd": round(median(known), 2), "metrics": len(known), "of": len(full["metrics"])}
    assert full["liquidity_median"] == expected
    assert client.get("/metrics", params={"min_liquidity": 50_000}).json()["liquidity_median"] == expected
    assert client.post("/metrics/check").json()["liquidity_median"] == expected


def test_builder_median_covers_validated_metrics_only(env):  # noqa: F811
    client, _ = env
    body = client.get("/builder/metrics", params={"min_liquidity": 1_000_000}).json()
    assert body["metrics"] == []
    assert body["liquidity_median"] == {"usd": 20_000.0, "metrics": 1, "of": 2}  # BASKET known, FED without volume


def test_median_hints_render_next_to_both_filters():
    html = TestClient(app).get("/").text
    assert 'id="metrics-liqmedian"' in html and 'id="mi-liqmedian"' in html
    js = (STATIC / "metricindex.js").read_text()
    assert 'showLiquidityMedian("mi-liqmedian", "mi-minliq"' in js and "builder.liquidity.median.click" in js


# Loads metrics.js against a stub DOM, shows the median, clicks "übernehmen" and checks filter and log.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [file, metrics, med] = process.argv.slice(1);
const events = [];
const node = (tag) => ({ tag, children: [], dataset: {}, style: {}, attrs: {}, handlers: {}, textContent: "", value: "",
  classList: { contains: () => true, toggle() {} },
  append(...c) { this.children.push(...c); }, appendChild(c) { this.children.push(c); }, replaceChildren(...c) { this.children = c; },
  setAttribute(k, v) { this.attrs[k] = v; },
  addEventListener(t, fn) { this.handlers[t] = fn; }, querySelector() { return node("x"); } });
const els = {};
const ctx = { console, JSON, Object, Math, Number, String, encodeURIComponent,
  document: { createElement: node, querySelector: () => node("x") },
  $: (id) => els[id] ??= node(id), logEvent: (level, event, detail) => events.push([event, detail]),
  qualCheckRow: () => node("tr"), fedNode: (tag) => node(tag), fedAxisTicks: () => [], fedDay: (iso) => Date.parse(iso),
  fedDate: (iso) => iso, fedBp: (v) => String(v), fedServerError: async () => "" };
vm.runInNewContext(fs.readFileSync(file, "utf8"), ctx);
ctx.renderMetrics(JSON.parse(metrics));
ctx.showLiquidityMedian("metrics-liqmedian", "metrics-minliq", JSON.parse(med), ctx.metricsApplyMinLiquidity);
const box = els["metrics-liqmedian"].children;
const text = box.map((c) => typeof c === "string" ? c : c.textContent).join("");
const take = box.find((c) => typeof c !== "string");
if (take) take.handlers.click();
console.log(JSON.stringify({ text, input: els["metrics-minliq"].value,
  shown: els["metrics-list"].children.map((c) => c.dataset.metric), events }));
"""


def run(med):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    data = [{"key": k, "name": k, "unit": "probability", "source": "s", "calculation": "", "status": "draft",
             "status_reason": None, "validated_at": None, "blocked_at": None, "checked_at": None, "checks": [],
             "all_green": False, "can_approve": False, "liquidity_usd": v, "series": []}
            for k, v in (("liquid", 25_000.0), ("mid", 5_000.0), ("thin", 500.0))]
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "metrics.js"), json.dumps(data), json.dumps(med)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_median_hint_takes_over_the_median_as_minimum_and_logs():
    r = run({"usd": 5_000.0, "metrics": 3, "of": 3})
    assert "Median der aktuellen Liquidität" in r["text"] and "3 von 3 Metriken" in r["text"]
    assert "nur am aktuellen Rand" in r["text"] and "nicht historisch" in r["text"]
    assert r["input"] == "5000" and r["shown"] == ["liquid", "mid"]
    assert [e[0] for e in r["events"]] == ["metrics.liquidity.median.click", "metrics.liquidity.filter"]


def test_median_hint_without_turnover_data_still_explains_the_filter():
    r = run({"usd": None, "metrics": 0, "of": 3})
    assert "kein Median" in r["text"] and "nicht historisch" in r["text"]
    assert r["input"] == "" and r["events"] == []
