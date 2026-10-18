// Tab "Risiko-Themen": topic with risk question, classification of directory markets (Haiku or marked fallback),
// review list (accept / reject / change reading) and timeline over at least three years (GET/POST/PATCH /risk/*).
// Uses $, fmtNum and logEvent; renders its panel itself and loads on first view. Below it the mode "Metriken anlegen"
// (creator.js, agent.js) works on the accepted markets of the selected topic (rkState.topic).

const rkState = { loaded: false, topics: [], topic: null, readings: {}, offset: 0, limit: 100 };
const RK_STATUS = { proposed: "vorgeschlagen", accepted: "angenommen", rejected: "abgelehnt" };
const rkDate = (d) => (d ? new Date(d).toLocaleDateString("de-DE") : "–");

function rkEl(tag, props, ...children) {
  const el = Object.assign(document.createElement(tag), props || {});
  el.append(...children);
  return el;
}

async function rkFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = "HTTP " + res.status;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

function rkStatus(text) { $("rk-status").textContent = text; }

$("rk-panel").innerHTML = `
  <div class="card"><div class="card-head"><div><h2>Risiko-Themen</h2>
    <p class="hint">Ein Thema hat eine Risiko-Frage und Suchbegriffe. Märkte aus dem Marktverzeichnis werden vorgefiltert und je Thema
      genau einmal von der KI eingestuft (relevant, Begründung, Lesart); neue Märkte aus dem Eingangskorb stuft der tägliche Lauf ein.</p></div>
    <div class="controls"><select id="rk-topic" aria-label="Thema"></select><button id="rk-classify" type="button">Jetzt einstufen</button></div></div>
    <p id="rk-question" class="hint"></p><p id="rk-terms" class="hint"></p>
    <p id="rk-status" class="hint" role="status" aria-live="polite"></p></div>
  <div class="card"><div class="card-head"><div><h2 id="rk-list-title">Prüfliste</h2>
    <p class="hint">Annehmen oder ablehnen und bei Bedarf die Lesart ändern – nur angenommene Märkte gehen weiter.</p></div>
    <div class="controls"><select id="rk-filter" aria-label="Status"><option value="">alle</option><option value="proposed">vorgeschlagen</option>
      <option value="accepted">angenommen</option><option value="rejected">abgelehnt</option></select>
      <label class="field"><input id="rk-only-relevant" type="checkbox" checked> nur relevante</label></div></div>
    <div class="table-scroll"><table><thead><tr><th>Quelle</th><th>Titel</th><th>Start</th><th>Ende</th><th>Relevant</th><th>Begründung</th>
      <th>Lesart</th><th>Status</th><th></th></tr></thead><tbody id="rk-rows"></tbody></table></div>
    <div class="controls"><button id="rk-prev" type="button" class="ghost">‹ zurück</button><span id="rk-page" class="hint"></span>
      <button id="rk-next" type="button" class="ghost">weiter ›</button></div></div>
  <div class="card"><div class="card-head"><div><h2>Zeitleiste</h2>
    <p class="hint">Relevante, nicht abgelehnte Märkte je Monat – ein Markt zählt erst ab seinem Startdatum. Mindestens 3 Jahre.</p></div></div>
    <p id="rk-note" class="rk-note hidden" role="note"></p><div id="rk-bars" class="rk-bars" role="img" aria-label="Relevante Märkte je Monat"></div>
    <p id="rk-range" class="hint"></p></div>`;

// ---- topics --------------------------------------------------------------------------------

function rkShowTopic() {
  const t = rkState.topic;
  $("rk-question").textContent = t ? "Risiko-Frage: " + t.question : "";
  $("rk-terms").textContent = t ? "Suchbegriffe: " + (t.search_terms.join(", ") || "–") + " · Tags: " + (t.tags.join(", ") || "–") : "";
}

async function rkLoadTopics() {
  rkState.topics = await rkFetch("/risk/topics");
  $("rk-topic").replaceChildren(...rkState.topics.map((t) => rkEl("option", { value: t.id, textContent: t.name })));
  rkState.topic = rkState.topics.find((t) => String(t.id) === $("rk-topic").value) || rkState.topics[0] || null;
  rkShowTopic();
}

async function rkClassify(button) {
  const t = rkState.topic;
  if (!t) return;
  logEvent("info", "risk.classify.click", { topic: t.id });
  button.disabled = true;
  rkStatus("Stufe Märkte für „" + t.name + "“ ein …");
  try {
    const r = await rkFetch("/risk/topics/" + t.id + "/classify", { method: "POST" });
    rkStatus((r.fallback ? "FALLBACK ohne KI-Schlüssel (Stichwort-Regel) · " : "Modell " + r.model + " · ") + fmtNum(r.candidates) +
      " Kandidaten, " + fmtNum(r.already) + " bereits eingestuft, " + fmtNum(r.stored) + " neu eingestuft, " + fmtNum(r.discarded) +
      " Antworten verworfen" + (r.remaining ? ", " + fmtNum(r.remaining) + " folgen beim nächsten Lauf" : "") + ".");
    logEvent("info", "risk.classify.done", r);
    await rkRefreshTopic();
  } catch (err) {
    rkStatus("Einstufung fehlgeschlagen: " + err.message);
    logEvent("error", "risk.classify.failed", { message: err.message });
  } finally { button.disabled = false; }
}

// ---- review list ---------------------------------------------------------------------------

async function rkReview(a, change) {
  logEvent("info", "risk.review.click", { assessment: a.id, market: a.market_id, ...change });
  try {
    await rkFetch("/risk/assessments/" + a.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(change) });
    rkStatus("Gespeichert: " + a.title.slice(0, 80) + " – " + (change.status ? RK_STATUS[change.status] : "Lesart " + rkState.readings[change.reading]));
    await Promise.all([rkLoadList(), rkLoadTimeline()]);
  } catch (err) {
    rkStatus("Speichern fehlgeschlagen: " + err.message);
    logEvent("error", "risk.review.failed", { assessment: a.id, message: err.message });
  }
}

function rkRow(a) {
  const reading = rkEl("select", { "aria-label": "Lesart" }, ...Object.entries(rkState.readings).map(([k, label]) =>
    rkEl("option", { value: k, textContent: label + (k === a.proposed_reading ? " (KI-Vorschlag)" : ""), selected: k === a.reading })));
  reading.addEventListener("change", () => rkReview(a, { reading: reading.value }));
  const accept = rkEl("button", { type: "button", textContent: "Annehmen", disabled: a.status === "accepted" });
  const reject = rkEl("button", { type: "button", className: "ghost", textContent: "Ablehnen", disabled: a.status === "rejected" });
  accept.addEventListener("click", () => rkReview(a, { status: "accepted" }));
  reject.addEventListener("click", () => rkReview(a, { status: "rejected" }));
  const badge = rkEl("span", { className: "badge " + ({ accepted: "ok", rejected: "bad" }[a.status] || "info"), textContent: RK_STATUS[a.status] });
  return rkEl("tr", null, rkEl("td", { textContent: a.source }), rkEl("td", { textContent: a.title }), rkEl("td", { textContent: rkDate(a.listed_at) }),
    rkEl("td", { textContent: rkDate(a.end_date) }), rkEl("td", { textContent: a.relevant ? "ja" : "nein" }),
    rkEl("td", { textContent: a.rationale + (a.fallback ? " [Fallback]" : "") }), rkEl("td", null, reading), rkEl("td", null, badge),
    rkEl("td", null, accept, reject));
}

async function rkLoadList() {
  const t = rkState.topic;
  if (!t) return;
  const p = new URLSearchParams({ status: $("rk-filter").value, limit: rkState.limit, offset: rkState.offset });
  if ($("rk-only-relevant").checked) p.set("relevant", "true");
  const res = await rkFetch("/risk/topics/" + t.id + "/assessments?" + p);
  rkState.readings = res.readings;
  const c = res.counts;
  $("rk-list-title").textContent = `Prüfliste – ${fmtNum(c.proposed)} vorgeschlagen, ${fmtNum(c.accepted)} angenommen, ${fmtNum(c.rejected)} abgelehnt`;
  $("rk-rows").replaceChildren(...(res.assessments.length ? res.assessments.map(rkRow)
    : [rkEl("tr", null, rkEl("td", { colSpan: 9, className: "muted", textContent: "Keine eingestuften Märkte – „Jetzt einstufen“ startet die Einstufung." }))]));
  const last = Math.min(res.offset + res.limit, res.total);
  $("rk-page").textContent = res.total ? (res.offset + 1) + "–" + last + " von " + fmtNum(res.total) : "";
  $("rk-prev").disabled = res.offset === 0;
  $("rk-next").disabled = last >= res.total;
}

// ---- timeline ------------------------------------------------------------------------------

async function rkLoadTimeline() {
  const t = rkState.topic;
  if (!t) return;
  const tl = await rkFetch("/risk/topics/" + t.id + "/timeline");
  $("rk-note").textContent = tl.note || "";
  $("rk-note").classList.toggle("hidden", !tl.note);
  const max = Math.max(1, ...tl.months.map((m) => m.count));
  $("rk-bars").replaceChildren(...tl.months.map((m) => rkEl("div", { className: "rk-bar", title: m.month + ": " + m.count + " Märkte" },
    rkEl("span", { style: "height:" + (m.count / max * 100) + "%" }))));
  $("rk-range").textContent = rkDate(tl.window_start) + " – " + rkDate(tl.window_end) + " · " + fmtNum(tl.total_markets) + " relevante Märkte" +
    (tl.history_start ? " · Historie ab " + rkDate(tl.history_start) + " (" + tl.history_years + " Jahre)" : "") + " · höchstens " + max + " Märkte gleichzeitig";
}

async function rkRefreshTopic() { await Promise.all([rkLoadList(), rkLoadTimeline()]); }

async function rkRefresh(reason) {
  logEvent("info", "risk.view.load", { reason });
  rkState.loaded = true;
  try {
    await rkLoadTopics();
    await rkRefreshTopic();
  } catch (err) {
    rkStatus("Risiko-Themen konnten nicht geladen werden: " + err.message);
    logEvent("error", "risk.view.failed", { message: err.message });
  }
}

function rkReloadList(event, detail) {
  logEvent("info", event, detail);
  rkLoadList().catch((err) => rkStatus("Laden fehlgeschlagen: " + err.message));
}

$("rk-classify").addEventListener("click", (e) => rkClassify(e.currentTarget));
$("rk-topic").addEventListener("change", () => {
  rkState.topic = rkState.topics.find((t) => String(t.id) === $("rk-topic").value) || null;
  rkState.offset = 0;
  logEvent("info", "risk.topic.select", { topic: $("rk-topic").value });
  rkShowTopic();
  mcReset(); // proposals and blueprint belong to one topic (creator.js, agent.js)
  agReset();
  rkRefreshTopic().catch((err) => rkStatus(err.message));
});
$("rk-filter").addEventListener("change", () => { rkState.offset = 0; rkReloadList("risk.filter.status", { status: $("rk-filter").value }); });
$("rk-only-relevant").addEventListener("change", () => { rkState.offset = 0; rkReloadList("risk.filter.relevant", { only: $("rk-only-relevant").checked }); });
$("rk-prev").addEventListener("click", () => { rkState.offset = Math.max(0, rkState.offset - rkState.limit); rkReloadList("risk.page", { offset: rkState.offset }); });
$("rk-next").addEventListener("click", () => { rkState.offset += rkState.limit; rkReloadList("risk.page", { offset: rkState.offset }); });
document.querySelector('.tab[data-view="risk"]').addEventListener("click", () => { if (!rkState.loaded) rkRefresh("tab"); });
if (!$("rk-view").classList.contains("hidden")) rkRefresh("start");
