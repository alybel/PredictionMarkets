// Tab "Datenqualität": point-in-time (look-ahead) guards, cleanliness checks, coverage per day and a
// market browser over the stored daily rows. Uses the globals $, fmtNum and logEvent; loads on first view.

const QUAL_STATUS = { ok: ["OK", "ok"], blocked: ["Gesperrt", "bad"], warn: ["Prüfen", ""], info: ["Info", "info"] };
const QUAL_VIOLATIONS = { future_date: "Zukunftsdatum", outside_day: "Außerhalb des Tages", after_close: "Nach Börsenschluss" };
let qualLoaded = false;

function qualCell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text;
  if (cls) td.className = cls;
  return td;
}

function qualCheckRow(check) {
  const tr = document.createElement("tr");
  const [label, cls] = QUAL_STATUS[check.status] || [check.status, ""];
  const badge = document.createElement("span");
  badge.className = "badge " + cls;
  badge.textContent = label;
  const status = document.createElement("td");
  status.appendChild(badge);
  const detail = qualCell(check.detail, "detail");
  if (check.examples.length) {
    const box = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Beispiele (" + check.examples.length + ")";
    const pre = document.createElement("pre");
    pre.textContent = check.examples.map((e) => Object.entries(e).map(([k, v]) => k + ": " + v).join(" · ")).join("\n");
    box.append(summary, pre);
    box.addEventListener("toggle", () => box.open && logEvent("info", "quality.examples.open", { check: check.key }));
    detail.appendChild(box);
  }
  tr.append(status, qualCell(check.label), qualCell(check.status === "info" && !check.count ? "–" : fmtNum(check.count), "num"), detail);
  return tr;
}

function qualKpi(label, value) {
  const div = document.createElement("div");
  div.className = "kpi";
  const strong = document.createElement("strong");
  strong.textContent = value;
  div.append(label, strong);
  return div;
}

function renderQuality(r) {
  const s = r.snapshots;
  const range = s.first_date ? new Date(s.first_date).toLocaleDateString("de-DE") + " – " + new Date(s.last_date).toLocaleDateString("de-DE") : "–";
  $("qual-kpis").replaceChildren(
    qualKpi("Zeitraum", range),
    qualKpi("Snapshot-Tage (davon live)", fmtNum(s.days) + " (" + fmtNum(s.live_days) + ")"),
    qualKpi("Marktzeilen (davon rückgefüllt)", fmtNum(s.rows) + " (" + fmtNum(s.backfill_rows) + ")"),
    qualKpi("Gesperrte Marktzeilen", fmtNum(s.blocked_rows)),
    qualKpi("Aktienkurse (Aktien)", fmtNum(r.stocks.rows) + " (" + r.stocks.tickers + "/" + r.stocks.universe + ")"),
  );
  const blocked = r.lookahead.filter((c) => c.status === "blocked").length;
  const warn = r.cleanliness.filter((c) => c.status === "warn").length;
  $("qual-status").textContent = (blocked ? blocked + " Lookahead-Prüfung(en) haben Zeilen gesperrt. " : "Kein Lookahead-Verstoß gefunden. ")
    + (warn ? warn + " Sauberkeits-Prüfung(en) zum Ansehen." : "Keine Auffälligkeiten in der Datensauberkeit.");
  $("qual-lookahead").replaceChildren(...r.lookahead.map(qualCheckRow));
  $("qual-clean").replaceChildren(...r.cleanliness.map(qualCheckRow));
  $("qual-coverage").replaceChildren(...r.coverage.map((d) => {
    const tr = document.createElement("tr");
    if (d.blocked_rows) tr.className = "qual-flag";
    tr.append(qualCell(new Date(d.date).toLocaleDateString("de-DE")), qualCell(fmtNum(d.live_rows), "num"),
      qualCell(fmtNum(d.backfill_rows), "num"), qualCell(fmtNum(d.blocked_rows), "num"), qualCell(fmtNum(d.stock_closes), "num"));
    return tr;
  }));
  $("qual-time").textContent = "Stand " + new Date(r.generated_at).toLocaleTimeString("de-DE");
}

async function loadQuality(reason) {
  $("qual-status").textContent = "Prüfe gespeicherte Daten …";
  logEvent("info", "quality.load", { reason });
  try {
    const res = await fetch("/quality/report");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const report = await res.json();
    renderQuality(report);
    qualLoaded = true;
    logEvent("info", "quality.loaded", { days: report.snapshots.days, blocked_rows: report.snapshots.blocked_rows });
  } catch (err) {
    $("qual-status").textContent = "Prüfung fehlgeschlagen: " + err.message;
    logEvent("error", "quality.failed", { message: err.message });
  }
}

async function searchQualityMarkets() {
  const q = $("qual-search").value.trim();
  if (q.length < 2) return;
  logEvent("info", "quality.search", { q });
  try {
    const res = await fetch("/quality/markets?q=" + encodeURIComponent(q));
    if (!res.ok) throw new Error("HTTP " + res.status);
    const hits = await res.json();
    $("qual-hits").replaceChildren(...(hits.length ? hits.map((h) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = h.title;
      btn.addEventListener("click", () => loadQualityMarket(h));
      li.append(btn, " ", Object.assign(document.createElement("span"), {
        className: "muted", textContent: "· " + h.source + " · " + h.days + " Tage (" + h.first_date + " – " + h.last_date + ")" }));
      return li;
    }) : [Object.assign(document.createElement("li"), { className: "muted", textContent: "Keine Treffer." })]));
  } catch (err) {
    logEvent("error", "quality.search.failed", { q, message: err.message });
    $("qual-hits").replaceChildren(Object.assign(document.createElement("li"), { className: "error-text", textContent: "Suche fehlgeschlagen: " + err.message }));
  }
}

async function loadQualityMarket(hit) {
  logEvent("info", "quality.market.open", { source: hit.source, external_id: hit.external_id });
  try {
    const res = await fetch("/quality/markets/" + encodeURIComponent(hit.source) + "/" + encodeURIComponent(hit.external_id));
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    $("qual-market-title").textContent = hit.title + " (" + hit.source + ")";
    $("qual-rows").replaceChildren(...data.rows.slice().reverse().map((r) => {
      const tr = document.createElement("tr");
      if (r.violation || r.jump) tr.className = "qual-flag";
      const verdict = r.violation ? "Gesperrt: " + (QUAL_VIOLATIONS[r.violation] || r.violation) : r.jump ? "Preissprung" : "OK";
      tr.append(qualCell(new Date(r.date).toLocaleDateString("de-DE")), qualCell(r.price == null ? "–" : fmtNum(r.price, 3), "num"),
        qualCell(fmtNum(r.volume_24h_usd), "num"), qualCell(r.origin === "backfill" ? "rückgefüllt" : "live"),
        qualCell(r.origin === "backfill" ? "aus Quell-Historie" : (r.fetched_at || "–").replace("T", " ").slice(0, 16)), qualCell(verdict));
      return tr;
    }));
    $("qual-market").classList.remove("hidden");
  } catch (err) {
    logEvent("error", "quality.market.failed", { external_id: hit.external_id, message: err.message });
  }
}

$("qual-refresh").addEventListener("click", () => loadQuality("button"));
$("qual-search-go").addEventListener("click", searchQualityMarkets);
$("qual-search").addEventListener("keydown", (e) => { if (e.key === "Enter") searchQualityMarkets(); });
document.querySelector('.tab[data-view="quality"]').addEventListener("click", () => { if (!qualLoaded) loadQuality("tab"); });
if (!document.querySelector('[data-view-panel="quality"]').classList.contains("hidden")) loadQuality("start");
