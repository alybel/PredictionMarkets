// Index builder from validated metrics: selection (GET /builder/metrics, validated only), create (POST) and
// edit (PUT /builder/indices/{name}), standardized series with warning banner (GET /builder/indices/{name}).
// Uses $, state, setStatus, loadIndices, selectIndex (indices.js), logEvent (shell.js), metricChart (metrics.js).
// Every click, save and warning goes to app.log via logEvent.

const MI_JSON = { "Content-Type": "application/json" };
const MI_UNITS = { bp: "bp", probability: "Wahrscheinlichkeit" };
const MI_DAYS_KEY = "pmi.metricindex.days";
const MI_RANGES = [90, 365, 1095, 1825]; // range switch (1015-2258): chart and stock evaluation share the window
const mi = { editing: null, current: null, request: 0, days: 365 };
try { const saved = Number(localStorage.getItem(MI_DAYS_KEY)); if (MI_RANGES.includes(saved)) mi.days = saved; } catch {}

const miZ = (v) => (v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(2));
const miRaw = (v, unit) => (v == null ? "–" : unit === "probability" ? (v * 100).toFixed(1) + " %" : Number(v).toFixed(1) + " " + unit);

async function miFetch(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || "HTTP " + res.status);
  return body;
}

// ---- selection + create / edit -----------------------------------------------------

async function miOpenForm(editing) {
  mi.editing = editing;
  const weights = Object.fromEntries((editing ? editing.components : []).map((c) => [c.key, c.weight]));
  $("new-index-name").value = editing ? editing.name : "";
  $("new-index-name").disabled = !!editing;
  $("create-index").textContent = editing ? "Speichern" : "Anlegen";
  miError("");
  $("new-index-form").classList.remove("hidden");
  const box = $("mi-choices");
  box.textContent = "Validierte Metriken werden geladen …";
  try {
    const { metrics, liquidity_median: median } = await miFetch("/builder/metrics");
    showLiquidityMedian("mi-liqmedian", "mi-minliq", median, miApplyMinLiquidity);
    logEvent("info", "builder.metrics.loaded", { validated: metrics.map((m) => m.key), editing: editing ? editing.name : null });
    box.replaceChildren();
    if (!metrics.length) box.textContent = "Keine validierte Metrik vorhanden – bitte zuerst im Tab „Metriken“ freigeben.";
    for (const m of metrics) {
      const label = document.createElement("label");
      const pick = Object.assign(document.createElement("input"), { type: "checkbox", checked: m.key in weights });
      pick.dataset.key = m.key;
      const weight = Object.assign(document.createElement("input"), { type: "number", min: "0.01", step: "0.1", value: weights[m.key] ?? 1, title: "Gewicht" });
      label.dataset.liquidity = m.liquidity_usd ?? "";
      label.append(pick, document.createTextNode(m.name + " (" + (MI_UNITS[m.unit] || m.unit) + ", " + m.days + " Tage, Liquidität Ø 30 T: " +
        metricUsd(m.liquidity_usd) + ") · Gewicht "), weight);
      box.appendChild(label);
    }
    miFilterChoices();
    const dropped = Object.keys(weights).filter((k) => !metrics.some((m) => m.key === k));
    if (dropped.length) miError("Nicht mehr validiert und daher nicht wählbar: " + dropped.join(", ") + " – bitte ersetzen oder entfernen.");
  } catch (err) {
    box.textContent = "";
    miError("Metriken konnten nicht geladen werden: " + err.message);
    logEvent("error", "builder.metrics.failed", { message: err.message });
  }
}

function miError(text) {
  $("new-index-error").textContent = text;
  $("new-index-error").classList.toggle("hidden", !text);
}

// Liquidity filter of the selection: hides unchecked metrics below the minimum (checked ones stay visible and saved).
function miFilterChoices() {
  const min = metricMinLiquidity("mi-minliq"), hidden = [];
  for (const label of document.querySelectorAll("#mi-choices label")) {
    const pick = label.querySelector("input[type=checkbox]");
    const liq = label.dataset.liquidity === "" ? null : Number(label.dataset.liquidity);
    label.hidden = !pick.checked && !metricLiquidityPasses(liq, min);
    if (label.hidden) hidden.push(pick.dataset.key);
  }
  return hidden;
}

function miFilterChanged() {
  logEvent("info", "builder.liquidity.filter", { min: metricMinLiquidity("mi-minliq"), hidden: miFilterChoices() });
}

function miApplyMinLiquidity(usd) {
  logEvent("info", "builder.liquidity.median.click", { median: usd });
  miFilterChanged();
}

$("mi-minliq").addEventListener("change", miFilterChanged);

async function miSave() {
  const name = $("new-index-name").value.trim();
  const components = [...document.querySelectorAll("#mi-choices label")]
    .filter((l) => l.querySelector("input[type=checkbox]").checked)
    .map((l) => ({ key: l.querySelector("input[type=checkbox]").dataset.key, weight: Number(l.querySelector("input[type=number]").value) }));
  const editing = mi.editing;
  logEvent("info", "button.click", { id: "create-index", name, components, mode: editing ? "update" : "create" });
  try {
    if (editing) await miFetch("/builder/indices/" + encodeURIComponent(editing.name), { method: "PUT", headers: MI_JSON, body: JSON.stringify({ components }) });
    else await miFetch("/builder/indices", { method: "POST", headers: MI_JSON, body: JSON.stringify({ name, components }) });
  } catch (err) {
    miError(err.message);
    logEvent("warning", "builder.index.save.refused", { name, message: err.message });
    return;
  }
  logEvent("info", "builder.index.saved", { name, components });
  $("new-index-form").classList.add("hidden");
  await loadIndices();
  selectIndex(name);
}

// ---- view -------------------------------------------------------------------------------

async function loadMetricIndex(name, btn) {
  const ticket = ++mi.request;
  state.name = null; // archive-only features (history, backfill) stay idle; stocks via regression.js (1015-2231)
  parkCorrelation();
  document.querySelectorAll("#index-buttons button").forEach((b) => b.classList.toggle("active", b === btn));
  $("result").classList.add("hidden");
  setStatus("Rufe „" + name + "“ ab …", false);
  try {
    const data = await miFetch("/builder/indices/" + encodeURIComponent(name) + "?days=" + mi.days);
    if (ticket !== mi.request) return;
    miRender(data);
    logEvent("info", "builder.index.loaded", { name, days: mi.days, points: data.points.length, first: data.points[0]?.date || null });
    showMetricCorrelation(name);
    setStatus("", false);
    if (data.warning) logEvent("warning", "builder.index.warning", { name, warnings: data.warnings });
  } catch (err) {
    setStatus("Abruf fehlgeschlagen: " + err.message, true);
    logEvent("error", "builder.index.failed", { name, message: err.message });
  }
}

function miRender(data) {
  mi.current = data;
  $("mi-title").textContent = data.name;
  $("mi-warning").textContent = data.warnings.map((w) => "⚠ " + w).join(" ");
  $("mi-warning").classList.toggle("hidden", !data.warning);
  const s = data.summary;
  $("mi-last").textContent = miZ(s.last);
  $("mi-change").textContent = miZ(s.change);
  $("mi-span").textContent = s.min == null ? "–" : miZ(s.min) + " – " + miZ(s.max);
  $("mi-days").textContent = data.points.length;
  $("mi-chart").replaceChildren(metricChart(data.points));
  $("mi-caption").textContent = data.points.length
    ? "Tage, an denen jede Metrik einen z-Wert hat (je Metrik ab dem " + data.min_history + ". Wert)."
    : "Noch kein Indexwert: jede Metrik braucht mindestens " + data.min_history + " Tageswerte an gemeinsamen Tagen.";
  const last = data.points.length ? data.points[data.points.length - 1].components : {};
  const tbody = $("mi-components");
  tbody.replaceChildren();
  for (const c of data.components) {
    const row = document.createElement("tr");
    const status = Object.assign(document.createElement("span"), {
      className: "badge " + (c.status === "validated" ? "ok" : c.status === "blocked" ? "bad" : ""), textContent: c.status,
      title: c.status_reason || "" });
    const cells = [c.name, MI_UNITS[c.unit] || c.unit || "–", String(c.weight), status, miRaw(c.last_value, c.unit), miZ(last[c.key])];
    cells.forEach((v, i) => {
      const td = document.createElement("td");
      if (v instanceof Node) td.appendChild(v); else td.textContent = v;
      if ([2, 4, 5].includes(i)) td.className = "num";
      row.appendChild(td);
    });
    tbody.appendChild(row);
  }
  miMarkRange();
  $("mi-result").classList.remove("hidden");
}

function miMarkRange() {
  document.querySelectorAll("#mi-range button[data-days]").forEach((b) => b.classList.toggle("active", Number(b.dataset.days) === mi.days));
}

// ---- wiring --------------------------------------------------------------------------

$("new-index").addEventListener("click", () => {
  logEvent("info", "button.click", { id: "new-index" });
  miOpenForm(null);
});
$("mi-edit").addEventListener("click", () => {
  logEvent("info", "button.click", { id: "mi-edit", name: mi.current?.name });
  if (mi.current) miOpenForm(mi.current);
});
document.querySelectorAll("#mi-range button[data-days]").forEach((btn) => btn.addEventListener("click", () => {
  mi.days = Number(btn.dataset.days);
  try { localStorage.setItem(MI_DAYS_KEY, String(mi.days)); } catch {}
  logEvent("info", "button.click", { id: "mi-range", days: mi.days, name: mi.current?.name });
  miMarkRange();
  if (mi.current) loadMetricIndex(mi.current.name, document.querySelector("#index-buttons button.active"));
}));
$("mi-delete").addEventListener("click", () => { if (mi.current) deleteIndex(mi.current.name, false); });
$("cancel-new-index").addEventListener("click", () => {
  logEvent("info", "button.click", { id: "cancel-new-index" });
  $("new-index-form").classList.add("hidden");
});
$("create-index").addEventListener("click", miSave);
$("new-index-name").addEventListener("keydown", (e) => { if (e.key === "Enter") miSave(); });
