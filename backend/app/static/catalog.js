// Tab "Datenkatalog": every stored prediction market (paged, filterable) and every stock of the universe with
// period, quality grade and, on click, the full daily history (chart + rows). Uses $, fmtNum, logEvent and
// metricChart (metrics.js); loads on first view.

const CAT_PAGE = 50;
const CAT_GRADES = { good: ["gut", "ok"], fair: ["mittel", ""], poor: ["schwach", "bad"] };
const CAT_VIOLATIONS = { future_date: "Zukunftsdatum", outside_day: "Außerhalb des Tages", after_close: "Nach Börsenschluss",
  provisional_close: "Vorläufiger Kurs" };
const catState = { loaded: false, offset: 0, total: 0, stocks: [] };

const catDate = (iso) => (iso ? new Date(iso).toLocaleDateString("de-DE") : "–");
const catPct = (v) => (v == null ? "–" : (v * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " %");

function catCell(content, cls) {
  const td = document.createElement("td");
  if (content instanceof Node) td.appendChild(content); else td.textContent = content;
  if (cls) td.className = cls;
  return td;
}

function catGrade(entry) {
  const [label, cls] = CAT_GRADES[entry.grade] || [entry.grade, ""];
  const wrap = document.createElement("span");
  const badge = Object.assign(document.createElement("span"), { className: "badge " + cls, textContent: label });
  wrap.appendChild(badge);
  if (entry.issues.length) wrap.appendChild(Object.assign(document.createElement("span"), { className: "cat-issues", textContent: entry.issues.join(" · ") }));
  return wrap;
}

function catKpi(label, value) {
  const div = Object.assign(document.createElement("div"), { className: "kpi" });
  div.append(label, Object.assign(document.createElement("strong"), { textContent: value }));
  return div;
}

async function catFetch(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// ---- overview -------------------------------------------------------------------------

async function loadCatalogSummary() {
  try {
    const s = await catFetch("/catalog/summary");
    $("cat-kpis").replaceChildren(...s.sources.map((src) => catKpi(src.source.charAt(0).toUpperCase() + src.source.slice(1) + " (Märkte / Tageszeilen)",
      fmtNum(src.markets) + " / " + fmtNum(src.rows) + " · " + catDate(src.first_date) + " – " + catDate(src.last_date))),
      catKpi("Aktien mit Kursen", s.stocks.with_prices + " von " + s.stocks.tickers + " · " + fmtNum(s.stocks.rows) + " Kurstage"));
    $("cat-time").textContent = "Stand " + new Date().toLocaleTimeString("de-DE");
  } catch (err) {
    $("cat-kpis").replaceChildren(Object.assign(document.createElement("p"), { className: "error-text", textContent: "Übersicht fehlgeschlagen: " + err.message }));
    logEvent("error", "catalog.summary.failed", { message: err.message });
  }
}

// ---- prediction markets ------------------------------------------------------------------

function catMarketQuery() {
  return { q: $("cat-q").value.trim(), source: $("cat-source").value, min_days: $("cat-min-days").value,
    sort: $("cat-sort").value, current: $("cat-current").checked };
}

async function loadCatalogMarkets(offset, reason) {
  const query = catMarketQuery();
  logEvent("info", "catalog.markets.load", { ...query, offset, reason });
  $("cat-status").textContent = "Lade Märkte …";
  const params = new URLSearchParams({ ...query, current: query.current ? "true" : "false", limit: CAT_PAGE, offset });
  try {
    const data = await catFetch("/catalog/markets?" + params);
    catState.offset = offset;
    catState.total = data.total;
    $("cat-rows").replaceChildren(...data.items.map(catMarketRow));
    if (!data.items.length) $("cat-rows").appendChild(Object.assign(document.createElement("tr"), { innerHTML: '<td colspan="8" class="muted">Keine Märkte gefunden.</td>' }));
    $("cat-status").textContent = data.total ? fmtNum(offset + 1) + "–" + fmtNum(offset + data.items.length) + " von " + fmtNum(data.total)
      + " Märkten · neuester Snapshot " + catDate(data.latest_date) : "Keine Märkte gefunden.";
    $("cat-prev").disabled = offset === 0;
    $("cat-next").disabled = offset + data.items.length >= data.total;
    logEvent("info", "catalog.markets.loaded", { total: data.total, offset, shown: data.items.length });
  } catch (err) {
    $("cat-status").textContent = "Laden fehlgeschlagen: " + err.message;
    logEvent("error", "catalog.markets.failed", { message: err.message });
  }
}

function catMarketRow(m) {
  const tr = document.createElement("tr");
  const title = Object.assign(document.createElement("span"), { textContent: m.title });
  if (!m.current) title.title = "Seit " + catDate(m.last_date) + " nicht mehr im Tagesstand";
  tr.append(catCell(title, "cat-title"), catCell(Object.assign(document.createElement("span"), { className: "source-pill", textContent: m.source })),
    catCell(catDate(m.first_date) + " – " + catDate(m.last_date)), catCell(fmtNum(m.days), "num"), catCell(catPct(m.coverage), "num"),
    catCell(m.last_price == null ? "–" : fmtNum(m.last_price, 3), "num"), catCell(fmtNum(m.avg_volume_24h_usd), "num"), catCell(catGrade(m)));
  tr.addEventListener("click", () => openCatalogMarket(m, tr));
  return tr;
}

function catSelect(tr) {
  document.querySelectorAll(".cat-scroll tr.selected").forEach((r) => r.classList.remove("selected"));
  tr?.classList.add("selected");
}

function catShowDetail(title, meta, series, head, rows) {
  $("cat-detail-title").textContent = title;
  $("cat-detail-meta").textContent = meta;
  $("cat-chart").replaceChildren(metricChart(series));
  const hr = document.createElement("tr");
  hr.append(...head.map(([label, cls]) => Object.assign(document.createElement("th"), { textContent: label, className: cls || "" })));
  $("cat-detail-head").replaceChildren(hr);
  $("cat-detail-rows").replaceChildren(...rows);
  $("cat-detail").classList.remove("hidden");
  $("cat-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function openCatalogMarket(m, tr) {
  logEvent("info", "catalog.market.open", { source: m.source, external_id: m.external_id });
  catSelect(tr);
  try {
    const data = await catFetch("/catalog/markets/" + encodeURIComponent(m.source) + "/" + encodeURIComponent(m.external_id));
    const s = data.summary;
    const series = data.rows.filter((r) => r.price != null).map((r) => ({ date: r.date, value: r.price * 100 }));
    const rows = data.rows.slice().reverse().map((r) => {
      const row = document.createElement("tr");
      if (r.violation || r.jump) row.className = "qual-flag";
      row.append(catCell(catDate(r.date)), catCell(r.price == null ? "–" : fmtNum(r.price, 3), "num"), catCell(fmtNum(r.volume_24h_usd), "num"),
        catCell(fmtNum(r.depth_usd), "num"), catCell(r.origin === "backfill" ? "rückgefüllt" : "live"),
        catCell(r.violation ? "Gesperrt: " + (CAT_VIOLATIONS[r.violation] || r.violation) : r.jump ? "Preissprung" : "OK"));
      return row;
    });
    const grade = (CAT_GRADES[s.grade] || [s.grade])[0];
    catShowDetail(s.title + " (" + s.source + ")", "Preisverlauf in % · " + fmtNum(s.days) + " Tage, Abdeckung " + catPct(s.coverage)
      + " · Qualität " + grade + (s.issues.length ? " (" + s.issues.join(", ") + ")" : "") + (m.url ? " · " + m.url : ""), series,
      [["Datum"], ["Preis", "num"], ["24h-Volumen (USD)", "num"], ["Orderbuch-Tiefe (USD)", "num"], ["Herkunft"], ["Prüfung"]], rows);
  } catch (err) {
    logEvent("error", "catalog.market.failed", { external_id: m.external_id, message: err.message });
    $("cat-status").textContent = "Verlauf konnte nicht geladen werden: " + err.message;
  }
}

// ---- stocks --------------------------------------------------------------------------------

function renderCatalogStocks() {
  const q = $("cat-stock-q").value.trim().toLowerCase();
  const list = catState.stocks.filter((s) => !q || s.ticker.toLowerCase().includes(q) || s.name.toLowerCase().includes(q));
  $("cat-stock-rows").replaceChildren(...list.map((s) => {
    const tr = document.createElement("tr");
    const ret = (v) => catCell(catPct(v), "num" + (v > 0 ? " up" : v < 0 ? " down" : ""));
    tr.append(catCell(s.ticker), catCell(s.name), catCell(s.first_date ? catDate(s.first_date) + " – " + catDate(s.last_date) : "–"),
      catCell(fmtNum(s.days), "num"), catCell(s.last_close == null ? "–" : fmtNum(s.last_close, 2), "num"), ret(s.return_1m), ret(s.return_total),
      catCell(catGrade(s)));
    if (s.days) tr.addEventListener("click", () => openCatalogStock(s, tr));
    return tr;
  }));
}

async function loadCatalogStocks() {
  try {
    catState.stocks = await catFetch("/catalog/stocks");
    renderCatalogStocks();
    logEvent("info", "catalog.stocks.loaded", { tickers: catState.stocks.length });
  } catch (err) {
    $("cat-stock-rows").replaceChildren(Object.assign(document.createElement("tr"), { innerHTML: '<td colspan="8" class="error-text"></td>' }));
    $("cat-stock-rows").querySelector("td").textContent = "Aktien konnten nicht geladen werden: " + err.message;
    logEvent("error", "catalog.stocks.failed", { message: err.message });
  }
}

async function openCatalogStock(s, tr) {
  logEvent("info", "catalog.stock.open", { ticker: s.ticker });
  catSelect(tr);
  try {
    const data = await catFetch("/catalog/stocks/" + encodeURIComponent(s.ticker));
    const valid = data.rows.filter((r) => !r.violation && r.close > 0);
    const base = valid.length ? valid[0].close : 1;
    const series = valid.map((r) => ({ date: r.date, value: (r.close / base - 1) * 100 }));
    const rows = data.rows.slice().reverse().map((r) => {
      const row = document.createElement("tr");
      if (r.violation) row.className = "qual-flag";
      row.append(catCell(catDate(r.date)), catCell(fmtNum(r.close, 2), "num"), catCell(catPct(r["return"]), "num"),
        catCell(r.violation ? "Gesperrt: " + (CAT_VIOLATIONS[r.violation] || r.violation) : "OK"));
      return row;
    });
    const sum = data.summary;
    catShowDetail(sum.ticker + " · " + sum.name, "Wertentwicklung in % seit " + catDate(sum.first_date) + " · " + fmtNum(sum.days)
      + " Kurstage · Qualität " + (CAT_GRADES[sum.grade] || [sum.grade])[0] + (sum.issues.length ? " (" + sum.issues.join(", ") + ")" : ""),
      series, [["Datum"], ["Schlusskurs (USD)", "num"], ["Tagesrendite", "num"], ["Prüfung"]], rows);
  } catch (err) {
    logEvent("error", "catalog.stock.failed", { ticker: s.ticker, message: err.message });
  }
}

// ---- wiring ------------------------------------------------------------------------------

function loadCatalog(reason) {
  logEvent("info", "catalog.load", { reason });
  catState.loaded = true;
  loadCatalogSummary();
  loadCatalogMarkets(0, reason);
  loadCatalogStocks();
}

$("cat-refresh").addEventListener("click", () => loadCatalog("button"));
$("cat-search").addEventListener("click", () => loadCatalogMarkets(0, "button"));
$("cat-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadCatalogMarkets(0, "enter"); });
["cat-source", "cat-min-days", "cat-sort", "cat-current"].forEach((id) => $(id).addEventListener("change", () => {
  logEvent("info", "catalog.filter", { field: id, value: $(id).type === "checkbox" ? $(id).checked : $(id).value });
  loadCatalogMarkets(0, "filter");
}));
$("cat-prev").addEventListener("click", () => loadCatalogMarkets(Math.max(0, catState.offset - CAT_PAGE), "prev"));
$("cat-next").addEventListener("click", () => loadCatalogMarkets(catState.offset + CAT_PAGE, "next"));
$("cat-stock-q").addEventListener("input", () => renderCatalogStocks());
$("cat-stock-q").addEventListener("change", () => logEvent("info", "catalog.stocks.filter", { q: $("cat-stock-q").value.trim() }));
$("cat-detail-close").addEventListener("click", () => {
  logEvent("info", "catalog.detail.close");
  $("cat-detail").classList.add("hidden");
  catSelect(null);
});
document.querySelector('.tab[data-view="catalog"]').addEventListener("click", () => { if (!catState.loaded) loadCatalog("tab"); });
if (!document.querySelector('[data-view-panel="catalog"]').classList.contains("hidden")) loadCatalog("start");
