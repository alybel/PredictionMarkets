// Tab "Metriken": metric catalog with status, check results, chart and release date per metric; buttons
// "Freigeben" (only when all checks are green; the backend refuses otherwise with 409) and "Sperren"; liquidity filter.
// Uses $, logEvent (shell.js / indices.js), qualCheckRow (quality.js), fedNode, fedAxisTicks, fedDay, fedDate,
// fedBp (fedchains.js) and fedServerError (fedstaging.js); every click goes to app.log via logEvent.

const METRIC_W = 800, METRIC_H = 160, METRIC_PAD = 16;
const METRIC_STATUS = { draft: ["draft", ""], validated: ["validated", "ok"], blocked: ["blocked", "bad"] };
let metricsLoaded = false, metricsAll = [];

// Liquidity (1015-2355): Ø daily 24h turnover of the last 30 days; a minimum hides metrics below it or without data.
const metricUsd = (v) => (v == null ? "–" : Math.round(v).toLocaleString("de-DE") + " USD");
const metricLiquidityPasses = (liq, min) => !(min > 0) || (liq != null && liq >= min);
const metricMinLiquidity = (id) => Math.max(0, Number($(id).value) || 0);

// Reference point for the filter (1016-1007): median of the current liquidity (Ø 30 T) from the backend; a click takes it
// over as minimum. The filter acts on the current edge only, the history of a shown metric stays complete.
function showLiquidityMedian(boxId, inputId, med, apply) {
  const box = $(boxId), parts = [];
  if (med && med.usd != null) {
    const take = Object.assign(document.createElement("button"), { type: "button", className: "link", textContent: "übernehmen" });
    take.addEventListener("click", () => { $(inputId).value = String(Math.round(med.usd)); apply(med.usd); });
    parts.push("Anhaltspunkt: Median der aktuellen Liquidität " + metricUsd(med.usd) + " (" + med.metrics + " von " + med.of +
      " Metriken mit Umsatzdaten) ", take, ". ");
  } else parts.push("Anhaltspunkt: kein Median – keine Metrik mit Umsatzdaten. ");
  parts.push("Der Filter wirkt nur am aktuellen Rand (Ø der letzten 30 Tage), nicht historisch.");
  box.replaceChildren(...parts);
}

const metricStamp = (iso) => (iso ? new Date(iso).toLocaleString("de-DE", { timeZone: "UTC" }) + " UTC" : "–");

function metricChart(series) {
  const wrap = document.createElement("div");
  const svg = fedNode("svg", { class: "metric-chart", viewBox: `0 0 ${METRIC_W} ${METRIC_H}`, preserveAspectRatio: "none", role: "img" });
  const axis = Object.assign(document.createElement("div"), { className: "fed-xaxis" });
  wrap.append(svg, axis);
  if (!series.length) {
    wrap.replaceChildren(Object.assign(document.createElement("p"), { className: "muted", textContent: "Keine Werte vorhanden." }));
    return wrap;
  }
  const x0 = fedDay(series[0].date), x1 = Math.max(fedDay(series[series.length - 1].date), x0 + 1);
  let lo = Math.min(0, ...series.map((p) => p.value)), hi = Math.max(0, ...series.map((p) => p.value));
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const x = (t) => METRIC_PAD + ((t - x0) / (x1 - x0)) * (METRIC_W - 2 * METRIC_PAD);
  const y = (v) => METRIC_H - METRIC_PAD - ((v - lo) / (hi - lo)) * (METRIC_H - 2 * METRIC_PAD);
  const ns = { "vector-effect": "non-scaling-stroke" };
  svg.appendChild(fedNode("line", { class: "zero", x1: METRIC_PAD, x2: METRIC_W - METRIC_PAD, y1: y(0), y2: y(0), ...ns }));
  for (const p of series.filter((q) => q.roll)) {
    svg.appendChild(fedNode("line", { class: "roll", x1: x(fedDay(p.date)), x2: x(fedDay(p.date)), y1: 4, y2: METRIC_H - 4, ...ns }));
  }
  const d = series.map((p, i) => (i ? "L" : "M") + x(fedDay(p.date)).toFixed(1) + " " + y(p.value).toFixed(1)).join(" ");
  svg.appendChild(fedNode("path", { class: "line", d, ...ns }));
  for (const tk of fedAxisTicks(x0, x1)) {
    const lab = document.createElement("span");
    lab.textContent = tk.label;
    lab.style.left = (x(tk.t) / METRIC_W * 100).toFixed(2) + "%";
    axis.appendChild(lab);
  }
  svg.setAttribute("aria-label", "Verlauf " + fedDate(series[0].date) + " – " + fedDate(series[series.length - 1].date));
  return wrap;
}

function metricCard(m) {
  const card = Object.assign(document.createElement("div"), { className: "card metric-card" });
  card.dataset.metric = m.key;
  const head = Object.assign(document.createElement("div"), { className: "card-head" });
  const title = document.createElement("div");
  const [label, cls] = METRIC_STATUS[m.status] || [m.status, ""];
  const badge = Object.assign(document.createElement("span"), { className: "badge " + cls, textContent: label });
  const h2 = Object.assign(document.createElement("h2"), { textContent: m.name + " " });
  h2.appendChild(badge);
  const last = m.series.length ? m.series[m.series.length - 1] : null;
  const facts = [
    "Einheit: " + m.unit, "Quelle: " + m.source,
    "Freigabe: " + (m.validated_at && m.status === "validated" ? metricStamp(m.validated_at) : "–"),
    m.status === "blocked" ? "Gesperrt seit: " + metricStamp(m.blocked_at) : null,
    "Geprüft: " + metricStamp(m.checked_at),
    "Liquidität (Ø 30 T): " + metricUsd(m.liquidity_usd),
    last ? "Letzter Wert: " + (m.unit === "bp" ? fedBp(last.value) + " bp" : (last.value * 100).toFixed(1) + " %") + " (" + fedDate(last.date) + ")" : null,
  ].filter(Boolean);
  title.append(h2, Object.assign(document.createElement("p"), { className: "hint", textContent: m.calculation }),
    Object.assign(document.createElement("p"), { className: "hint metric-facts", textContent: facts.join(" · ") }));
  if (m.status_reason) title.appendChild(Object.assign(document.createElement("p"), { className: "hint metric-reason", textContent: m.status_reason }));
  const controls = Object.assign(document.createElement("div"), { className: "controls" });
  const approveBtn = Object.assign(document.createElement("button"), { type: "button", textContent: "Freigeben", disabled: !m.can_approve });
  approveBtn.title = m.all_green ? (m.status === "validated" ? "Bereits freigegeben" : "Alle Prüfungen grün") : "Erst möglich, wenn alle Prüfungen grün sind";
  approveBtn.dataset.action = "approve";
  const blockBtn = Object.assign(document.createElement("button"), { type: "button", className: "danger", textContent: "Sperren", disabled: m.status === "blocked" });
  blockBtn.dataset.action = "block";
  approveBtn.addEventListener("click", () => metricAction(m.key, "approve"));
  blockBtn.addEventListener("click", () => metricAction(m.key, "block"));
  controls.append(approveBtn, blockBtn);
  head.append(title, controls);
  const table = document.createElement("div");
  table.className = "table-scroll";
  table.innerHTML = '<table class="qual-checks"><thead><tr><th>Status</th><th>Prüfung</th><th class="num">Anzahl</th><th>Bedeutung</th></tr></thead><tbody></tbody></table>';
  table.querySelector("tbody").replaceChildren(...m.checks.map(qualCheckRow));
  card.append(head, metricChart(m.series), table);
  return card;
}

function renderMetrics(metrics) {
  metricsAll = metrics;
  const min = metricMinLiquidity("metrics-minliq");
  const shown = metrics.filter((m) => metricLiquidityPasses(m.liquidity_usd, min));
  $("metrics-list").replaceChildren(...shown.map(metricCard));
  const counts = shown.reduce((a, m) => ({ ...a, [m.status]: (a[m.status] || 0) + 1 }), {});
  $("metrics-status").textContent = (shown.length === metrics.length ? metrics.length : shown.length + " von " + metrics.length) +
    " Metriken" + (min > 0 ? " ab " + metricUsd(min) + " Liquidität" : "") + " · " + Object.entries(counts).map(([k, v]) => v + " " + k).join(" · ");
  return shown;
}

async function loadMetrics(reason) {
  $("metrics-status").textContent = "Lade Metrik-Katalog …";
  logEvent("info", "metrics.load", { reason });
  try {
    const res = await fetch(reason === "check" ? "/metrics/check" : "/metrics", { method: reason === "check" ? "POST" : "GET" });
    if (!res.ok) throw new Error(await fedServerError(res));
    const data = await res.json();
    renderMetrics(data.metrics);
    showLiquidityMedian("metrics-liqmedian", "metrics-minliq", data.liquidity_median, metricsApplyMinLiquidity);
    metricsLoaded = true;
    logEvent("info", "metrics.loaded", { reason, status: Object.fromEntries(data.metrics.map((m) => [m.key, m.status])) });
  } catch (err) {
    $("metrics-status").textContent = "Metrik-Katalog fehlgeschlagen: " + err.message;
    logEvent("error", "metrics.failed", { reason, message: err.message });
  }
}

async function metricAction(key, action) {
  logEvent("info", "metrics." + action + ".click", { key });
  try {
    const res = await fetch("/metrics/" + encodeURIComponent(key) + "/" + action, { method: "POST" });
    if (!res.ok) throw new Error(await fedServerError(res));
    logEvent("info", "metrics." + action + ".done", { key, status: (await res.json()).status });
    await loadMetrics(action);
  } catch (err) {
    await loadMetrics(action + "_failed");
    $("metrics-status").textContent = (action === "approve" ? "Freigabe" : "Sperre") + " fehlgeschlagen: " + err.message;
    logEvent("error", "metrics." + action + ".failed", { key, message: err.message });
  }
}

function metricsFilterChanged() {
  const shown = renderMetrics(metricsAll);
  logEvent("info", "metrics.liquidity.filter", { min: metricMinLiquidity("metrics-minliq"), shown: shown.map((m) => m.key) });
}

function metricsApplyMinLiquidity(usd) {
  logEvent("info", "metrics.liquidity.median.click", { median: usd });
  metricsFilterChanged();
}

$("metrics-minliq").addEventListener("change", metricsFilterChanged);
$("metrics-check").addEventListener("click", () => { logEvent("info", "metrics.check.click", {}); loadMetrics("check"); });
document.querySelector('.tab[data-view="metrics"]').addEventListener("click", () => { if (!metricsLoaded) loadMetrics("tab"); });
if (!document.querySelector('[data-view-panel="metrics"]').classList.contains("hidden")) loadMetrics("start");
