// Tab "Themen": collapsible tree topic -> subtopic -> event (with its outcome markets) or market. Topics come from
// GET /topics, the nodes of a subtopic load on first open (GET /topics/nodes). Clicking a node opens/closes its
// level and shows its data on the right. Uses $, fmtNum and logEvent; loads on first view.

const tpState = { loaded: false, open: new Set(), nodes: {} };

const tpPrice = (v) => (v == null ? "–" : (v * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " %");
const tpCount = (n) => [n.events ? fmtNum(n.events) + " Events" : "", n.markets ? fmtNum(n.markets) + " Märkte" : ""].filter(Boolean).join(" · ");

function tpEl(tag, props, ...children) {
  const el = Object.assign(document.createElement(tag), props || {});
  el.append(...children);
  return el;
}

// Flips one node's open state; returns true when it is open now.
function tpToggle(key) {
  if (tpState.open.has(key)) { tpState.open.delete(key); return false; }
  tpState.open.add(key);
  return true;
}

function tpLink(url) {
  return url ? tpEl("a", { href: url, target: "_blank", rel: "noopener", textContent: "öffnen" }) : "–";
}

// ---- detail panel --------------------------------------------------------------------------

function tpShow(title, meta, headers, rows) {
  $("tp-detail-title").textContent = title;
  $("tp-detail-meta").textContent = meta;
  $("tp-detail-head").replaceChildren(tpEl("tr", null, ...headers.map(([h, cls]) => tpEl("th", { textContent: h, className: cls || "" }))));
  $("tp-detail-rows").replaceChildren(...rows.map((cells) => tpEl("tr", null, ...cells.map((c, i) =>
    tpEl("td", { className: headers[i][1] || "" }, c)))));
  $("tp-detail").classList.remove("hidden");
}

const TP_MARKET_HEAD = [["Outcome / Markt"], ["Preis", "num"], ["24h-Volumen (USD)", "num"], ["Liquidität (USD)", "num"], ["Link"]];
const tpMarketRow = (m) => [m.outcome_label || m.title, tpPrice(m.price), fmtNum(m.volume_24h), fmtNum(m.liquidity), tpLink(m.url)];

function tpShowTopic(t) {
  tpShow(t.name, "Thema · " + tpCount(t), [["Unterthema"], ["Events", "num"], ["Märkte", "num"]],
    t.subtopics.map((s) => [s.name, fmtNum(s.events), fmtNum(s.markets)]));
}

function tpShowSubtopic(topic, sub, nodes) {
  tpShow(topic + " › " + sub.name, "Unterthema · " + tpCount(sub), [["Event / Markt"], ["Quelle"], ["Outcomes", "num"], ["24h-Volumen (USD)", "num"]],
    nodes.map((n) => [n.title, n.source, n.kind === "event" ? fmtNum(n.outcome_count) : "–", fmtNum(n.volume_24h)]));
}

function tpShowEvent(n) {
  tpShow(n.title, "Event · " + n.source + " · " + n.outcome_count + " Outcomes · 24h-Volumen " + fmtNum(n.volume_24h) + " USD",
    TP_MARKET_HEAD, n.outcomes.map(tpMarketRow));
}

function tpShowMarket(m) {
  tpShow(m.title, "Markt · " + m.source + (m.end_date ? " · Ende " + new Date(m.end_date).toLocaleDateString("de-DE") : ""),
    TP_MARKET_HEAD, [tpMarketRow(m)]);
}

// ---- tree ----------------------------------------------------------------------------------

// One expandable node: the first open fills its children (fill may be async), every click selects it.
function tpBranch(key, label, meta, fill, select, kind) {
  const children = tpEl("ul", { className: "tree-children hidden" });
  const caret = tpEl("span", { className: "tree-caret", textContent: "▸" });
  const button = tpEl("button", { type: "button", className: "tree-node " + kind }, caret, label, tpEl("span", { className: "muted", textContent: " " + meta }));
  button.setAttribute("aria-expanded", "false");
  let filled = false;
  button.addEventListener("click", async () => {
    const open = tpToggle(key);
    logEvent("info", "topics.node.toggle", { kind, key, open });
    button.setAttribute("aria-expanded", String(open));
    caret.textContent = open ? "▾" : "▸";
    children.classList.toggle("hidden", !open);
    tpSelect(button);
    if (open && !filled) {
      filled = true;
      try { children.replaceChildren(...await fill()); } catch (err) {
        filled = false;
        children.replaceChildren(tpEl("li", { className: "error-text", textContent: "Laden fehlgeschlagen: " + err.message }));
        logEvent("error", "topics.nodes.failed", { key, message: err.message });
        return;
      }
    }
    select();
  });
  return tpEl("li", null, button, children);
}

function tpSelect(button) {
  document.querySelectorAll(".tree-node.selected").forEach((b) => b.classList.remove("selected"));
  if (button) button.classList.add("selected");
}

function tpOutcomeLeaf(o) {
  return tpEl("li", { className: "tree-leaf" }, (o.outcome_label || o.title) + " ", tpEl("span", { className: "muted", textContent: tpPrice(o.price) }));
}

function tpNode(n) {
  if (n.kind === "event") {
    return tpBranch(n.id, n.title, "(" + n.outcome_count + " Outcomes)", () => n.outcomes.map(tpOutcomeLeaf), () => tpShowEvent(n), "event");
  }
  const button = tpEl("button", { type: "button", className: "tree-node market", textContent: n.title });
  button.addEventListener("click", () => {
    logEvent("info", "topics.market.select", { id: n.id });
    tpSelect(button);
    tpShowMarket(n);
  });
  return tpEl("li", null, button);
}

async function tpLoadNodes(topic, sub) {
  const key = JSON.stringify([topic, sub]);
  if (!tpState.nodes[key]) {
    const res = await fetch("/topics/nodes?topic=" + encodeURIComponent(topic) + "&subtopic=" + encodeURIComponent(sub));
    if (!res.ok) throw new Error("HTTP " + res.status);
    tpState.nodes[key] = (await res.json()).nodes;
  }
  return tpState.nodes[key];
}

function tpTopicNode(t) {
  const subs = t.subtopics.map((s) => tpBranch(JSON.stringify([t.name, s.name]), s.name, "(" + tpCount(s) + ")",
    async () => (await tpLoadNodes(t.name, s.name)).map(tpNode),
    () => tpShowSubtopic(t.name, s, tpState.nodes[JSON.stringify([t.name, s.name])] || []), "subtopic"));
  return tpBranch(JSON.stringify([t.name]), t.name, "(" + tpCount(t) + ")", () => subs, () => tpShowTopic(t), "topic");
}

function renderTopics(topics) {
  $("tp-tree").replaceChildren(...topics.map(tpTopicNode));
  $("tp-status").textContent = topics.length ? topics.length + " Themen" : "Keine Themen – der Markt-Cache ist leer.";
}

async function loadTopics(reason) {
  logEvent("info", "topics.load", { reason });
  Object.assign(tpState, { loaded: true, open: new Set(), nodes: {} });
  $("tp-status").textContent = "Lade Themen …";
  $("tp-detail").classList.add("hidden");
  try {
    const res = await fetch("/topics");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    renderTopics(data.topics);
    $("tp-time").textContent = "Stand " + new Date(data.as_of).toLocaleTimeString("de-DE");
  } catch (err) {
    $("tp-status").textContent = "Themen konnten nicht geladen werden: " + err.message;
    logEvent("error", "topics.load.failed", { message: err.message });
  }
}

$("tp-refresh").addEventListener("click", () => loadTopics("button"));
document.querySelector('.tab[data-view="topics"]').addEventListener("click", () => { if (!tpState.loaded) loadTopics("tab"); });
if (!document.querySelector('[data-view-panel="topics"]').classList.contains("hidden")) loadTopics("start");
