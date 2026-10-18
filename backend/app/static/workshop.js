// Tab "Risiko-Index-Werkstatt" (risk index 7/7): guided steps 1-5 for one risk topic (pilot "Marktcrash-Risiko" preselected).
// Status, open tasks and AI hint per step come from GET /workshop/topics/{id}/status; every action uses the endpoints of
// tickets 1-6 (directory, risk, agent, metrics, builder, forecast); step 1 asks the raw data layer (/raw/requests) for fresh data.
// An AI proposal only takes effect after a human click.
// Uses $, fmtNum, logEvent (shell.js, indices.js), fcTarget (forecast.js) and rkState/rkRefresh (risk.js). Clicks and step changes -> app.log.

const ws = { loaded: false, topics: [], topic: null, status: null, step: 1, proposals: [], weighting: null, rawTimer: null };
const WS_STATUS = { open: ["offen", "muted"], in_progress: ["in Arbeit", "info"], done: ["erledigt", "ok"] };
const WS_JSON = { "Content-Type": "application/json" };

function wsEl(tag, props, ...children) {
  const el = Object.assign(document.createElement(tag), props || {});
  wsAdd(el, ...children);
  return el;
}

const wsAdd = (el, ...children) => el.append(...children.filter((c) => c != null));

async function wsFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = "HTTP " + res.status;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

const wsOpenTab = (view) => document.querySelector('.tab[data-view="' + view + '"]').click(); // also starts that tab's loader
const wsPost = (url, body) => wsFetch(url, { method: "POST", headers: WS_JSON, body: JSON.stringify(body ?? {}) });
function wsMsg(text) { $("ws-status").textContent = text; }

function wsButton(text, id, handler, ghost) {
  const b = wsEl("button", { type: "button", className: ghost ? "ghost" : "", textContent: text });
  b.addEventListener("click", async () => {
    logEvent("info", "workshop.click", { id, topic: ws.topic?.id, step: ws.step });
    b.disabled = true;
    try { await handler(); } catch (err) {
      wsMsg("Fehlgeschlagen: " + err.message);
      logEvent("error", "workshop.action.failed", { id, topic: ws.topic?.id, message: err.message });
    } finally { b.disabled = false; }
  });
  return b;
}

$("ws-panel").innerHTML = `
  <div class="card"><div class="card-head"><div><h2>Risiko-Index-Werkstatt</h2>
    <p class="hint">Führt ein Risiko-Thema in fünf Schritten von den Rohdaten bis zum Prognosekraft-Bericht. Die KI macht Vorschläge –
      wirksam wird jeder Vorschlag erst, wenn du ihn bestätigst. Alle bisherigen Tabs bleiben unverändert nutzbar.</p></div>
    <div class="controls"><select id="ws-topic" aria-label="Thema"></select><button id="ws-reload" type="button" class="ghost">Status neu laden</button></div></div>
    <p id="ws-question" class="hint"></p>
    <details><summary>Weiteres Thema anlegen</summary><div class="ws-form">
      <label class="field"><span>Name</span><input id="ws-new-name" type="text" maxlength="128" placeholder="z. B. Rezessions-Risiko"></label>
      <label class="field"><span>Risiko-Frage</span><input id="ws-new-question" type="text" maxlength="512"></label>
      <label class="field"><span>Suchbegriffe (Komma)</span><input id="ws-new-terms" type="text" maxlength="1000"></label>
      <label class="field"><span>Tags (Komma)</span><input id="ws-new-tags" type="text" maxlength="500"></label>
      <div class="controls"><button id="ws-new-save" type="button">Thema anlegen</button></div></div></details></div>
  <nav id="ws-steps" class="ws-steps" aria-label="Schritte"></nav>
  <div class="card"><div class="card-head"><div><h2 id="ws-step-title"></h2><p id="ws-step-ai" class="hint"></p></div>
    <div class="controls"><button id="ws-prev" type="button" class="ghost">‹ Schritt zurück</button><button id="ws-next" type="button" class="ghost">Nächster Schritt ›</button></div></div>
    <div id="ws-tasks"></div><div id="ws-body"></div><p id="ws-status" class="hint" role="status" aria-live="polite"></p></div>`;

// ---- topics and status ---------------------------------------------------------------------

async function wsLoadTopics(selectId) {
  ws.topics = await wsFetch("/risk/topics");
  $("ws-topic").replaceChildren(...ws.topics.map((t) => wsEl("option", { value: t.id, textContent: t.name })));
  const pilot = ws.topics.find((t) => t.slug === "marktcrash");
  ws.topic = ws.topics.find((t) => t.id === selectId) || ws.topic && ws.topics.find((t) => t.id === ws.topic.id) || pilot || ws.topics[0] || null;
  if (ws.topic) $("ws-topic").value = ws.topic.id;
  $("ws-question").textContent = ws.topic ? "Risiko-Frage: " + ws.topic.question : "";
}

async function wsLoadStatus() {
  if (!ws.topic) return;
  ws.status = await wsFetch("/workshop/topics/" + ws.topic.id + "/status");
  if (typeof rkState !== "undefined") rkState.loaded = false; // tab "Risiko-Themen" reloads on its next view
  logEvent("info", "workshop.status", { topic: ws.topic.id, steps: Object.fromEntries(ws.status.steps.map((s) => [s.step, s.status])) });
  wsRenderSteps();
  await wsRenderStep();
}

function wsRenderSteps() {
  $("ws-steps").replaceChildren(...ws.status.steps.map((s) => {
    const [label, cls] = WS_STATUS[s.status];
    const open = s.tasks.reduce((n, t) => n + t.count, 0);
    const b = wsEl("button", { type: "button", className: "ws-step" + (s.step === ws.step ? " active" : ""), disabled: !s.ready && s.step !== ws.step },
      wsEl("span", { className: "ws-num", textContent: s.step }), wsEl("strong", { textContent: s.title }),
      wsEl("span", { className: "badge " + cls, textContent: label }), open ? wsEl("span", { className: "hint", textContent: fmtNum(open) + " offen" }) : null);
    b.title = s.ready ? "" : "Noch gesperrt – erst den vorherigen Schritt bearbeiten";
    b.addEventListener("click", () => wsGo(s.step, "stepper"));
    return b;
  }));
}

function wsGo(step, via) {
  if (step < 1 || step > 5 || step === ws.step) return;
  logEvent("info", "workshop.step", { topic: ws.topic?.id, from: ws.step, to: step, via });
  ws.step = step;
  wsMsg("");
  wsRenderSteps();
  wsRenderStep().catch((err) => wsMsg(err.message));
}

async function wsRenderStep() {
  const s = ws.status.steps[ws.step - 1];
  $("ws-step-title").textContent = "Schritt " + s.step + " – " + s.title + " · " + WS_STATUS[s.status][0];
  $("ws-step-ai").textContent = "KI-Vorschlag: " + s.ai;
  $("ws-tasks").replaceChildren(s.tasks.length ? wsEl("ul", { className: "ag-list" }, ...s.tasks.map((t) => wsEl("li", { textContent: fmtNum(t.count) + " " + t.label })))
    : wsEl("p", { className: "hint", textContent: "Keine offenen Aufgaben." }));
  $("ws-prev").disabled = ws.step === 1;
  $("ws-next").disabled = ws.step === 5 || !ws.status.steps[ws.step].ready;
  const body = $("ws-body");
  body.replaceChildren();
  if (!s.ready) { body.append(wsEl("p", { className: "hint", textContent: "Dieser Schritt braucht zuerst das Ergebnis des vorherigen." })); return; }
  await [wsRaw, wsReview, wsMetrics, wsWeighting, wsForecast][s.step - 1](s, body);
}

const wsAfter = async (text) => { await wsLoadStatus(); wsMsg(text); };

// ---- step 1: raw data -------------------------------------------------------------------------

async function wsRaw(s, body) {
  const c = s.counts, box = await wsFetch("/directory/inbox");
  wsAdd(body, wsEl("p", { textContent: fmtNum(c.markets) + " Märkte im Verzeichnis, davon " + fmtNum(c.candidates) + " Kandidaten für das Thema · letzter Ladelauf " +
      (c.last_run ? new Date(c.last_run).toLocaleString("de-DE") : "noch keiner") }),
    wsEl("div", { className: "controls" },
      wsButton("Rohdaten aktualisieren", "ws-raw-refresh", wsRawRefresh),
      box.count ? wsButton("Alle " + fmtNum(box.count) + " neuen Märkte als gesehen markieren", "ws-inbox-reviewed", async () => {
        const r = await wsPost("/directory/inbox/reviewed", {}); await wsAfter(fmtNum(r.marked) + " Märkte als gesehen markiert."); }) : null,
      wsButton("Zum Marktverzeichnis", "ws-goto-directory", async () => wsOpenTab("directory"), true)),
    wsEl("p", { id: "ws-raw-progress", className: "hint", role: "status" }),
    box.count ? wsEl("ul", { className: "ag-list" }, ...box.markets.slice(0, 15).map((m) => wsEl("li", { textContent: m.source + " · " + m.title }))) : null);
  const last = await wsFetch("/raw/requests/latest?job=directory");
  if (last?.running) wsRawWatch(last); // a fetch started elsewhere: show its progress
}

// The directory only reads raw data: the button just asks the raw data layer (POST /raw/requests, returns at once);
// the layer fetches in the background, stores raw_responses and rebuilds the directory. Progress is polled here.
async function wsRawRefresh() {
  const job = await wsPost("/raw/requests", { job: "directory", trigger: "workshop" });
  logEvent("info", "workshop.raw.requested", { job: job.id, duplicate: job.duplicate });
  wsMsg(job.duplicate ? "Ein Abruf läuft bereits (Auftrag #" + job.id + ") – es wird kein zweiter gestartet." : "Aktualisierung angefragt (Auftrag #" + job.id + ").");
  wsRawWatch(job);
}

function wsRawWatch(job) {
  if (ws.rawTimer) clearTimeout(ws.rawTimer);
  const show = (j) => {
    const p = j.progress || {}, el = $("ws-raw-progress");
    if (el) el.textContent = "Rohdaten-Abruf #" + j.id + ": " + (p.label || j.status) + (p.total ? " (" + p.done + "/" + p.total + ")" : "");
  };
  const poll = async () => {
    try {
      const j = await wsFetch("/raw/requests/" + job.id);
      show(j);
      if (j.running) { ws.rawTimer = setTimeout(poll, 2000); return; }
      ws.rawTimer = null;
      const built = ((j.summary || {}).after || {}).directory || {}, src = built.sources || {};
      if (j.status !== "done" || built.error) {
        logEvent("error", "workshop.raw.failed", { job: j.id, error: j.error || built.error });
        wsMsg("Rohdaten-Abruf #" + j.id + " fehlgeschlagen: " + (j.error || built.error));
        return;
      }
      logEvent("info", "workshop.raw.done", { job: j.id, polymarket: src.polymarket?.new, kalshi: src.kalshi?.new });
      await wsAfter("Rohdaten aktualisiert, Verzeichnis neu aufgebaut: Polymarket " + fmtNum(src.polymarket?.new || 0) + " neu, Kalshi " +
        fmtNum(src.kalshi?.new || 0) + " neu.");
    } catch (err) {
      ws.rawTimer = null;
      wsMsg("Fortschritt nicht abrufbar: " + err.message);
      logEvent("error", "workshop.raw.poll.failed", { job: job.id, message: err.message });
    }
  };
  show(job);
  poll();
}

// ---- step 2: review list ----------------------------------------------------------------------

async function wsReview(s, body) {
  const t = ws.topic, c = s.counts;
  const res = await wsFetch("/risk/topics/" + t.id + "/assessments?status=proposed&relevant=true&limit=25");
  const row = (a) => {
    const decide = (status) => wsButton(status === "accepted" ? "Annehmen" : "Ablehnen", "ws-review-" + status, async () => {
      await wsFetch("/risk/assessments/" + a.id, { method: "PATCH", headers: WS_JSON, body: JSON.stringify({ status }) });
      logEvent("info", "workshop.review", { topic: t.id, assessment: a.id, market: a.market_id, status, reading: a.reading });
      await wsAfter((status === "accepted" ? "Angenommen: " : "Abgelehnt: ") + a.title.slice(0, 80));
    }, status !== "accepted");
    return wsEl("tr", null, wsEl("td", { textContent: a.source }), wsEl("td", { textContent: a.title }),
      wsEl("td", { textContent: a.rationale + (a.fallback ? " [Fallback]" : "") }), wsEl("td", { textContent: res.readings[a.reading] || a.reading }),
      wsEl("td", null, decide("accepted"), decide("rejected")));
  };
  wsAdd(body, wsEl("p", { textContent: fmtNum(c.accepted) + " angenommen · " + fmtNum(c.rejected) + " abgelehnt · " + fmtNum(c.unreviewed) +
      " relevante ungeprüft · " + fmtNum(c.unclassified) + " noch nicht eingestuft" }),
    wsEl("div", { className: "controls" },
      wsButton("KI-Einordnung starten", "ws-classify", async () => {
        wsMsg("Stufe Märkte ein …");
        const r = await wsPost("/risk/topics/" + t.id + "/classify");
        await wsAfter((r.fallback ? "FALLBACK ohne KI-Schlüssel · " : "Modell " + r.model + " · ") + fmtNum(r.stored) + " neu eingestuft, " +
          fmtNum(r.discarded) + " Antworten verworfen – bitte prüfen.");
      }),
      wsButton("Ganze Prüfliste & Zeitleiste öffnen", "ws-goto-risk", async () => {
        if (!rkState.loaded) await rkRefresh("workshop");
        if ($("rk-topic").value !== String(t.id)) { $("rk-topic").value = t.id; $("rk-topic").dispatchEvent(new Event("change")); }
        wsOpenTab("risk");
      }, true)),
    res.assessments.length ? wsEl("div", { className: "table-scroll" }, wsEl("table", null,
      wsEl("thead", null, wsEl("tr", null, ...["Quelle", "Titel", "Begründung (KI)", "Lesart (KI)", ""].map((h) => wsEl("th", { textContent: h })))),
      wsEl("tbody", null, ...res.assessments.map(row)))) : wsEl("p", { className: "hint", textContent: "Keine ungeprüften relevanten Märkte." }));
}

// ---- step 3: chain metrics --------------------------------------------------------------------

function wsProposal(p) {
  const a = p.assessment || {};
  const take = wsButton("Als Metrik-Entwurf übernehmen", "ws-proposal-accept", async () => {
    const r = await wsPost("/agent/proposals/accept", { proposal: p });
    logEvent("info", "workshop.proposal.accepted", { topic: ws.topic.id, name: p.name, key: r.key, status: r.status });
    ws.proposals = ws.proposals.filter((x) => x !== p);
    await wsAfter("Entwurf " + r.key + " angelegt (Status " + r.status + ") – jetzt prüfen und freigeben.");
  });
  return wsEl("div", { className: "ws-item" }, wsEl("strong", { textContent: p.name + " (" + (p.kind === "chain" ? "Kette" : "Einzelmarkt") + ")" }),
    wsEl("p", { className: "hint", textContent: p.links.map((l) => l.title || l.external_id).join(" → ") + " · Lesart " + (p.reading_label || p.direction) }),
    wsEl("p", { className: "hint", textContent: fmtNum(a.trading_days) + " Handelstage" + (a.warnings?.length ? " · ⚠ " + a.warnings.join("; ") : "") }),
    wsEl("div", { className: "controls" }, take, wsButton("Verwerfen", "ws-proposal-reject", async () => {
      logEvent("info", "workshop.proposal.rejected", { topic: ws.topic.id, name: p.name });
      ws.proposals = ws.proposals.filter((x) => x !== p);
      await wsRenderStep();
    }, true)));
}

async function wsMetrics(s, body) {
  const t = ws.topic;
  const approve = (m) => wsButton("Freigeben", "ws-metric-approve", async () => {
    const r = await wsPost("/metrics/" + encodeURIComponent(m.key) + "/approve");
    logEvent("info", "workshop.metric.approved", { topic: t.id, key: m.key, status: r.status });
    await wsAfter("Metrik „" + m.name + "“ freigegeben.");
  });
  wsAdd(body, wsEl("div", { className: "controls" },
      wsButton("KI-Vorschlag holen", "ws-propose", async () => {
        wsMsg("Der Agent stellt Vorschläge aus den angenommenen Märkten zusammen …");
        const r = await wsPost("/agent/propose", { topic_id: t.id, idea: t.question });
        ws.proposals = r.proposals;
        logEvent("info", "workshop.proposals", { topic: t.id, model: r.model, names: r.proposals.map((p) => p.name), dropped: r.dropped.length });
        await wsRenderStep();
        wsMsg(r.reply + " (" + r.model + ")");
      }),
      wsButton("Zum Metrik-Katalog", "ws-goto-metrics", async () => wsOpenTab("metrics"), true)),
    ...ws.proposals.map(wsProposal),
    wsEl("h3", { textContent: "Metriken des Themas" }),
    s.counts.list.length ? wsEl("ul", { className: "ag-list" }, ...s.counts.list.map((m) => wsEl("li", null, m.name + " · " + m.key + " · ",
      wsEl("span", { className: "badge " + ({ validated: "ok", blocked: "bad" }[m.status] || "info"), textContent: m.status }), " ",
      m.status === "draft" ? approve(m) : null)))
      : wsEl("p", { className: "hint", textContent: "Noch keine Metrik – hol dir einen KI-Vorschlag und übernimm ihn." }));
}

// ---- step 4: weighting ------------------------------------------------------------------------

async function wsSaveIndex(name, keys, method, weights, proposal) {
  const exists = ws.status.steps[3].counts.indices.some((i) => i.name === name);
  const body = { components: keys.map((k) => ({ key: k, weight: method === "manual" ? (weights[k] || 1) : 1 })), weighting: method, proposal };
  await wsFetch(exists ? "/builder/indices/" + encodeURIComponent(name) : "/builder/indices",
    { method: exists ? "PUT" : "POST", headers: WS_JSON, body: JSON.stringify(exists ? body : { name, ...body }) });
  logEvent("info", "workshop.index.saved", { topic: ws.topic.id, name, keys, method, proposal: proposal || null, updated: exists });
  ws.weighting = null;
  await wsAfter("Index „" + name + "“ gespeichert – der Prognosekraft-Bericht wurde dabei gerechnet.");
}

async function wsWeighting(s, body) {
  const validated = ws.status.steps[2].counts.list.filter((m) => m.status === "validated");
  const sel = await wsFetch("/builder/metrics");
  const picks = validated.map((m) => wsEl("label", { className: "field" }, wsEl("input", { type: "checkbox", checked: true, value: m.key }), " " + m.name));
  const keys = () => picks.map((l) => l.firstChild).filter((i) => i.checked).map((i) => i.value);
  const name = wsEl("input", { type: "text", maxLength: 128, value: s.counts.indices[0]?.name || ws.topic.name + " Index" });
  const method = wsEl("select", { "aria-label": "Verfahren" }, ...Object.entries(sel.weighting_methods).map(([v, l]) => wsEl("option", { value: v, textContent: l })));
  method.value = "equal";
  method.addEventListener("change", () => logEvent("info", "workshop.weighting.method", { topic: ws.topic.id, method: method.value }));
  const p = ws.weighting;
  wsAdd(body, wsEl("label", { className: "field" }, wsEl("span", { textContent: "Name des Index" }), name), wsEl("div", { className: "ws-form" }, ...picks),
    wsEl("div", { className: "controls" },
      wsButton("KI-Vorschlag Gewichtung", "ws-weighting-propose", async () => {
        if (!keys().length) throw new Error("Bitte mindestens eine Metrik wählen.");
        ws.weighting = { ...(await wsPost("/builder/weighting/propose", { keys: keys() })), keys: keys() };
        logEvent("info", "workshop.weighting.proposal", { topic: ws.topic.id, method: ws.weighting.method, weights: ws.weighting.weights_pct, model: ws.weighting.model });
        await wsRenderStep();
      }),
      method, wsButton("Ohne KI speichern", "ws-index-save", () => wsSaveIndex(name.value.trim(), keys(), method.value, {}, null), true)),
    p ? wsEl("div", { className: "ws-item" }, wsEl("strong", { textContent: "KI-Vorschlag: " + p.method_label + " (" + p.model + ")" }),
      wsEl("p", { className: "hint", textContent: Object.entries(p.weights_pct).map(([k, v]) => k + " " + String(v).replace(".", ",") + " %").join(" · ") }),
      wsEl("p", { className: "hint", textContent: p.rationale }),
      wsEl("p", { className: "hint", textContent: "Noch nicht wirksam – erst „Übernehmen & Index speichern“ legt den Index an." }),
      wsEl("div", { className: "controls" },
        wsButton("Übernehmen & Index speichern", "ws-weighting-confirm", () => wsSaveIndex(name.value.trim(), p.keys, p.method, p.weights_pct, "confirmed")),
        wsButton("Verwerfen", "ws-weighting-reject", async () => {
          logEvent("info", "workshop.weighting.proposal.rejected", { topic: ws.topic.id, method: p.method });
          ws.weighting = null; await wsRenderStep();
        }, true))) : null,
    s.counts.indices.length ? wsEl("p", { className: "hint", textContent: "Indizes des Themas: " +
      s.counts.indices.map((i) => i.name + " (" + i.weighting_label + ", " + i.components.length + " Metriken)").join(" · ") }) : null);
}

// ---- step 5: forecast power -------------------------------------------------------------------

async function wsForecast(s, body) {
  for (const ix of ws.status.steps[3].counts.indices) {
    const url = "/builder/indices/" + encodeURIComponent(ix.name) + "/forecast";
    const report = await wsFetch(url);
    logEvent("info", "workshop.forecast.loaded", { topic: ws.topic.id, index: ix.name, lights: Object.fromEntries(report.targets.map((t) => [t.ticker, t.light])) });
    wsAdd(body, wsEl("h3", { textContent: "Prognosekraft – " + ix.name }),
      wsEl("p", { className: "hint", textContent: "Stichtag " + report.as_of + " · " + report.index_days + " Indextage" }),
      ...report.warnings.map((w) => wsEl("p", { className: "hint", textContent: "⚠ " + w })),
      wsEl("div", { className: "fc-targets" }, ...report.targets.map(fcTarget)),
      wsEl("div", { className: "controls" }, wsButton("Neu berechnen", "ws-forecast-recompute", async () => {
        await wsFetch(url, { method: "POST" }); await wsAfter("Bericht für „" + ix.name + "“ neu berechnet.");
      }, true)));
  }
}

// ---- wiring -----------------------------------------------------------------------------------

async function wsRefresh(reason) {
  logEvent("info", "workshop.view.load", { reason });
  ws.loaded = true;
  try {
    await wsLoadTopics();
    await wsLoadStatus();
  } catch (err) {
    wsMsg("Werkstatt konnte nicht geladen werden: " + err.message);
    logEvent("error", "workshop.view.failed", { message: err.message });
  }
}

$("ws-topic").addEventListener("change", () => {
  ws.topic = ws.topics.find((t) => String(t.id) === $("ws-topic").value) || null;
  ws.proposals = []; ws.weighting = null; ws.step = 1;
  logEvent("info", "workshop.topic.select", { topic: ws.topic?.id });
  $("ws-question").textContent = ws.topic ? "Risiko-Frage: " + ws.topic.question : "";
  wsLoadStatus().catch((err) => wsMsg(err.message));
});
$("ws-new-save").addEventListener("click", async () => {
  const split = (id) => $(id).value.split(",").map((x) => x.trim()).filter(Boolean);
  const topic = { name: $("ws-new-name").value.trim(), question: $("ws-new-question").value.trim(), search_terms: split("ws-new-terms"), tags: split("ws-new-tags") };
  logEvent("info", "workshop.topic.create.click", { name: topic.name });
  try {
    const t = await wsPost("/risk/topics", topic);
    ws.step = 1;
    await wsLoadTopics(t.id);
    await wsLoadStatus();
    wsMsg("Thema „" + t.name + "“ angelegt.");
  } catch (err) {
    wsMsg("Thema nicht angelegt: " + err.message);
    logEvent("error", "workshop.topic.create.failed", { message: err.message });
  }
});
$("ws-reload").addEventListener("click", () => { logEvent("info", "workshop.click", { id: "ws-reload" }); wsLoadStatus().catch((err) => wsMsg(err.message)); });
$("ws-prev").addEventListener("click", () => wsGo(ws.step - 1, "prev"));
$("ws-next").addEventListener("click", () => wsGo(ws.step + 1, "next"));
document.querySelector('.tab[data-view="workshop"]').addEventListener("click", () => { if (!ws.loaded) wsRefresh("tab"); else wsLoadStatus().catch((err) => wsMsg(err.message)); });
if (!$("ws-view").classList.contains("hidden")) wsRefresh("start");
