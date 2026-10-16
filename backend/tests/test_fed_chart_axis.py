"""Fed chain chart: x-axis with date ticks below the chart (1015-2147). fedchains.js runs in node with a stub DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

# Loads fedchains.js against stub $/document, evaluates `expr` in the script's scope, prints the JSON result.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [file, expr] = process.argv.slice(1);
const node = (tag) => ({ tag, attrs: {}, style: {}, children: [], textContent: "", classList: { contains: () => true },
  setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.children.push(c); },
  replaceChildren(...c) { this.children = c; }, addEventListener() {} });
const els = {};
const ctx = { console, JSON, Math, Date, $: (id) => els[id] ??= node("div"), logEvent: () => {},
  document: { createElementNS: (ns, tag) => node(tag), createElement: node, querySelector: () => node("div") } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(file, "utf8"), ctx);
console.log(JSON.stringify(vm.runInContext(expr, ctx)));
"""


def run(expr: str):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "fedchains.js"), expr], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def ticks(start: str, end: str) -> list[str]:
    return run(f'fedAxisTicks(Date.parse("{start}"), Date.parse("{end}")).map((t) => t.label)')


def test_short_range_uses_monthly_ticks():
    assert ticks("2026-01-15", "2026-04-20") == ["Feb. 2026", "März 2026", "Apr. 2026"]


def test_long_range_uses_half_year_ticks():
    assert ticks("2023-04-08", "2026-10-15") == ["Juli 2023", "Jan. 2024", "Juli 2024", "Jan. 2025", "Juli 2025",
                                                 "Jan. 2026", "Juli 2026"]


def test_multi_year_range_switches_to_years():
    assert ticks("2018-03-01", "2026-10-15") == ["2020", "2022", "2024", "2026"]


def test_ticks_stay_inside_range():
    start, end = "2024-04-04", "2026-10-15"
    inside = run(f'fedAxisTicks(Date.parse("{start}"), Date.parse("{end}"))'
                 f'.every((t) => t.t >= Date.parse("{start}") && t.t <= Date.parse("{end}"))')
    assert inside is True


def test_chart_renders_axis_line_ticks_and_labels():
    series = [{"date": d, "value": v, "roll": False} for d, v in
              [("2026-01-05", 1.0), ("2026-02-10", -3.0), ("2026-03-20", 2.5), ("2026-04-28", 0.5)]]
    data = json.dumps({"series": {"kalshi": {"front": series, "second": series}}})
    out = run(f'$("fed-series").value = "kalshi"; fedChains = {data}; renderFedChart();'
              '({ axis: $("fed-chart").children.filter((c) => c.attrs.class === "axis").length,'
              '   labels: $("fed-xaxis").children.map((c) => c.textContent),'
              '   left: $("fed-xaxis").children.map((c) => parseFloat(c.style.left)) })')
    assert out["labels"] == ["Feb. 2026", "März 2026", "Apr. 2026"]
    assert out["axis"] == 1 + len(out["labels"])  # baseline + one tick per label
    assert all(3 <= p <= 97 for p in out["left"]) and out["left"] == sorted(out["left"])


def test_empty_series_clears_axis_labels():
    out = run('$("fed-xaxis").replaceChildren({}); $("fed-series").value = "kalshi"; fedChains = null; renderFedChart();'
              '[$("fed-xaxis").children.length, $("fed-chart-caption").textContent]')
    assert out == [0, "keine Daten"]
