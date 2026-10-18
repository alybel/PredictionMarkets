// Weighting of a metric index (risk index 5/7): method select (manual, equal, liquidity, inverse volatility) with live
// weights in percent (POST /builder/weighting/preview) and the AI proposal (POST /builder/weighting/propose). A proposal
// only fills the form after "Übernehmen"; saving then sends proposal = confirmed (unchanged) or changed (edited).
// Uses $, logEvent (shell.js), miFetch, miError, MI_JSON (metricindex.js). Every click and choice goes to app.log.

const miw = { names: {}, decision: null, pending: null, request: 0 };

function miwChoices() {
  return [...document.querySelectorAll("#mi-choices label")].filter((l) => l.querySelector("input[type=checkbox]").checked)
    .map((l) => ({ key: l.querySelector("input[type=checkbox]").dataset.key, input: l.querySelector("input[type=number]") }));
}

function miwFormat(weights) {
  return Object.entries(weights).map(([k, v]) => (miw.names[k] || k) + " " + v.toFixed(1).replace(".", ",") + " %").join(" · ");
}

function miwInit(editing, metrics, methods) {
  miw.names = Object.fromEntries(metrics.map((m) => [m.key, m.name]));
  miw.decision = miw.pending = null;
  const select = $("mi-weighting");
  select.replaceChildren(...Object.entries(methods || { manual: "Manuell" }).map(([value, text]) => Object.assign(document.createElement("option"), { value, text })));
  select.value = editing?.weighting || "manual";
  $("mi-proposal").classList.add("hidden");
  miwRefresh();
}

async function miwRefresh() {
  const method = $("mi-weighting").value, picked = miwChoices(), ticket = ++miw.request;
  document.querySelectorAll("#mi-choices input[type=number]").forEach((i) => { i.disabled = method !== "manual"; });
  if (!picked.length) { $("mi-weights").textContent = ""; return; }
  try {
    const body = await miFetch("/builder/weighting/preview", { method: "POST", headers: MI_JSON, body: JSON.stringify({
      keys: picked.map((p) => p.key), method, weights: Object.fromEntries(picked.map((p) => [p.key, Number(p.input.value)])) }) });
    if (ticket === miw.request) $("mi-weights").textContent = "Gewichte heute (" + body.method_label + "): " + miwFormat(body.weights_pct);
  } catch (err) {
    if (ticket === miw.request) $("mi-weights").textContent = "Gewichte nicht berechenbar: " + err.message;
    logEvent("warning", "builder.weighting.preview.failed", { method, message: err.message });
  }
}

function miwChanged(what) {
  if (miw.decision === "confirmed") {
    miw.decision = "changed";
    logEvent("info", "builder.weighting.proposal.changed", { what, method: $("mi-weighting").value });
  }
  miwRefresh();
}

async function miwPropose() {
  const keys = miwChoices().map((p) => p.key);
  logEvent("info", "button.click", { id: "mi-propose", keys });
  if (!keys.length) { miError("Bitte zuerst Metriken auswählen."); return; }
  miError("");
  $("mi-propose").disabled = true;
  $("mi-propose").textContent = "KI denkt nach …";
  try {
    const p = await miFetch("/builder/weighting/propose", { method: "POST", headers: MI_JSON, body: JSON.stringify({ keys }) });
    miw.pending = p;
    $("mi-proposal-method").textContent = p.method_label;
    $("mi-proposal-model").textContent = "(" + p.model + ")";
    $("mi-proposal-weights").textContent = miwFormat(p.weights_pct);
    $("mi-proposal-rationale").textContent = p.rationale || "Keine Begründung geliefert.";
    $("mi-proposal").classList.remove("hidden");
    logEvent("info", "builder.weighting.proposal", { method: p.method, weights: p.weights_pct, model: p.model });
  } catch (err) {
    miError("KI-Vorschlag nicht möglich: " + err.message);
    logEvent("warning", "builder.weighting.proposal.failed", { keys, message: err.message });
  } finally {
    $("mi-propose").disabled = false;
    $("mi-propose").textContent = "KI-Vorschlag";
  }
}

function miwAccept() {
  const p = miw.pending;
  if (!p) return;
  $("mi-weighting").value = p.method;
  if (p.method === "manual") for (const c of miwChoices()) if (c.key in p.weights_pct) c.input.value = p.weights_pct[c.key];
  miw.decision = "confirmed";
  $("mi-proposal").classList.add("hidden");
  logEvent("info", "builder.weighting.proposal.confirmed", { method: p.method, weights: p.weights_pct, model: p.model });
  miwRefresh();
}

function miwReject() {
  logEvent("info", "builder.weighting.proposal.rejected", { method: miw.pending?.method, model: miw.pending?.model });
  miw.pending = null;
  $("mi-proposal").classList.add("hidden");
}

function miwPayload() {
  return { weighting: $("mi-weighting").value, ...(miw.decision ? { proposal: miw.decision } : {}) };
}

$("mi-weighting").addEventListener("change", () => {
  logEvent("info", "builder.weighting.method", { method: $("mi-weighting").value });
  miwChanged("method");
});
$("mi-choices").addEventListener("change", (e) => miwChanged(e.target.type === "checkbox" ? "selection" : "weight"));
$("mi-propose").addEventListener("click", miwPropose);
$("mi-proposal-accept").addEventListener("click", miwAccept);
$("mi-proposal-reject").addEventListener("click", miwReject);
