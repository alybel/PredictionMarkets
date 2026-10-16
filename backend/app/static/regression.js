// Stock evaluation of metric indices (1015-2231) in the correlation card: mode switch archive/metric, target switch
// (excess return default / raw return), market row, regression columns (simple R², multiple R², F-test, coefficient
// per component), multicollinearity warning and the side-by-side regressions of the stock detail.
// Archive indices keep the card exactly as before. Relies on shell.js, correlation.js and trades.js
// ($, logEvent, corr, corrRow, corrCell, loadCorrelation, saveCorrSettings, signedPct, fmtCorr, fmtP, trd).

const CR_HOME = { parent: $("cr-card").parentNode, next: $("cr-card").nextSibling };
const CR_MIN_CHANGES = {
  archive: [...$("cr-min-change").options].map((o) => [o.value, o.textContent]),
  metric: [["0", "jeder Veränderung"], ["0.25", "±0,25 z"], ["0.5", "±0,5 z"], ["1", "±1 z"]],
};
const CR_SLOPE = {
  archive: ["Beta je Pkt.", "Rendite am Folgetag je 1 Indexpunkt Veränderung"],
  metric: ["Steigung je z", "Einfache Regression: Rendite am Folgetag je 1 z-Punkt Veränderung des Gesamtindex"],
};
const fmtR2 = (v) => (v == null ? "–" : (v * 100).toFixed(1).replace(".", ",") + " %");
const fmtDec = (v, digits = 2) => (v == null ? "–" : v.toFixed(digits).replace(".", ","));
const crComponentName = (d, key) => (d.components.find((c) => c.key === key) || { name: key }).name;

function crBadge(text, title, cls = "ok") {
  return Object.assign(document.createElement("span"), { className: "badge " + cls, textContent: text, title: title || "" });
}

// Switch the card between archive and metric index; a change of kind closes the open stock detail
function setCorrMode(kind) {
  if (corr.kind !== kind) {
    corr.ticker = null;
    corr.data = null;
    trd.request++;
    $("cr-detail").classList.add("hidden");
    logEvent("info", "correlation.mode", { kind });
  }
  corr.kind = kind;
  const card = $("cr-card");
  card.classList.toggle("metric", kind === "metric");
  if (kind === "metric") $("mi-cr-slot").appendChild(card);
  else CR_HOME.parent.insertBefore(card, CR_HOME.next);
  const select = $("cr-min-change");
  select.replaceChildren(...CR_MIN_CHANGES[kind].map(([value, text]) => Object.assign(document.createElement("option"), { value, textContent: text })));
  if (![...select.options].some((o) => Number(o.value) === corrMinChange())) {
    if (kind === "metric") corr.minChangeZ = 0; else corr.minChange = 0;
  }
  select.value = [...select.options].find((o) => Number(o.value) === corrMinChange()).value;
  $("cr-slope-label").textContent = CR_SLOPE[kind][0];
  $("cr-slope-label").parentNode.title = CR_SLOPE[kind][1];
  document.querySelectorAll("#cr-card th.mr-coef").forEach((th) => th.remove());
  if (kind !== "metric") $("cr-mc-warning").classList.add("hidden");
}

// Metric index view: card below the metric index, loaded with the metric endpoints
function showMetricCorrelation(name) {
  setCorrMode("metric");
  loadCorrelation(name, null);
}

// While a metric index loads, the card waits hidden in the (hidden) archive view
function parkCorrelation() {
  CR_HOME.parent.insertBefore($("cr-card"), CR_HOME.next);
}

function renderMetricExtras(d, body) {
  const mc = d.collinearity;
  const parts = mc.strong_pairs.map((p) => crComponentName(d, p.a) + " ↔ " + crComponentName(d, p.b) + " (r = " + fmtCorr(p.correlation) + ")")
    .concat(mc.high_vif.map((k) => crComponentName(d, k) + " (VIF " + (mc.vif[k] == null ? "∞" : fmtDec(mc.vif[k], 1)) + ")"));
  $("cr-mc-warning").textContent = mc.warning
    ? "⚠ Stark korrelierte Bestandteile: " + parts.join(", ") + ". Die Einzel-Koeffizienten der mehrfachen Regression sind dann " +
      "unzuverlässig (Multikollinearität, Schwelle |r| ≥ " + fmtDec(mc.corr_limit, 1) + " bzw. VIF ≥ " + fmtDec(mc.vif_limit, 0) +
      "); F-Test und R² bleiben aussagekräftig."
    : "";
  $("cr-mc-warning").classList.toggle("hidden", !mc.warning);
  $("cr-reg-significant").textContent = d.regression_significant + " / " + d.regression_tested;
  $("cr-reg-expected").textContent = "≈ " + String(d.regression_expected_false_positives).replace(".", ",");
  const anchor = $("cr-f-head");
  document.querySelectorAll("#cr-card th.mr-coef").forEach((th) => th.remove());
  [...d.components].reverse().forEach((c) => anchor.after(Object.assign(document.createElement("th"), {
    className: "num mr-coef", textContent: "b " + c.name,
    title: "Mehrfache Regression: Rendite am Folgetag je 1 " + c.label + " Tagesänderung von „" + c.name + "“ (p-Wert in Klammern)" })));
  const market = corrRow(d.market, d, false);
  market.classList.add("market-row");
  market.title = "Gleichgewichteter Schnitt der Top-100-Renditen, immer rohe Rendite; zählt nicht zu den Mehrfachtests";
  body.appendChild(market);
}

function regressionCells(tr, r, d) {
  const simple = document.createElement("td");
  simple.className = "num sig-col";
  simple.textContent = fmtR2(r.r2);
  if (r.bonferroni) simple.append(" ", crBadge("Bonferroni", "p unter 0,05 ÷ " + d.stocks_tested + " getestete Aktien"));
  tr.appendChild(simple);
  corrCell(tr, fmtR2(r.multi_r2), "num");
  const f = document.createElement("td");
  f.className = "num";
  f.textContent = fmtP(r.multi_f_p);
  if (r.multi_bonferroni) f.append(" ", crBadge("Bonferroni", "F-Test p unter 0,05 ÷ " + d.regression_tested + " getestete Aktien"));
  tr.appendChild(f);
  for (const c of d.components) {
    const coef = r.coefficients.find((x) => x.key === c.key);
    const td = document.createElement("td");
    td.className = "num" + (coef && coef.significant ? (coef.coef >= 0 ? " up" : " down") : "");
    td.textContent = coef ? signedPct(coef.coef, 3) + " (" + fmtP(coef.p_value) + ")" : "–";
    if (coef && coef.bonferroni) td.append(" ", crBadge("B", "p unter 0,05 ÷ " + d.coefficient_tests + " Koeffizienten-Tests"));
    tr.appendChild(td);
  }
}

function metricCaption(d, shown) {
  if (!d.stocks_with_prices) return "Noch keine Aktienkurse gespeichert – „↻ Kurse laden“ holt die Schlusskurse der Top-100-US-Aktien.";
  if (d.index_days < 2) return "Zu wenige Tage mit Indexwert für eine Analyse (jede Metrik braucht erst einige Tageswerte für ihren z-Wert).";
  if (!d.stocks_tested) return "Noch zu wenige Paare (mindestens " + d.min_observations + " Handelstage mit Index-Veränderung nötig).";
  const bonf = (v) => String(+v.toPrecision(2)).replace(".", ",");
  return "Oberste Zeile: Gesamtmarkt (gleichgewichteter Ø der Top 100, rohe Rendite). Darunter " + shown + " Aktien mit " +
    (d.target === "excess" ? "Überrendite gegenüber diesem Markt" : "roher Rendite") + ". " +
    "Einfach: Rendite je 1 z-Punkt Änderung des Gesamtindex. Mehrfach: Tagesänderung jeder Metrik in ihrer Einheit als eigener Regressor (p-Wert je Bestandteil, F-Test für alle zusammen). " +
    "Zufall: Bei " + d.stocks_tested + " Aktien sind ≈ " + d.expected_false_positives + " Korrelationen und ≈ " +
    d.regression_expected_false_positives + " F-Tests mit p < 5 % reiner Zufall; belastbar erst unter p < " + bonf(d.bonferroni_alpha) +
    (d.regression_bonferroni_alpha ? " bzw. F-Test p < " + bonf(d.regression_bonferroni_alpha) : "") +
    (d.coefficient_bonferroni_alpha ? " und je Koeffizient p < " + bonf(d.coefficient_bonferroni_alpha) : "") + " (Bonferroni). " +
    "Index-Tag T vor US-Schluss gegen Rendite Schlusskurs T → nächster Schlusskurs, kein Lookahead. Keine Anlageempfehlung – Korrelation ist keine Kausalität." +
    priceCoverageNote(d);
}

// Stock closes start later than the index window → the analysis is shorter than the index history (1015-2258)
function priceCoverageNote(d) {
  if (!d.first_index_date || !d.first_price_date) return "";
  const gapDays = (Date.parse(d.first_price_date) - Date.parse(d.first_index_date)) / 86400000;
  if (gapDays <= 7) return "";
  return " · Hinweis: Aktienkurse erst ab " + new Date(d.first_price_date).toLocaleDateString("de-DE") + " gespeichert, der Index reicht bis " +
    new Date(d.first_index_date).toLocaleDateString("de-DE") + " zurück – „↻ Kurse laden“ holt die älteren Schlusskurse.";
}

// ---- stock detail: simple and multiple regression side by side -------------------------------------------------

function regTable(head, rows) {
  const table = document.createElement("table");
  const tr = document.createElement("tr");
  head.forEach((h, i) => tr.appendChild(Object.assign(document.createElement("th"), { textContent: h, className: i ? "num" : "" })));
  table.appendChild(document.createElement("thead")).appendChild(tr);
  const body = table.appendChild(document.createElement("tbody"));
  for (const cells of rows) {
    const row = body.appendChild(document.createElement("tr"));
    cells.forEach((c, i) => row.appendChild(Object.assign(document.createElement("td"), { textContent: c, className: i ? "num" : "" })));
  }
  return table;
}

function renderRegressionDetail(data) {
  const simpleBox = $("cr-reg-simple"), multiBox = $("cr-reg-multiple");
  if (!data) {
    simpleBox.replaceChildren();
    multiBox.replaceChildren();
    $("cr-reg-caption").textContent = "Regressionen werden berechnet …";
    return;
  }
  const reg = data.regression, s = reg.simple, m = reg.multiple, mc = reg.collinearity;
  simpleBox.replaceChildren(regTable(["Einfach: ΔIndex (z)", "Wert"], [
    ["Steigung je 1 z", signedPct(s.coef, 3)], ["Std.-Fehler", signedPct(s.std_error, 3).replace("+", "")],
    ["t-Wert", fmtDec(s.t)], ["p-Wert", fmtP(s.p_value)], ["R²", fmtR2(s.r2)], ["n (Tage)", String(s.n)],
  ]));
  const coefRows = m.coefficients.map((c) => {
    const comp = reg.components.find((x) => x.key === c.key) || { name: c.key, label: "" };
    return [comp.name + " (je " + comp.label + ")", signedPct(c.coef, 3), signedPct(c.std_error, 3).replace("+", ""), fmtP(c.p_value)];
  });
  multiBox.replaceChildren(regTable(["Mehrfach: Bestandteile (Roheinheit)", "Koeffizient", "Std.-Fehler", "p-Wert"], coefRows.concat([
    ["R² (bereinigt)", fmtR2(m.multi_r2) + " (" + fmtR2(m.multi_adj_r2) + ")", "", ""],
    ["F-Test (alle Koeffizienten = 0)", "F = " + fmtDec(m.multi_f), "", fmtP(m.multi_f_p)],
    ["n (Tage)", String(m.multi_n), "", ""],
  ])));
  const vif = Object.entries(mc.vif).map(([k, v]) => ((reg.components.find((c) => c.key === k) || {}).name || k) + " " + (v == null ? "∞" : fmtDec(v, 1)));
  $("cr-reg-caption").textContent = (s.coef == null
    ? "Zu wenige Paare (" + s.n + " von mindestens " + reg.min_observations + ") – keine Regression."
    : "Zielgröße: " + (data.target === "excess" ? "Überrendite gegenüber dem Ø der Top 100" : "rohe Rendite") +
      " am Folgetag. Koeffizient = Renditeänderung je Einheit; p-Wert < 5 % heißt: einzeln auffällig, bei vielen Tests aber oft Zufall.") +
    (vif.length > 1 ? " VIF der Bestandteile: " + vif.join(", ") + "." : "") +
    (mc.warning ? " ⚠ Bestandteile stark korreliert – Einzel-Koeffizienten unzuverlässig." : "");
  $("cr-reg-caption").classList.toggle("mi-warning", mc.warning);
}

// ---- wiring --------------------------------------------------------------------------------------------------

$("cr-target").value = corr.target;
$("cr-target").addEventListener("change", (e) => {
  corr.target = e.target.value;
  saveCorrSettings();
  logEvent("info", "correlation.target", { target: corr.target, name: corr.name });
  if (corr.name && corr.kind === "metric") loadCorrelation(corr.name, null);
});
