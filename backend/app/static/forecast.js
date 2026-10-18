// Forecast power report of a metric index (risk index 6/7): S&P 500, 10-year yield, TLT and VIX over 1/5/20 trading
// days plus crash hit rate, measured walk-forward; one traffic light per target with explanation.
// GET /builder/indices/{name}/forecast (stored; built on save and daily), POST … to recompute (?load_prices=true).
// Uses $, logEvent (shell.js) and miFetch (metricindex.js). Every load and click goes to app.log via logEvent.

const fc = { name: null, request: 0 };
const FC_LIGHTS = { green: "ok", yellow: "", red: "bad", grey: "muted" };
const fcNum = (v, digits = 2) => (v == null ? "–" : v.toFixed(digits).replace(".", ","));
const fcPct = (v, digits = 1) => (v == null ? "–" : fcNum(100 * v, digits) + " %");
const fcP = (v) => (v == null ? "–" : v < 1e-4 ? "< 0,0001" : fcNum(v, 4));

function fcEl(tag, props = {}, children = []) {
  const el = Object.assign(document.createElement(tag), props);
  el.append(...children);
  return el;
}

function fcCard() {
  let card = $("fc-card");
  if (card) return card;
  card = fcEl("section", { id: "fc-card", className: "card hidden" }, [
    fcEl("div", { className: "card-head" }, [
      fcEl("div", {}, [fcEl("h2", { textContent: "Prognosekraft (Aktien, Anleihen, Volatilität)" }),
        fcEl("p", { className: "hint", textContent: "Sagt eine Index-Veränderung S&P 500, 10-jährige US-Rendite, TLT und VIX über 1, 5 und 20 Handelstage voraus? " +
          "Gemessen wird nur außerhalb der Lernphase (Walk-Forward): Richtung, Steigung und Schwellen stammen je Testblock nur aus früheren Daten. " +
          "Crash-Trefferquote: Anteil der Signaltage (Index im oberen Fünftel der Lernphase) mit einem Rückgang über 10 % innerhalb von 30 Tagen." })]),
      fcEl("div", { className: "controls" }, [
        fcEl("button", { id: "fc-recompute", type: "button", className: "ghost", textContent: "Neu berechnen" }),
        fcEl("button", { id: "fc-load", type: "button", className: "ghost", textContent: "Kurse laden & neu berechnen" })])]),
    fcEl("p", { id: "fc-status", className: "hint", role: "status" }),
    fcEl("div", { id: "fc-warnings" }),
    fcEl("div", { id: "fc-targets", className: "fc-targets" })]);
  $("mi-fc-slot").appendChild(card);
  $("fc-recompute").addEventListener("click", () => fcRecompute(false));
  $("fc-load").addEventListener("click", () => fcRecompute(true));
  return card;
}

async function showForecast(name) {
  const ticket = ++fc.request;
  fc.name = name;
  fcCard().classList.remove("hidden");
  $("mi-fc-slot").classList.remove("hidden");
  $("fc-status").textContent = "Prognosekraft-Bericht wird geladen …";
  $("fc-targets").replaceChildren();
  try {
    const data = await miFetch("/builder/indices/" + encodeURIComponent(name) + "/forecast");
    if (ticket !== fc.request) return;
    fcRender(data);
    logEvent("info", "forecast.loaded", { name, as_of: data.as_of, trigger: data.trigger,
      lights: Object.fromEntries(data.targets.map((t) => [t.ticker, t.light])), short_history: data.short_history });
  } catch (err) {
    $("fc-status").textContent = "Bericht nicht verfügbar: " + err.message;
    logEvent("error", "forecast.failed", { name, message: err.message });
  }
}

async function fcRecompute(loadPrices) {
  const name = fc.name;
  logEvent("info", "button.click", { id: loadPrices ? "fc-load" : "fc-recompute", name });
  if (!name) return;
  const ticket = ++fc.request;
  $("fc-status").textContent = loadPrices ? "Kurse werden geladen und der Bericht neu berechnet …" : "Bericht wird neu berechnet …";
  try {
    const data = await miFetch("/builder/indices/" + encodeURIComponent(name) + "/forecast" + (loadPrices ? "?load_prices=true" : ""), { method: "POST" });
    if (ticket !== fc.request) return;
    fcRender(data);
    logEvent("info", "forecast.recomputed", { name, load_prices: loadPrices, as_of: data.as_of });
  } catch (err) {
    $("fc-status").textContent = "Neuberechnung fehlgeschlagen: " + err.message;
    logEvent("error", "forecast.recompute.failed", { name, message: err.message });
  }
}

function fcRender(data) {
  const trigger = { save: "beim Speichern", daily: "im täglichen Lauf", manual: "manuell", on_demand: "beim ersten Aufruf" }[data.trigger] || data.trigger;
  $("fc-status").textContent = "Stichtag " + data.as_of + " · berechnet " + (data.computed_at || "").slice(0, 16).replace("T", " ") + " UTC " + trigger +
    " · " + data.index_days + " Indextage (" + (data.first_date || "–") + " bis " + (data.last_date || "–") + ")";
  $("fc-warnings").replaceChildren(...data.warnings.map((w, i) =>
    fcEl("p", { className: i === 0 && data.short_history ? "mi-warning" : "hint", role: i === 0 && data.short_history ? "alert" : "", textContent: "⚠ " + w })));
  $("fc-targets").replaceChildren(...data.targets.map(fcTarget));
}

function fcTarget(t) {
  const head = fcEl("div", { className: "fc-head" }, [
    fcEl("strong", { textContent: t.name }),
    fcEl("span", { className: "badge " + FC_LIGHTS[t.light], textContent: t.light_label, title: "Ampel der Gesamtkennzahl" }),
    fcEl("span", { className: "fc-score", textContent: "Prognosekraft " + t.score + " %", title: t.passed + " von " + t.checks + " Prüfungen bestanden" })]);
  const unit = t.kind === "return" ? "Rendite" : t.unit;
  const rows = t.horizons.map((h) => fcEl("tr", {}, [
    h.horizon + (h.horizon === 1 ? " Tag" : " Tage"), fcNum(h.forecast_correlation), fcNum(h.correlation), fcP(h.p_value), fcPct(h.oos_r2),
    h.slope == null ? "–" : (t.kind === "return" ? fcPct(h.slope, 2) : fcNum(h.slope)) + " / z", String(h.effective_n), h.passed ? "✓" : h.enough ? "–" : "zu wenig Daten",
  ].map((v, i) => fcEl("td", { className: i > 0 && i < 7 ? "num" : "", textContent: v }))));
  const heads = [["Zeitraum", ""], ["Prognose-Korr.", "Korrelation von Prognose und tatsächlicher Veränderung außerhalb der Lernphase"],
    ["Korrelation", "Index-Veränderung ↔ " + unit + " im Testzeitraum"], ["p (einseitig)", "mit effektiver Fallzahl (überlappende Fenster)"],
    ["OOS-R²", "Erklärte Streuung außerhalb der Lernphase gegenüber dem Lernmittelwert"], ["Steigung", unit + " je z-Punkt (letzte Lernphase)"],
    ["Test-Beob.", "unabhängige Testbeobachtungen"], ["Bestanden", "p unter Bonferroni-Schwelle und OOS-R² > 0"]]
    .map(([text, title], i) => fcEl("th", { className: i > 0 && i < 7 ? "num" : "", textContent: text, title }));
  const table = fcEl("div", { className: "table-scroll" }, [
    fcEl("table", {}, [fcEl("thead", {}, [fcEl("tr", {}, heads)]), fcEl("tbody", {}, rows)])]);
  const parts = [head, fcEl("p", { className: "hint", textContent: t.explanation }), table];
  if (t.crash) {
    const c = t.crash;
    parts.push(fcEl("p", { className: "hint", textContent: "Crash-Trefferquote (Rückgang über 10 % in 30 Tagen): " + fcPct(c.hit_rate, 0) + " von " + c.signals +
      " Signaltagen, Grundrate " + fcPct(c.base_rate, 0) + ", Lift " + fcNum(c.lift) + ", erkannt " + fcPct(c.recall, 0) + " der " + c.crash_days +
      " Crash-Tage, p " + fcP(c.p_value) + (c.passed ? " ✓" : c.enough ? "" : " · zu wenig Signale oder keine Crashs im Testzeitraum") }));
  }
  return fcEl("div", { className: "fc-target" }, parts);
}
