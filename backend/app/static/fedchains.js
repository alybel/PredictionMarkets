// Section "Fed-Zinsketten" in the tab "Datenqualität": chain chart (front + second, roll days), checks, coverage bars
// per meeting and source, drill-down to the raw outcome prices of one day. Uses $, fmtNum, logEvent, qualCell and
// qualCheckRow (quality.js), fedServerError (fedstaging.js); loads together with the tab.

const FED_SVG = "http://www.w3.org/2000/svg";
const FED_W = 800, FED_H = 220, FED_PAD = 24;
const FED_SOURCES = { kalshi: "Kalshi", polymarket: "Polymarket", combined: "Kombiniert" };
const FED_CHAIN_NAMES = { front: "Front", second: "Zweit" };
let fedChains = null;
let fedLoaded = false;

const fedDay = (iso) => new Date(iso + "T00:00:00Z").getTime();
const fedDate = (iso) => new Date(iso + "T00:00:00Z").toLocaleDateString("de-DE", { timeZone: "UTC" });
const fedBp = (v) => (v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(2).replace(".", ","));

function fedNode(tag, attrs) {
  const n = document.createElementNS(FED_SVG, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

// x-axis ticks: month starts (UTC) at the smallest step that keeps at most `max` ticks within [x0, x1]
function fedAxisTicks(x0, x1, max = 8) {
  const months = (t) => new Date(t).getUTCFullYear() * 12 + new Date(t).getUTCMonth();
  const step = [1, 2, 3, 6, 12, 24, 60].find((s) => (months(x1) - months(x0)) / s < max) || 120;
  const ticks = [];
  for (let m = Math.ceil(months(x0) / step) * step; m <= months(x1); m += step) {
    const t = Date.UTC(Math.floor(m / 12), m % 12, 1);
    if (t < x0) continue;
    const label = new Date(t).toLocaleDateString("de-DE", step >= 12 ? { year: "numeric", timeZone: "UTC" } : { month: "short", year: "numeric", timeZone: "UTC" });
    ticks.push({ t, label });
  }
  return ticks;
}

function fedSeries() {
  return $("fed-series").value;
}

function renderFedChart() {
  const svg = $("fed-chart");
  svg.replaceChildren();
  const data = fedChains && fedChains.series[fedSeries()];
  const pts = data ? data.front.concat(data.second).filter((p) => p.value != null) : [];
  $("fed-xaxis").replaceChildren();
  if (!pts.length) { $("fed-chart-caption").textContent = "keine Daten"; return; }
  const days = data.front.map((p) => fedDay(p.date));
  const x0 = Math.min(...days), x1 = Math.max(...days, x0 + 1);
  let lo = Math.min(0, ...pts.map((p) => p.value)), hi = Math.max(0, ...pts.map((p) => p.value));
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const x = (iso) => FED_PAD + ((fedDay(iso) - x0) / (x1 - x0)) * (FED_W - 2 * FED_PAD);
  const y = (v) => FED_H - FED_PAD - ((v - lo) / (hi - lo)) * (FED_H - 2 * FED_PAD);
  svg.appendChild(fedNode("line", { class: "zero", x1: FED_PAD, x2: FED_W - FED_PAD, y1: y(0), y2: y(0), "vector-effect": "non-scaling-stroke" }));
  for (const p of data.front.filter((q) => q.roll)) {
    svg.appendChild(fedNode("line", { class: "roll", x1: x(p.date), x2: x(p.date), y1: FED_PAD / 2, y2: FED_H - FED_PAD / 2, "vector-effect": "non-scaling-stroke" }));
  }
  // x-axis: baseline with ticks in the SVG, labels as HTML (the SVG stretches horizontally, text would distort)
  const base = FED_H - FED_PAD / 2;
  svg.appendChild(fedNode("line", { class: "axis", x1: FED_PAD, x2: FED_W - FED_PAD, y1: base, y2: base, "vector-effect": "non-scaling-stroke" }));
  const ticks = fedAxisTicks(x0, x1);
  for (const tk of ticks) {
    const tx = FED_PAD + ((tk.t - x0) / (x1 - x0)) * (FED_W - 2 * FED_PAD);
    svg.appendChild(fedNode("line", { class: "axis", x1: tx, x2: tx, y1: base, y2: base + 6, "vector-effect": "non-scaling-stroke" }));
    const lab = document.createElement("span");
    lab.textContent = tk.label;
    lab.style.left = (tx / FED_W * 100).toFixed(2) + "%";
    $("fed-xaxis").appendChild(lab);
  }
  for (const name of ["second", "front"]) {
    // a null value or a roll starts a new segment: the chain changes meeting, the line is not drawn across it
    let d = "", open = false;
    for (const p of data[name]) {
      if (p.value == null) { open = false; continue; }
      d += (open && !p.roll ? "L" : "M") + x(p.date).toFixed(1) + " " + y(p.value).toFixed(1) + " ";
      open = true;
    }
    svg.appendChild(fedNode("path", { class: "line " + name, d, "vector-effect": "non-scaling-stroke" }));
  }
  const rolls = data.front.filter((p) => p.roll).length;
  $("fed-chart-caption").textContent = FED_SOURCES[fedSeries()] + ": " + fedDate(data.front[0].date) + " – "
    + fedDate(data.front[data.front.length - 1].date) + ", " + data.front.length + " Tage, " + rolls + " Roll-Tage, Skala "
    + fedBp(lo) + " bis " + fedBp(hi) + " bp";
  svg.onclick = (e) => {
    const box = svg.getBoundingClientRect();
    const t = x0 + ((e.clientX - box.left) / box.width * FED_W - FED_PAD) / (FED_W - 2 * FED_PAD) * (x1 - x0);
    const near = data.front.reduce((a, b) => (Math.abs(fedDay(b.date) - t) < Math.abs(fedDay(a.date) - t) ? b : a));
    logEvent("info", "fed.chart.click", { series: fedSeries(), date: near.date });
    openFedDay(near.date);
  };
}

function renderFedCoverage(cov) {
  const box = $("fed-coverage");
  if (!cov.rows.length) { box.replaceChildren(Object.assign(document.createElement("p"), { className: "muted", textContent: "Noch keine Rohdaten – „Rohdaten nachladen“ klicken." })); return; }
  const x0 = fedDay(cov.first_date), span = Math.max(fedDay(cov.last_date) - x0, 1);
  const pct = (iso) => ((fedDay(iso) - x0) / span) * 100;
  box.replaceChildren(...cov.rows.slice().reverse().map((r) => {
    const row = document.createElement("div");
    row.className = "fed-cov-row";
    row.title = r.days + " Tage, " + r.gaps.length + " Lücke(n) – Klick zeigt den letzten Tag";
    const label = Object.assign(document.createElement("span"), { className: "fed-cov-label", textContent: fedDate(r.session_date) + " · " + FED_SOURCES[r.source] });
    const track = Object.assign(document.createElement("span"), { className: "fed-cov-track" });
    const bar = Object.assign(document.createElement("span"), { className: "fed-cov-bar " + r.source });
    bar.style.left = pct(r.first_date) + "%";
    bar.style.width = Math.max(pct(r.last_date) - pct(r.first_date), 0.3) + "%";
    track.appendChild(bar);
    for (const g of r.gaps) {
      const gap = Object.assign(document.createElement("span"), { className: "fed-cov-gap", title: "Lücke " + fedDate(g.from) + " – " + fedDate(g.to) });
      gap.style.left = pct(g.from) + "%";
      gap.style.width = Math.max(pct(g.to) - pct(g.from), 0.25) + "%";
      track.appendChild(gap);
    }
    const info = Object.assign(document.createElement("span"), { className: "fed-cov-info muted", textContent: r.days + " T" + (r.gaps.length ? " · " + r.gaps.length + " Lücken" : "") });
    row.append(label, track, info);
    row.addEventListener("click", () => {
      logEvent("info", "fed.coverage.click", { session_date: r.session_date, source: r.source });
      openFedDay(r.last_date);
    });
    return row;
  }));
}

async function openFedDay(day) {
  logEvent("info", "fed.day.open", { date: day });
  try {
    const res = await fetch("/quality/fed/day/" + day);
    if (!res.ok) throw new Error(await fedServerError(res));
    const d = await res.json();
    $("fed-day-title").textContent = "Rohwerte am " + fedDate(d.date) + " (Front " + (d.front_session ? fedDate(d.front_session) : "–")
      + ", Zweit " + (d.second_session ? fedDate(d.second_session) : "–") + ")";
    const rows = [];
    for (const s of d.sessions) {
      for (const [src, v] of Object.entries(s.sources)) {
        const head = document.createElement("tr");
        head.className = "fed-day-sum" + (v.rejected ? " qual-flag" : "");
        head.append(qualCell(fedDate(s.session_date)), qualCell(s.chain ? FED_CHAIN_NAMES[s.chain] : "–"), qualCell(FED_SOURCES[src]),
          qualCell("Erwartet " + fedBp(v.expected_bp) + " bp" + (v.rejected ? " (verworfen)" : "")), qualCell(""),
          qualCell("Σ " + fmtNum(v.raw_sum, 3), "num"), qualCell("100 %", "num"), qualCell(""), qualCell(""),
          qualCell(s.combined_bp == null ? "" : "kombiniert " + fedBp(s.combined_bp) + " bp"));
        rows.push(head);
        for (const o of v.outcomes) {
          const tr = document.createElement("tr");
          if (!o.used) tr.className = "qual-flag";
          tr.append(qualCell(""), qualCell(""), qualCell(""), qualCell(o.label), qualCell((o.bp > 0 ? "+" : "") + o.bp, "num"),
            qualCell(fmtNum(o.price, 3), "num"), qualCell(o.probability == null ? "–" : (o.probability * 100).toFixed(1).replace(".", ",") + " %", "num"),
            qualCell(o.volume_usd == null ? "–" : fmtNum(o.volume_usd), "num"), qualCell(o.observed_at.replace("T", " ").slice(0, 16)),
            qualCell((o.origin === "live" ? "live" : "historisch") + (o.used ? "" : " · nach Entscheidung")));
          rows.push(tr);
        }
      }
    }
    $("fed-day-rows").replaceChildren(...rows);
    $("fed-day").classList.remove("hidden");
  } catch (err) {
    logEvent("error", "fed.day.failed", { date: day, message: err.message });
    $("fed-status").textContent = "Rohwerte für " + day + " nicht verfügbar: " + err.message;
  }
}

async function loadFedChains(reason) {
  $("fed-status").textContent = "Berechne Fed-Zinsketten …";
  logEvent("info", "fed.load", { reason });
  try {
    const [cov, chains, checks] = await Promise.all(["coverage", "chains", "checks"].map(async (path) => {
      const res = await fetch("/quality/fed/" + path);
      if (!res.ok) throw new Error(path + ": " + await fedServerError(res));
      return res.json();
    }));
    fedChains = chains;
    renderFedChart();
    renderFedCoverage(cov);
    $("fed-checks").replaceChildren(...checks.map(qualCheckRow));
    fedLoaded = true;
    $("fed-status").textContent = cov.rows.length
      ? cov.sessions.length + " Sitzungen, Daten " + fedDate(cov.first_date) + " – " + fedDate(cov.last_date) + ". "
        + checks.filter((c) => c.status !== "ok").length + " Prüfung(en) mit Befund."
      : "Noch keine Fed-Rohdaten gespeichert.";
    logEvent("info", "fed.loaded", { sessions: cov.sessions.length, checks: Object.fromEntries(checks.map((c) => [c.key, c.count])) });
  } catch (err) {
    $("fed-status").textContent = "Fed-Zinsketten fehlgeschlagen: " + err.message;
    logEvent("error", "fed.failed", { message: err.message });
  }
}

async function runFedRawLoad() {
  const btn = $("fed-load");
  btn.disabled = true;
  $("fed-status").textContent = "Lade Fed-Märkte beider Quellen nach (kann einige Minuten dauern) …";
  logEvent("info", "fed.raw_load.click", {});
  try {
    const res = await fetch("/fed/load", { method: "POST" });
    if (!res.ok) throw new Error(await fedServerError(res));
    const s = await res.json();
    logEvent("info", "fed.raw_load.done", { listed: s.markets_listed, rows_added: s.rows_added, failed: s.markets_failed });
    await loadFedChains("raw_load");
    $("fed-status").textContent = s.markets_listed + " Märkte geprüft, " + s.markets_new + " neu, " + s.rows_added + " Tageswerte ergänzt, "
      + s.markets_failed + " fehlgeschlagen. " + $("fed-status").textContent;
  } catch (err) {
    $("fed-status").textContent = "Nachladen fehlgeschlagen: " + err.message;
    logEvent("error", "fed.raw_load.failed", { message: err.message });
  } finally {
    btn.disabled = false;
  }
}

$("fed-series").addEventListener("change", () => { logEvent("info", "fed.series", { series: fedSeries() }); renderFedChart(); });
$("fed-refresh").addEventListener("click", () => loadFedChains("button"));
$("fed-load").addEventListener("click", runFedRawLoad);
document.querySelector('.tab[data-view="quality"]').addEventListener("click", () => { if (!fedLoaded) loadFedChains("tab"); });
if (!document.querySelector('[data-view-panel="quality"]').classList.contains("hidden")) loadFedChains("start");
