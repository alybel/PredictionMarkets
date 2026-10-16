// Archive view of rule-based indices (read only): rule summary in the right-hand panel and the historical
// performance chart (GET /indices/{name}/history). Relies on the globals of shell.js and indices.js
// ($, fmtNum, logEvent, state).

const IB_DAYS_KEY = "pmi.builder.days";
const builder = { days: 90, request: 0 };
try { builder.days = Number(localStorage.getItem(IB_DAYS_KEY)) || builder.days; } catch {}

const pct = (v) => (v == null ? "–" : (v * 100).toFixed(1) + " %");
const listOrAll = (items) => (items.length ? items.join(", ") : "keine Einschränkung");

// ---- rule summary ----------------------------------------------------------------

function renderRuleSummary(rule) {
  const ul = $("rule-summary");
  ul.replaceChildren();
  if (!rule) return;
  const rows = [
    ["Liquiditätsfilter", "24h-Umsatz ≥ " + fmtNum(Math.max(rule.min_liquidity, 10000)) + " USD"],
    ["Preis-Korridor", rule.min_price + " – " + rule.max_price],
    ["Tags", listOrAll(rule.categories)],
    ["Keywords", listOrAll(rule.keywords)],
    ["Laufzeit", "nur Märkte mit Enddatum in der Zukunft"],
    ["Gewichtung", "liquiditätsgewichteter Durchschnitt der Preise"],
  ];
  for (const [label, text] of rows) {
    const li = document.createElement("li");
    const strong = document.createElement("strong");
    strong.textContent = label;
    li.append(strong, document.createTextNode(text));
    ul.appendChild(li);
  }
}

// ---- history ---------------------------------------------------------------------

async function loadHistory(name) {
  const ticket = ++builder.request;
  $("ib-caption").textContent = "Wertentwicklung wird berechnet …";
  try {
    const res = await fetch("/indices/" + encodeURIComponent(name) + "/history?days=" + builder.days);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (ticket !== builder.request) return; // a newer request is under way
    renderHistory(data);
    logEvent("info", "builder.history.loaded", { name, days: builder.days, points: data.points.length });
  } catch (err) {
    if (ticket !== builder.request) return;
    renderHistory(null);
    $("ib-caption").textContent = "Wertentwicklung konnte nicht geladen werden: " + err.message;
    logEvent("error", "builder.history.failed", { name, message: err.message });
  }
}

function renderHistory(data) {
  const s = data ? data.summary : {};
  $("ib-first").textContent = pct(s.first);
  $("ib-last").textContent = pct(s.last);
  $("ib-change").textContent = s.change == null ? "–" : (s.change >= 0 ? "+" : "") + (s.change * 100).toFixed(1) + " Pkt.";
  $("ib-change").className = s.change == null ? "" : s.change >= 0 ? "up" : "down";
  $("ib-span").textContent = s.min == null ? "–" : pct(s.min) + " – " + pct(s.max);
  renderHistoryChart(data ? data.points : []);
}

function renderHistoryChart(allPoints) {
  const svg = $("ib-chart");
  svg.replaceChildren();
  const points = allPoints.filter((p) => p.value != null);
  if (!points.length) {
    $("ib-caption").textContent = allPoints.length
      ? "An keinem der " + allPoints.length + " Snapshot-Tage hat ein Markt die Regeln erfüllt."
      : "Noch keine Tages-Snapshots vorhanden – der tägliche Lauf legt sie an; „Historie nachladen“ ergänzt vergangene Tage.";
    return;
  }
  const W = 600, H = 160, pad = 8;
  const values = points.map((p) => p.value);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (hi - lo < 0.02) { lo -= 0.01; hi += 0.01; } // flat line: keep it in the middle
  const x = (i) => (points.length === 1 ? W / 2 : pad + (i * (W - 2 * pad)) / (points.length - 1));
  const y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => {
    const node = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    svg.appendChild(node);
    return node;
  };
  for (const f of [0.25, 0.5, 0.75]) {
    const gy = (pad + f * (H - 2 * pad)).toFixed(1);
    el("line", { class: "grid", "vector-effect": "non-scaling-stroke", x1: pad, x2: W - pad, y1: gy, y2: gy });
  }
  const lineD = points.map((p, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p.value).toFixed(1)).join(" ");
  el("path", { class: "area", d: lineD + " L" + x(points.length - 1).toFixed(1) + " " + (H - pad) + " L" + x(0).toFixed(1) + " " + (H - pad) + " Z" });
  el("path", { class: "line", "vector-effect": "non-scaling-stroke", d: lineD });
  // invisible hover bands: date, value and number of constituents per day
  const band = points.length > 1 ? (W - 2 * pad) / (points.length - 1) : W;
  points.forEach((p, i) => {
    const rect = el("rect", { class: "hover", x: (x(i) - band / 2).toFixed(1), y: 0, width: band.toFixed(1), height: H });
    const title = document.createElementNS(ns, "title");
    title.textContent = new Date(p.date).toLocaleDateString("de-DE") + ": " + pct(p.value) + " · " + p.market_count + " Bestandteile";
    rect.appendChild(title);
  });
  const first = points[0], last = points[points.length - 1];
  $("ib-caption").textContent = "Verlauf " + new Date(first.date).toLocaleDateString("de-DE") + " – " +
    new Date(last.date).toLocaleDateString("de-DE") + " (" + points.length + " Tage mit Wert; Skala " + pct(lo) + " – " + pct(hi) +
    "; Bestandteile zuletzt: " + last.market_count + ")";
}

function renderRangeButtons() {
  document.querySelectorAll("#ib-range button[data-days]").forEach((b) => b.classList.toggle("active", Number(b.dataset.days) === builder.days));
}

// ---- backfill: past days from the sources' price histories ---------------------------

async function backfillHistory() {
  if (!state.name) return;
  const name = state.name, btn = $("ib-backfill");
  btn.disabled = true;
  $("ib-caption").textContent = "Historische Tageskurse werden bei Polymarket und Kalshi abgerufen … (bis zu einer Minute)";
  logEvent("info", "button.click", { id: "ib-backfill", name });
  try {
    const res = await fetch("/indices/" + encodeURIComponent(name) + "/history/backfill?days=365", { method: "POST" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    logEvent("info", "builder.backfill.done", { name, markets: data.markets, rows: data.rows_added, days: data.days_added });
    await loadHistory(name);
    $("ib-caption").textContent += " · Nachgeladen: " + data.rows_added + " Tageswerte für " + data.markets_with_history +
      " von " + data.markets + " Märkten (Snapshots ab " + (data.status.first_date ? new Date(data.status.first_date).toLocaleDateString("de-DE") : "–") + ")";
  } catch (err) {
    $("ib-caption").textContent = "Historie konnte nicht nachgeladen werden: " + err.message;
    logEvent("error", "builder.backfill.failed", { name, message: err.message });
  } finally {
    btn.disabled = false;
  }
}

// ---- wiring -------------------------------------------------------------------------

document.addEventListener("index-loaded", (e) => {
  renderRuleSummary(state.rules[e.detail.name]);
  loadHistory(e.detail.name);
});
document.querySelectorAll("#ib-range button[data-days]").forEach((btn) => btn.addEventListener("click", () => {
  builder.days = Number(btn.dataset.days);
  try { localStorage.setItem(IB_DAYS_KEY, String(builder.days)); } catch {}
  logEvent("info", "button.click", { id: "ib-range", days: builder.days });
  renderRangeButtons();
  if (state.name && state.rules[state.name]) loadHistory(state.name);
}));
$("ib-backfill").addEventListener("click", backfillHistory);
renderRangeButtons();
