"""Topic tree (1016-1549-2): topic -> subtopic -> event/market from the topic list, API, and the collapsible tree in
topics.js (node with a stub DOM, no browser, no network)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_markets
from backend.app.events.tree import GENERAL, NO_TOPIC, build_tree, subtopic_nodes
from tests.eui_helpers import market

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
ELECTION = "Who will win the 2028 election?"
CANDIDATES = [market(external_id=f"pm-{i}", title=f"Will candidate {i} win?", price=p, volume_24h=v, tags=["Elections", "Politics"],
                     category="Elections", event_key="ev-2028", event_title=ELECTION, outcome_label=f"Candidate {i}")
              for i, p, v in ((1, 0.2, 100.0), (2, 0.5, 300.0), (3, None, 50.0))]
FED = market(external_id="fed", title="Fed cut in December?", tags=["Economics", "Fed"], volume_24h=900.0)
CPI = market(external_id="cpi", title="CPI above 3%?", tags=["Economics"], volume_24h=10.0)
SENATE = market(source="kalshi", external_id="sen", title="Senate control?", tags=["Politics"], volume_24h=5.0)
UNTAGGED = market(external_id="x", title="Untagged market", tags=["Recurring", "Earn 3.25%"])  # meta tags only
MARKETS = [*CANDIDATES, FED, CPI, SENATE, UNTAGGED]


def test_tree_is_built_from_topic_list():
    # topic list: Politics (4), Elections (3), Economics (2), Fed (1); a node sits under its two highest-ranked tags, an event counts once.
    tree = build_tree(MARKETS)
    assert [t["name"] for t in tree] == ["Politics", "Economics", NO_TOPIC]
    politics, economics, none = tree
    assert politics["subtopics"] == [{"name": "Elections", "events": 1, "markets": 0}, {"name": GENERAL, "events": 0, "markets": 1}]
    assert (politics["events"], politics["markets"]) == (1, 1)
    assert [s["name"] for s in economics["subtopics"]] == ["Fed", GENERAL]
    assert none["subtopics"] == [{"name": GENERAL, "events": 0, "markets": 1}]
    assert build_tree([]) == []


def test_event_node_carries_all_outcomes_sorted_by_price():
    nodes = subtopic_nodes(MARKETS, "Politics", "Elections")
    assert len(nodes) == 1
    event = nodes[0]
    assert (event["kind"], event["title"], event["outcome_count"], event["volume_24h"]) == ("event", ELECTION, 3, 450.0)
    assert [o["outcome_label"] for o in event["outcomes"]] == ["Candidate 2", "Candidate 1", "Candidate 3"]
    assert subtopic_nodes(MARKETS, "Economics", GENERAL)[0]["kind"] == "market"
    assert subtopic_nodes(MARKETS, "Nope", GENERAL) == []


@pytest.fixture()
def api():
    app.dependency_overrides[get_markets] = lambda: MARKETS
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_topics_endpoints(api):
    body = api.get("/topics").json()
    assert body["topics"] == build_tree(MARKETS) and body["as_of"]
    nodes = api.get("/topics/nodes", params={"topic": "Politics", "subtopic": "Elections"}).json()
    assert nodes["nodes"][0]["outcome_count"] == 3
    assert api.get("/topics/nodes", params={"topic": "Politics"}).status_code == 422
    assert api.get("/events").status_code == 200  # existing event endpoint untouched


def test_existing_tabs_stay_and_topics_tab_is_added():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for view in ("indices", "quality", "metrics", "catalog", "topics"):
        assert f'data-view="{view}"' in html and f'data-view-panel="{view}"' in html
    assert '<script src="/static/topics.js"></script>' in html


# Loads topics.js against a stub DOM, loads the tree, clicks through it and prints the visible state after each click.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [file, tree, nodes] = process.argv.slice(1);
const events = [], calls = [];
function node(tag) {
  const n = { tag, children: [], attrs: {}, handlers: {}, className: "", textContent: "",
    append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; },
    setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, fn) { this.handlers[t] = fn; } };
  const set = (fn) => { const s = new Set(n.className.split(" ").filter(Boolean)); fn(s); n.className = [...s].join(" "); };
  n.classList = { contains: (c) => n.className.split(" ").includes(c), add: (c) => set((s) => s.add(c)), remove: (c) => set((s) => s.delete(c)),
    toggle: (c, on) => set((s) => (on ?? !s.has(c)) ? s.add(c) : s.delete(c)) };
  return n;
}
const els = {};
const ctx = { console, JSON, Object, Math, Date, Set, String, encodeURIComponent, Number,
  document: { createElement: node, querySelectorAll: () => [], querySelector: () => Object.assign(node("x"), { className: "hidden" }) },
  $: (id) => els[id] ??= node(id), fmtNum: (v) => (v == null ? "–" : String(v)), logEvent: (level, event, detail) => events.push(event),
  fetch: async (url) => { calls.push(url); return { ok: true, status: 200, json: async () => JSON.parse(url.startsWith("/topics/nodes") ? nodes : tree) }; } };
vm.runInNewContext(fs.readFileSync(file, "utf8"), ctx);
const parts = (li) => ({ button: li.children[0], list: li.children[1] });
const state = (li) => ({ open: !parts(li).list.classList.contains("hidden"), expanded: parts(li).button.attrs["aria-expanded"],
  items: parts(li).list.children.length });
(async () => {
  await ctx.loadTopics("test");
  const steps = [];
  const topic = els["tp-tree"].children[0];
  await parts(topic).button.handlers.click(); steps.push(state(topic));
  const sub = parts(topic).list.children[0];
  await parts(sub).button.handlers.click(); steps.push(state(sub));
  const ev = parts(sub).list.children[0];
  await parts(ev).button.handlers.click(); steps.push(state(ev));
  const detail = els["tp-detail-rows"].children.length, title = els["tp-detail-title"].textContent;
  await parts(topic).button.handlers.click(); steps.push(state(topic));
  await parts(topic).button.handlers.click(); steps.push(state(topic));
  console.log(JSON.stringify({ topics: els["tp-tree"].children.length, steps, detail, title, calls, events }));
})();
"""


def test_tree_click_opens_and_closes_levels_and_event_shows_all_outcomes():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    tree = json.dumps({"as_of": "2026-10-16T12:00:00+00:00", "topics": build_tree(MARKETS)})
    nodes = json.dumps({"nodes": subtopic_nodes(MARKETS, "Politics", "Elections")})
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "topics.js"), tree, nodes], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["topics"] == 3
    topic_open, sub_open, event_open, topic_closed, topic_reopened = r["steps"]
    assert topic_open == {"open": True, "expanded": "true", "items": 2}
    assert sub_open == {"open": True, "expanded": "true", "items": 1}
    assert event_open == {"open": True, "expanded": "true", "items": 3}  # all three outcomes as leaves
    assert (r["detail"], r["title"]) == (3, ELECTION)  # selection shows the event's outcome rows
    assert topic_closed == {"open": False, "expanded": "false", "items": 2}
    assert topic_reopened["open"] is True
    assert r["calls"] == ["/topics", "/topics/nodes?topic=Politics&subtopic=Elections"]  # nodes load once, lazily
    assert "topics.load" in r["events"] and r["events"].count("topics.node.toggle") == 5
