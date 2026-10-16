// Stock correlation card of the index builder: index change on snapshot day T vs. the next-day return
// of the top-100 US stocks (GET /indices/{name}/correlation, POST .../correlation/preview for unsaved rules).
// Metric indices (1015-2231) use the same card via GET /builder/indices/{name}/correlation (kind "metric",
// index unit z-points, target switch, market row and regression columns from regression.js).
// Relies on the globals of shell.js, indices.js and builder.js ($, logEvent, state, builder).

const CR_KEY = "pmi.correlation";
const corr = { request: 0, kind: "archive", name: null, rule: null, data: null, ticker: null, sortKey: null, sortDir: -1,
  minChange: 0, minChangeZ: 0, target: "excess", minStockMove: 0.01, onlySignificant: false };
try { Object.assign(corr, JSON.parse(localStorage.getItem(CR_KEY) || "{}")); } catch {}

const signedPct = (v, digits = 2) => (v == null ? "–" : (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + " %");
const fmtCorr = (v) => (v == null ? "–" : (v >= 0 ? "+" : "") + v.toFixed(2));
const fmtP = (v) => (v == null ? "–" : v < 0.001 ? "< 0,001" : v.toFixed(3).replace(".", ","));
const fmtMove = (v) => (v * 100).toLocaleString("de-DE", { maximumFractionDigits: 2 }) + " %";
const fmtShare = (v) => (v == null ? "–" : (v * 100).toFixed(0) + " %");

// "62 % (40 von 61)"; with a negative correlation also the opposite rate ("38 % · gegenläufig 62 %")
function hitRateText(r) {
  if (r.hit_rate == null) return "–";
  const text = fmtShare(r.hit_rate) + " (" + (r.events_up + r.events_down) + " von " + r.n + ")";
  return r.correlation != null && r.correlation < 0 ? text + " · gegenläufig " + fmtShare(1 - r.hit_rate) : text;
}

function saveCorrSettings() {
  try {
    localStorage.setItem(CR_KEY, JSON.stringify({ minChange: corr.minChange, minChangeZ: corr.minChangeZ, target: corr.target,
      minStockMove: corr.minStockMove, onlySignificant: corr.onlySignificant }));
  } catch {}
}

// Index change that counts as an event: index points (archive) or z-points (metric index)
const corrMinChange = () => (corr.kind === "metric" ? corr.minChangeZ : corr.minChange);

// Window in days: metric indices use their own range switch (mi.days, metricindex.js; 1015-2258), archive the builder's
const corrDays = () => (corr.kind === "metric" ? mi.days : builder.days);

// Base URL of the correlation endpoints of the shown index; metric indices also send the target return
function corrUrl(suffix, extra) {
  const metric = corr.kind === "metric";
  return (metric ? "/builder/indices/" : "/indices/") + encodeURIComponent(corr.name) + "/correlation" + suffix +
    "?days=" + corrDays() + "&min_change=" + corrMinChange() + (extra || "") + (metric ? "&target=" + corr.target : "");
}

async function loadCorrelation(name, rule) {
  const ticket = ++corr.request;
  corr.name = name;
  corr.rule = rule || null;
  $("cr-caption").textContent = "Korrelation wird berechnet …";
  const url = corrUrl(rule ? "/preview" : "", "&min_stock_move=" + corr.minStockMove);
  try {
    const res = await fetch(url, rule
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rule) }
      : undefined);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (ticket !== corr.request) return; // a newer request is under way
    corr.data = data;
    renderCorrelation();
    document.dispatchEvent(new CustomEvent("correlation-loaded"));
    logEvent("info", "correlation.loaded", { name, kind: corr.kind, target: data.target, days: data.days, index_days: data.index_days,
      tested: data.stocks_tested, significant: data.significant, regression_significant: data.regression_significant,
      collinearity_warning: data.collinearity?.warning, preview: !!rule,
      total_signals: data.total_signals, below_min_signals: data.stocks_below_min_signals });
  } catch (err) {
    if (ticket !== corr.request) return;
    corr.data = null;
    renderCorrelation();
    $("cr-caption").textContent = "Korrelation konnte nicht geladen werden: " + err.message;
    logEvent("error", "correlation.failed", { name, kind: corr.kind, message: err.message });
  }
}

function sortedCorrRows(rows) {
  if (!corr.sortKey) return rows; // backend order: strongest |correlation| first
  const key = corr.sortKey;
  return [...rows].sort((a, b) => {
    const va = a[key], vb = b[key];
    if (va == null || vb == null) return va == null ? (vb == null ? 0 : 1) : -1; // empty values last
    return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * corr.sortDir;
  });
}

function corrCell(tr, text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  tr.appendChild(td);
}

function renderCorrelation() {
  const d = corr.data;
  $("cr-days").textContent = d ? d.index_days : "–";
  $("cr-tested").textContent = d ? d.stocks_tested : "–";
  $("cr-significant").textContent = d ? d.significant : "–";
  $("cr-expected").textContent = d ? "≈ " + String(d.expected_false_positives).replace(".", ",") : "–";
  $("cr-prices").textContent = d ? d.stocks_with_prices + " / 100" : "–";
  document.querySelectorAll("#cr-card th[data-cr-sort]").forEach((th) => {
    th.querySelector(".arrow").textContent = th.dataset.crSort === corr.sortKey ? (corr.sortDir > 0 ? "▲" : "▼") : "";
  });
  const body = $("cr-rows");
  body.replaceChildren();
  if (!d) return;
  const rows = sortedCorrRows(d.stocks.filter((r) => r.n > 0 && (!corr.onlySignificant || r.significant)));
  if (corr.kind === "metric") renderMetricExtras(d, body); // regression.js: warning, KPIs, market row
  for (const r of rows) body.appendChild(corrRow(r, d, true));
  $("cr-caption").textContent = corr.kind === "metric" ? metricCaption(d, rows.length) : correlationCaption(d, rows.length);
}

// One table row; ``clickable`` rows open the trade view (the market row of a metric index is not clickable)
function corrRow(r, d, clickable) {
  const tr = document.createElement("tr");
  if (clickable) {
    tr.className = "clickable" + (r.ticker === corr.ticker ? " selected" : "");
    tr.title = "Klicken: Performance, Renditeverläufe und Trades anzeigen";
    tr.addEventListener("click", () => openTrades(r.ticker)); // trades.js
  }
  const td = document.createElement("td");
  const strong = document.createElement("strong");
  strong.textContent = r.ticker;
  td.append(strong, document.createTextNode(" " + r.name));
  if (r.significant) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "p < 5 %";
    td.append(" ", badge);
  }
  tr.appendChild(td);
  corrCell(tr, r.n, "num");
  corrCell(tr, fmtCorr(r.correlation), "num " + (r.correlation == null ? "" : r.correlation >= 0 ? "up" : "down"));
  corrCell(tr, fmtP(r.p_value), "num");
  corrCell(tr, hitRateText(r), "num");
  corrCell(tr, signedPct(r.mean_return_after_up) + " (" + r.events_up + ")", "num");
  corrCell(tr, signedPct(r.mean_return_after_down) + " (" + r.events_down + ")", "num");
  corrCell(tr, signedPct(r.slope_per_point, 3), "num");
  if (corr.kind === "metric") regressionCells(tr, r, d); // regression.js
  precisionCells(tr, r, d);
  return tr;
}

// Signal precision of the test half (1015-1157); under min_signals the API sends null -> "zu wenig Daten"
function precisionCells(tr, r, d) {
  corrCell(tr, r.signals, "num sig-col");
  if (r.precision == null) {
    const td = document.createElement("td");
    td.className = "thin";
    td.colSpan = 4;
    td.textContent = r.expected_direction == null ? "keine Richtung bestimmbar" : "zu wenig Daten (< " + d.min_signals + " Signale)";
    tr.appendChild(td);
    return;
  }
  corrCell(tr, fmtShare(r.precision), "num");
  corrCell(tr, fmtShare(r.base_rate), "num");
  corrCell(tr, r.lift == null ? "–" : r.lift.toFixed(2).replace(".", ","), "num " + (r.lift > 1 ? "up" : "down"));
  const td = document.createElement("td");
  td.className = "num";
  td.textContent = fmtP(r.p_signal);
  if (r.signal_significant) {
    const badge = document.createElement("span");
    badge.className = "badge ok";
    badge.textContent = "Bonferroni";
    badge.title = "p (Signal) unter 0,05 ÷ " + d.signal_tested + " getestete Aktien";
    td.append(" ", badge);
  }
  tr.appendChild(td);
}

function correlationCaption(d, shown) {
  if (!d.stocks_with_prices) return "Noch keine Aktienkurse gespeichert – „↻ Kurse laden“ holt die Schlusskurse der Top-100-US-Aktien (erstes Laden dauert etwa eine Minute).";
  if (d.index_days < 2) return "Zu wenige Snapshot-Tage mit Indexwert für eine Analyse – der tägliche Lauf legt sie an.";
  if (!d.stocks_tested) return "Noch zu wenige Paare (mindestens " + d.min_observations + " Handelstage mit Index-Veränderung nötig).";
  return shown + " Aktien angezeigt. Bei " + d.stocks_tested + " getesteten Aktien sind ≈ " + d.expected_false_positives +
    " Treffer mit p < 5 % reiner Zufall; belastbar erst unter p < " + String(+d.bonferroni_alpha.toPrecision(2)).replace(".", ",") +
    " (Bonferroni). Signal-Precision: Richtung aus der ersten Hälfte, gemessen nur auf der zweiten; Treffer = Folgetag über " +
    fmtMove(d.min_stock_move) + " in erwarteter Richtung." +
    " Keine Anlageempfehlung – Korrelation ist keine Kausalität." + (corr.rule ? " · Vorschau mit ungespeicherten Regeln" : "");
}

async function refreshStockPrices() {
  const btn = $("cr-refresh-prices");
  btn.disabled = true;
  $("cr-caption").textContent = "Lade Schlusskurse der Top-100-US-Aktien (inkl. älterer Historie bis 5 Jahre) …";
  logEvent("info", "button.click", { id: "cr-refresh-prices" });
  try {
    const res = await fetch("/stocks/refresh?backfill=true", { method: "POST" }); // also older closes (1015-2258)
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    logEvent(data.failed.length ? "warning" : "info", "stocks.refreshed", { rows_added: data.rows_added, failed: data.failed,
      backfilled: data.backfilled.length, first_date: data.status.first_date });
    if (corr.name) await loadCorrelation(corr.name, corr.rule);
    if (data.failed.length) $("cr-caption").textContent += " · Keine Kurse für: " + data.failed.join(", ");
  } catch (err) {
    $("cr-caption").textContent = "Kurse konnten nicht geladen werden: " + err.message;
    logEvent("error", "stocks.refresh.failed", { message: err.message });
  } finally {
    btn.disabled = false;
  }
}

// ---- wiring -------------------------------------------------------------------------

document.addEventListener("index-loaded", (e) => {
  setCorrMode("archive"); // regression.js
  loadCorrelation(e.detail.name, null);
});
document.querySelectorAll("#ib-range button").forEach((btn) => btn.addEventListener("click", () => {
  if (state.name) loadCorrelation(state.name, corr.rule); // builder.js has already set builder.days
}));
$("cr-refresh-prices").addEventListener("click", refreshStockPrices);
$("cr-min-change").value = String(corr.minChange);
$("cr-min-change").addEventListener("change", (e) => {
  if (corr.kind === "metric") corr.minChangeZ = Number(e.target.value); else corr.minChange = Number(e.target.value);
  saveCorrSettings();
  logEvent("info", "correlation.min_change", { value: corrMinChange(), unit: corr.kind === "metric" ? "z" : "points", name: corr.name });
  if (corr.name) loadCorrelation(corr.name, corr.rule);
});
$("cr-min-stock-move").value = String(corr.minStockMove);
$("cr-min-stock-move").addEventListener("change", (e) => {
  corr.minStockMove = Number(e.target.value);
  saveCorrSettings();
  logEvent("info", "correlation.min_stock_move", { value: corr.minStockMove, name: corr.name });
  if (corr.name) loadCorrelation(corr.name, corr.rule);
});
$("cr-only-significant").checked = corr.onlySignificant;
$("cr-only-significant").addEventListener("change", (e) => {
  corr.onlySignificant = e.target.checked;
  saveCorrSettings();
  logEvent("info", "correlation.filter", { only_significant: corr.onlySignificant });
  if (corr.data) renderCorrelation();
});
document.querySelectorAll("#cr-card th[data-cr-sort]").forEach((th) => th.addEventListener("click", () => {
  const key = th.dataset.crSort;
  corr.sortDir = corr.sortKey === key ? -corr.sortDir : ["ticker", "p_value", "p_signal"].includes(key) ? 1 : -1;
  corr.sortKey = key;
  logEvent("info", "correlation.sort", { key, dir: corr.sortDir });
  if (corr.data) renderCorrelation();
}));
