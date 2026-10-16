// Trade view of the correlation card: a click on a stock row shows the performance of the signal
// (strategy vs. buy & hold, trades marked), the index change and the next-day return overlapping, and the
// trade list (GET /indices/{name}/correlation/{ticker}, POST .../preview for unsaved rules).
// Relies on the globals of shell.js, builder.js and correlation.js ($, logEvent, state, builder, corr, signedPct).

const trd = { request: 0, data: null };
const SVG_NS = "http://www.w3.org/2000/svg";
const fmtDate = (iso) => new Date(iso).toLocaleDateString("de-DE");
const fmtPts = (v, digits = 2) => (v >= 0 ? "+" : "") + (v * 100).toFixed(digits).replace(".", ",") + " Pkt.";
// Index change in the unit of the shown index: index points (archive) or z-points (metric index, 1015-2231)
const fmtIdx = (v, digits = 2) => (corr.kind === "metric" ? (v >= 0 ? "+" : "") + v.toFixed(digits).replace(".", ",") + " z" : fmtPts(v, digits));

function openTrades(ticker) {
  corr.ticker = ticker;
  logEvent("info", "correlation.stock.click", { ticker, name: corr.name, kind: corr.kind, target: corr.kind === "metric" ? corr.target : null });
  if (corr.data) renderCorrelation(); // highlight the selected row
  loadTrades();
}

function closeTrades() {
  logEvent("info", "button.click", { id: "cr-detail-close", ticker: corr.ticker });
  corr.ticker = null;
  trd.request++;
  $("cr-detail").classList.add("hidden");
  if (corr.data) renderCorrelation();
}

async function loadTrades() {
  if (!corr.ticker || !corr.name) return;
  const ticket = ++trd.request, ticker = corr.ticker, rule = corr.rule;
  $("cr-detail").classList.remove("hidden");
  $("cr-detail-title").textContent = ticker;
  $("cr-detail-caption").textContent = "Trades werden berechnet …";
  if (corr.kind === "metric") renderRegressionDetail(null); // regression.js; filled from this payload
  else startAttribution(ticker, rule);
  const url = corrUrl("/" + encodeURIComponent(ticker) + (rule ? "/preview" : ""));
  try {
    const res = await fetch(url, rule
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(rule) }
      : undefined);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (ticket !== trd.request) return;
    trd.data = data;
    renderTrades(data);
    if (corr.kind === "metric") renderRegressionDetail(data);
    logEvent("info", "correlation.trades.loaded", { ticker, name: corr.name, kind: corr.kind, target: data.target, days: data.days,
      trades: data.summary.trades, preview: !!rule });
  } catch (err) {
    if (ticket !== trd.request) return;
    trd.data = null;
    renderTrades(null);
    $("cr-detail-caption").textContent = "Trades konnten nicht geladen werden: " + err.message;
    logEvent("error", "correlation.trades.failed", { ticker, message: err.message });
  }
}

// Component attribution (attribution.js, 1015-1200) loads independently: any failure there, even a script that
// did not load at all, is logged but never stops the KPIs, charts, robustness and trade list.
function startAttribution(ticker, rule) {
  const fail = (err) => logEvent("error", "correlation.attribution.failed", { ticker, message: String(err?.message ?? err) });
  try { Promise.resolve(loadAttribution(ticker, rule)).catch(fail); } catch (err) { fail(err); }
}

function setKpi(id, value, signed) {
  const el = $(id);
  el.textContent = value == null ? "–" : signed ? signedPct(value, 1) : value;
  el.className = !signed || value == null ? "" : value >= 0 ? "up" : "down";
}

function renderTrades(d) {
  const s = d ? d.summary : {};
  $("cr-detail-title").textContent = d ? d.ticker + " · " + d.name : corr.ticker || "";
  setKpi("cr-t-strategy", s.strategy_return, true);
  setKpi("cr-t-buyhold", s.buy_hold_return, true);
  setKpi("cr-t-count", d ? s.trades + " (" + s.longs + " Long / " + s.shorts + " Short)" : null);
  setKpi("cr-t-win", s.win_rate == null ? null : (s.win_rate * 100).toFixed(0) + " %");
  setKpi("cr-t-direction", d ? (d.direction > 0 ? "Index ↑ → Long" : "Index ↑ → Short") : null);
  const rows = d ? d.series : [];
  drawChart($("cr-perf"), rows, [
    { key: "strategy", cls: "strategy" }, { key: "buy_hold", cls: "buyhold" },
  ], true);
  drawChart($("cr-returns"), rows, [
    { key: "index_change", cls: "signal", scaled: true }, { key: "stock_return", cls: "stock", scaled: true },
  ], false);
  renderTradeTable(rows);
  renderRobustness(d);
  $("cr-detail-caption").textContent = !d ? "" : !rows.length
    ? "Keine Signaltage mit Kurs am Folgetag – Kurse laden oder Zeitraum vergrößern."
    : "Regel: Ändert sich der Index am Tag T" + (corrMinChange() ? " um mehr als " + fmtIdx(corrMinChange(), corr.kind === "metric" ? 2 : 1).replace("+", "±") : "") +
      ", wird zum Schlusskurs T gekauft (bzw. leerverkauft) und zum nächsten Schlusskurs geschlossen; ohne Kosten. " +
      "Die Richtung stammt aus der Korrelation des ganzen Zeitraums (" + fmtCorr(d.correlation) + ") – " +
      "das ist eine rückblickende Plausibilitätsprüfung, kein echter Backtest. Keine Anlageempfehlung." +
      (d.n < corr.data?.min_observations ? " · Zu wenige Paare für eine Korrelation." : "") +
      (corr.kind === "metric" && d.target === "excess" ? " Rendite = Überrendite gegenüber dem Ø der Top 100 (Aktie abgesichert gegen den Markt)." : "") +
      (corr.rule ? " · Vorschau mit ungespeicherten Regeln" : "");
}

// Lines over the signal days; ``scaled`` series are each stretched to their own 98 % quantile of |value|
// (two y-axes; rare outliers such as a constituent jump are clipped at the edge instead of flattening the rest).
function drawChart(svg, rows, series, markTrades) {
  svg.replaceChildren();
  if (!rows.length) return;
  const W = 600, H = 180, pad = 10;
  const el = (tag, attrs, text) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text) { const t = document.createElementNS(SVG_NS, "title"); t.textContent = text; node.appendChild(t); }
    svg.appendChild(node);
    return node;
  };
  const scaledSeries = series.map((s) => {
    const vals = rows.map((r) => r[s.key]);
    const abs = vals.map(Math.abs).sort((a, b) => a - b);
    const m = abs[Math.floor(0.98 * (abs.length - 1))] || abs[abs.length - 1] || 1;
    const clamp = (v) => Math.max(-1, Math.min(1, v / m));
    return { ...s, max: m, clipped: abs[abs.length - 1] > m, vals: s.scaled ? vals.map(clamp) : vals };
  });
  const all = scaledSeries.flatMap((s) => s.vals).concat(0);
  let lo = Math.min(...all), hi = Math.max(...all);
  if (scaledSeries.every((s) => s.scaled)) { lo = -1; hi = 1; } // zero line in the middle
  if (hi - lo < 1e-9) { lo -= 0.01; hi += 0.01; }
  const band = (W - 2 * pad) / Math.max(rows.length - 1, 1);
  const x = (i) => (rows.length === 1 ? W / 2 : pad + i * band);
  const y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad);
  // background bands on trade days: green = trade won, red = trade lost
  rows.forEach((r, i) => {
    if (!r.position) return;
    el("rect", { class: "trade " + (r.trade_return > 0 ? "win" : "loss"), x: (x(i) - band / 2).toFixed(1), y: 0,
      width: Math.max(band, 1).toFixed(1), height: H });
  });
  el("line", { class: "zero", x1: pad, x2: W - pad, y1: y(0).toFixed(1), y2: y(0).toFixed(1) });
  // axis labels: value range (per series when scaled) and date range
  const label = (x0, y0, text, cls = "", anchor = "start") => el("text", { class: "axis " + cls, x: x0, y: y0, "text-anchor": anchor }).textContent = text;
  if (scaledSeries.every((s) => s.scaled)) {
    scaledSeries.forEach((s, k) => label(k ? W - pad : pad, 12, "± " + (s.key === "index_change" ? fmtIdx(s.max).slice(1) : signedPct(s.max).slice(1)) + (s.clipped ? " (Ausreißer gekappt)" : ""), s.cls, k ? "end" : "start"));
  } else {
    label(pad, 12, signedPct(hi, 1));
    label(pad, H - pad - 14, signedPct(lo, 1));
  }
  label(pad, H - 1, fmtDate(rows[0].date), "date");
  label(W - pad, H - 1, fmtDate(rows[rows.length - 1].date), "date", "end");
  for (const s of scaledSeries) {
    el("path", { class: "line " + s.cls, d: s.vals.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ") });
  }
  rows.forEach((r, i) => {
    const tip = fmtDate(r.date) + ": Index " + fmtIdx(r.index_change) + " · Rendite " + fmtDate(r.exit_date) + " " +
      signedPct(r.stock_return) + (r.position ? " · " + (r.position > 0 ? "Long" : "Short") + " " + signedPct(r.trade_return) : " · kein Trade") +
      " · Strategie " + signedPct(r.strategy, 1) + " · Buy & Hold " + signedPct(r.buy_hold, 1);
    if (markTrades && r.position) {
      const cx = x(i), cy = y(r.strategy), d = r.position > 0 ? 1 : -1; // ▲ long, ▼ short
      el("path", { class: "marker " + (r.trade_return > 0 ? "win" : "loss"),
        d: "M" + cx.toFixed(1) + " " + (cy - 6 * d).toFixed(1) + " L" + (cx - 4).toFixed(1) + " " + (cy + 3 * d).toFixed(1) +
          " L" + (cx + 4).toFixed(1) + " " + (cy + 3 * d).toFixed(1) + " Z" });
    }
    el("rect", { class: "hover", x: (x(i) - band / 2).toFixed(1), y: 0, width: Math.max(band, 1).toFixed(1), height: H }, tip);
  });
}

// 3×3 grid index threshold × stock threshold (1015-1157); the cell of the current settings is outlined
function renderRobustness(d) {
  const body = $("cr-robust-rows");
  body.replaceChildren();
  const cells = d && d.robustness ? d.robustness : [];
  for (const change of [...new Set(cells.map((c) => c.min_change))]) {
    const row = document.createElement("tr");
    const head = document.createElement("td");
    head.textContent = fmtIdx(change, corr.kind === "metric" ? 2 : 1).replace("+", "±");
    row.appendChild(head);
    for (const c of cells.filter((cell) => cell.min_change === change)) {
      const td = document.createElement("td");
      td.className = "num" + (c.min_change === corrMinChange() && c.min_stock_move === corr.minStockMove ? " current" : "");
      td.textContent = (c.precision == null ? "zu wenig" : (c.precision * 100).toFixed(0) + " %") + " (" + c.signals + ")";
      td.title = c.signal_hits + " Treffer von " + c.signals + " Signalen · Basisrate " +
        (c.base_rate == null ? "–" : (c.base_rate * 100).toFixed(0) + " %") + (c.p_signal == null ? "" : " · p " + fmtP(c.p_signal));
      row.appendChild(td);
    }
    body.appendChild(row);
  }
  $("cr-robust-caption").textContent = !cells.length ? "" :
    "Precision im Testteil (Anzahl Signale) je Schwellen-Kombination; unter " + d.min_signals + " Signalen „zu wenig“. " +
    "Nur zur Prüfung der Stabilität: Die beste Zelle darf nicht nachträglich ausgewählt werden – das wäre Überanpassung, " +
    "ein echtes Signal muss über benachbarte Schwellen ähnlich bleiben.";
}

function renderTradeTable(rows) {
  const body = $("cr-trade-rows");
  body.replaceChildren();
  for (const r of rows.filter((row) => row.position).reverse()) { // newest first
    const row = document.createElement("tr");
    const cells = [
      [fmtDate(r.date), ""], [fmtIdx(r.index_change), "num " + (r.index_change >= 0 ? "up" : "down")],
      [r.position > 0 ? "▲ Long" : "▼ Short", ""], [fmtDate(r.exit_date), ""],
      [signedPct(r.stock_return), "num"], [signedPct(r.trade_return), "num " + (r.trade_return > 0 ? "up" : "down")],
      [signedPct(r.strategy, 1), "num"],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      if (cls) td.className = cls;
      row.appendChild(td);
    }
    body.appendChild(row);
  }
}

// ---- wiring -------------------------------------------------------------------------

document.addEventListener("correlation-loaded", () => { if (corr.ticker) loadTrades(); });
$("cr-detail-close").addEventListener("click", closeTrades);
