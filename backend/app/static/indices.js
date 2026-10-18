// Index builder (view "indices"): index buttons, on-demand value, constituents table with
// filter/sort for archived rule indices. Globals ($, state, render, loadIndex, ...) are used by builder.js and metricindex.js.

const $ = (id) => document.getElementById(id);
const fmtNum = (v, digits = 0) =>
  v == null ? "–" : Number(v).toLocaleString("de-DE", { maximumFractionDigits: digits });
const METHOD_LABELS = {
  liquidity_weighted: "liquiditätsgewichtet",
  simple_average: "einfacher Durchschnitt",
  no_data: "keine Daten",
};

// Interactive view state: stored rules, last payload plus current sort/filter selection.
// Constituents are listed by weight (largest first) unless the user picks another column.
const state = { name: null, rules: {}, data: null, sortKey: "weight", sortDir: -1, filter: "", source: "" };

// (Re)builds the index buttons: metric indices (metricindex.js) first, then the read-only archive of
// rule-based indices; returns all names.
async function loadIndices() {
  try {
    const [archiveRes, metricRes] = await Promise.all([fetch("/indices"), fetch("/builder/indices")]);
    if (!archiveRes.ok) throw new Error("HTTP " + archiveRes.status);
    if (!metricRes.ok) throw new Error("HTTP " + metricRes.status);
    const archive = await archiveRes.json();
    const metricIndices = (await metricRes.json()).indices;
    state.rules = Object.fromEntries(archive.map((idx) => [idx.index_name, idx]));
    const container = $("index-buttons");
    container.replaceChildren();
    for (const idx of metricIndices) {
      container.appendChild(indexButton(idx.name, (idx.warning ? "⚠ " : "") + idx.name, false,
        (btn) => loadMetricIndex(idx.name, btn)));
    }
    for (const idx of archive) {
      container.appendChild(indexButton(idx.index_name, idx.index_name + " · Archiv", true, (btn) => loadIndex(idx.index_name, btn)));
    }
    const names = metricIndices.map((i) => i.name).concat(archive.map((i) => i.index_name));
    if (!names.length) {
      $("result").classList.add("hidden");
      setStatus("Keine Indizes definiert – „+ Neuer Index“ baut einen aus validierten Metriken.", false);
    } else {
      setStatus("Index anklicken, um ihn abzurufen.", false);
    }
    return names;
  } catch (err) {
    setStatus("Indizes konnten nicht geladen werden: " + err.message, true);
    logEvent("error", "indices.load.failed", { message: err.message });
    return [];
  }
}

function indexButton(name, label, archived, open) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = label;
  btn.dataset.name = name;
  btn.classList.toggle("archived", archived);
  btn.title = archived ? "Regelbasierter Index (Archiv)" : "Index aus validierten Metriken";
  btn.addEventListener("click", () => { logEvent("info", "index.select", { name, archived }); open(btn); });
  return btn;
}

function selectIndex(name) {
  const btn = document.querySelector(`#index-buttons button[data-name="${CSS.escape(name)}"]`);
  if (btn) btn.click();
}

// Filter funnel of the rule engine: survivor count after each stage.
const FUNNEL_LABELS = [
  ["loaded", "Geladen"], ["tags", "nach Tags"], ["keywords", "nach Keywords"],
  ["liquidity", "nach Liquidität"], ["price", "nach Preis-Korridor"], ["end_date", "nach Enddatum"],
];

async function loadIndex(name, btn, refresh = false) {
  state.name = name;
  document.querySelectorAll("#index-buttons button").forEach((b) => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  setStatus("Rufe „" + name + "“ ab …", false);
  $("result").classList.add("hidden");
  $("mi-result").classList.add("hidden");
  $("mi-fc-slot").classList.add("hidden"); // forecast report (forecast.js) belongs to metric indices only
  try {
    const res = await fetch("/indices/" + encodeURIComponent(name) + (refresh ? "?refresh=1" : ""));
    if (!res.ok) throw new Error("HTTP " + res.status);
    render(await res.json());
    setStatus("", false);
    document.dispatchEvent(new CustomEvent("index-loaded", { detail: { name } }));
  } catch (err) {
    setStatus("Abruf fehlgeschlagen: " + err.message, true);
    logEvent("error", "index.load.failed", { name, message: err.message });
  }
}

function render(data) {
  state.data = data;
  $("result-title").textContent = data.index_name || state.name || "Index";
  $("value").textContent = data.value == null ? "–" : (data.value * 100).toFixed(1) + " %";
  $("method").textContent = METHOD_LABELS[data.method] || data.method;
  $("market-count").textContent = data.market_count;
  $("total-liquidity").textContent = fmtNum(data.total_liquidity);
  $("total-volume").textContent = fmtNum(data.total_volume);
  $("as-of").textContent = new Date(data.as_of).toLocaleString("de-DE");
  renderFunnel(data.funnel);
  assignWeights(data);
  renderConstituentSummary(data.markets);

  const select = $("source-filter");
  const sources = [...new Set(data.markets.map((m) => m.source))].sort();
  select.replaceChildren(new Option("Alle Quellen", ""));
  for (const s of sources) select.appendChild(new Option(s, s));
  if (sources.includes(state.source)) select.value = state.source; else state.source = "";

  renderTable();
  $("result").classList.remove("hidden");
}

// Share of each market in the consolidated value (mirrors backend.app.aggregation).
function assignWeights(data) {
  const priced = data.markets.filter((m) => m.price != null);
  const total = priced.reduce((sum, m) => sum + (m.liquidity || 0), 0);
  for (const m of data.markets) {
    if (m.price == null) m.weight = 0;
    else m.weight = total > 0 ? (m.liquidity || 0) / total : 1 / priced.length;
  }
  [...data.markets].sort((a, b) => b.weight - a.weight).forEach((m, i) => { m.rank = i + 1; });
}

// One line above the constituents table: count per source, concentration, unpriced markets.
function renderConstituentSummary(markets) {
  const el = $("constituents-summary");
  if (!markets.length) { el.textContent = "Keine Bestandteile – kein Markt erfüllt das Regelwerk."; return; }
  const perSource = {};
  for (const m of markets) perSource[m.source] = (perSource[m.source] || 0) + 1;
  const parts = Object.entries(perSource).sort((a, b) => b[1] - a[1]).map(([s, n]) => s + " " + fmtNum(n));
  const top = [...markets].sort((a, b) => b.weight - a.weight).slice(0, 10);
  const topShare = top.reduce((sum, m) => sum + m.weight, 0);
  parts.push("Top " + top.length + " tragen " + (topShare * 100).toFixed(1).replace(".", ",") + " % des Gewichts");
  const unpriced = markets.filter((m) => m.price == null).length;
  if (unpriced) parts.push(fmtNum(unpriced) + " ohne Preis (Gewicht 0)");
  el.textContent = parts.join(" · ");
}

function renderFunnel(funnel) {
  const el = $("funnel");
  if (!funnel) { el.classList.add("hidden"); return; }
  el.replaceChildren();
  FUNNEL_LABELS.forEach(([key, label], i) => {
    if (i > 0) el.appendChild(document.createTextNode(" → "));
    const strong = document.createElement("strong");
    strong.textContent = fmtNum(funnel[key]);
    el.appendChild(document.createTextNode(label + " "));
    el.appendChild(strong);
  });
  el.classList.remove("hidden");
}

const marketTags = (m) => (m.tags && m.tags.length ? m.tags : (m.category ? [m.category] : []));

function visibleMarkets() {
  const q = state.filter.trim().toLowerCase();
  let rows = state.data.markets.filter(
    (m) =>
      (!state.source || m.source === state.source) &&
      (!q || (m.title + " " + marketTags(m).join(" ")).toLowerCase().includes(q))
  );
  if (state.sortKey) {
    const key = state.sortKey, dir = state.sortDir;
    rows = [...rows].sort((a, b) => {
      const [x, y] = [a[key], b[key]];
      if (x == null) return 1;
      if (y == null) return -1;
      const cmp = typeof x === "number" ? x - y : String(x).localeCompare(String(y), "de");
      return cmp * dir;
    });
  }
  return rows;
}

function renderTable() {
  document.querySelectorAll("th[data-key]").forEach((th) => {
    th.querySelector(".arrow").textContent =
      th.dataset.key === state.sortKey ? (state.sortDir === 1 ? "▲" : "▼") : "";
  });
  const rows = visibleMarkets();
  $("visible-count").textContent = rows.length;
  $("empty-note").classList.toggle("hidden", rows.length > 0);
  const tbody = $("markets");
  tbody.replaceChildren();
  for (const m of rows) {
    const row = document.createElement("tr");
    const cells = [
      [String(m.rank), "num"], [sourcePill(m.source), ""], [marketTitle(m), ""], [marketTags(m).join(", ") || "–", "tags optional"],
      [m.price == null ? "–" : Number(m.price).toFixed(3), "num"],
      [fmtNum(m.volume), "num"], [liquidityCell(m), "num"],
      [(m.weight * 100).toFixed(1) + " %", "num"],
    ];
    for (const [content, cls] of cells) {
      const td = document.createElement("td");
      if (content instanceof Node) td.appendChild(content); else td.textContent = content;
      if (cls) td.className = cls;
      row.appendChild(td);
    }
    tbody.appendChild(row);
  }
}

function sourcePill(source) {
  const span = document.createElement("span");
  span.className = "source-pill";
  span.textContent = source;
  return span;
}

function marketTitle(m) {
  if (!m.url) return m.title;
  const a = document.createElement("a");
  a.href = m.url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = m.title;
  return a;
}

$("kpi-constituents").addEventListener("click", (e) => {
  e.preventDefault();
  logEvent("info", "button.click", { id: "kpi-constituents", name: state.name });
  $("constituents-card").scrollIntoView({ behavior: "smooth", block: "start" });
});
$("market-filter").addEventListener("input", (e) => {
  state.filter = e.target.value;
  logEvent("debug", "filter.change", { value: state.filter });
  if (state.data) renderTable();
});
$("source-filter").addEventListener("change", (e) => {
  state.source = e.target.value;
  logEvent("info", "source.filter", { source: state.source });
  if (state.data) renderTable();
});
$("refresh").addEventListener("click", () => {
  if (!state.name) return;
  logEvent("info", "button.click", { id: "refresh", name: state.name });
  loadIndex(state.name, activeButton(), true);
});
document.querySelectorAll("th[data-key]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    state.sortDir = state.sortKey === key ? -state.sortDir : 1;
    state.sortKey = key;
    logEvent("debug", "sort.change", { key, dir: state.sortDir });
    if (state.data) renderTable();
  });
});

// Deletes a metric index (DELETE /builder/indices/{name}) or an archive index (DELETE /indices/{name})
// after a confirmation; reloads the index buttons afterwards.
async function deleteIndex(name, archived) {
  logEvent("info", "button.click", { id: archived ? "delete-archive-index" : "mi-delete", name, archived });
  if (!confirm("Index „" + name + "“ endgültig löschen?")) {
    logEvent("info", "index.delete.cancelled", { name, archived });
    return;
  }
  const url = (archived ? "/indices/" : "/builder/indices/") + encodeURIComponent(name);
  try {
    const res = await fetch(url, { method: "DELETE" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "HTTP " + res.status);
  } catch (err) {
    setStatus("Löschen fehlgeschlagen: " + err.message, true);
    logEvent("error", "index.delete.failed", { name, archived, message: err.message });
    return;
  }
  logEvent("info", "index.deleted", { name, archived });
  state.name = null;
  parkCorrelation(); // the stock card of a deleted metric index goes back into the hidden archive view
  $("result").classList.add("hidden");
  $("mi-result").classList.add("hidden");
  $("mi-fc-slot").classList.add("hidden"); // forecast report (forecast.js) belongs to metric indices only
  await loadIndices();
  setStatus("Index „" + name + "“ gelöscht.", false);
}

$("delete-archive-index").addEventListener("click", () => { if (state.name) deleteIndex(state.name, true); });

const activeButton = () => document.querySelector("#index-buttons button.active");

function setStatus(text, isError) {
  const el = $("status");
  el.textContent = text;
  el.classList.toggle("error", isError);
  el.classList.toggle("hidden", !text);
}

loadIndices();
