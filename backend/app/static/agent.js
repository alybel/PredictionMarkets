// Blueprint chat of mode "Metriken anlegen" (tab "Risiko-Themen", below the review list; POST /agent/blueprint). Each turn
// sends the selected topic (only its accepted markets are used, 1017-1107 4/7), the transcript and the current blueprint and shows the updated blueprint (components with history, harmonization rules, caveats) plus the detected
// chains. "Als Metrik-Entwurf anlegen" registers all markets via POST /agent/metric. Uses $, fmtNum and logEvent.

const agState = { messages: [], blueprint: null, busy: false };

const agDate = (d) => (d ? new Date(d + "T00:00:00").toLocaleDateString("de-DE") : "–");
const agPeriod = (a, b) => (a ? agDate(a) + " – " + agDate(b) : "keine Historie");

function agEl(tag, props, ...children) {
  const el = Object.assign(document.createElement(tag), props || {});
  el.append(...children);
  return el;
}

// Topic selected in tab "Risiko-Themen" (risk.js loads after this file).
function agTopic() { return typeof rkState !== "undefined" ? rkState.topic : null; }

function agRenderChat() {
  $("ag-chat").replaceChildren(...agState.messages.map((m) => agEl("div", { className: "ag-msg " + m.role, textContent: m.content })));
  $("ag-chat").scrollTop = $("ag-chat").scrollHeight;
}

function agComponentRow(c) {
  const kind = c.kind === "chain" ? "Kette (" + c.link_count + " Glieder, " + c.rolls + " Rolls)" : "Markt";
  const gaps = c.gaps.length ? agEl("span", { className: "ag-gap", textContent: " · " + c.gaps.length + " Lücke(n)" }) : "";
  return agEl("tr", null, agEl("td", { className: "cat-title", textContent: c.label }), agEl("td", { textContent: kind }),
    agEl("td", { textContent: c.sources.join(", ") }), agEl("td", null, agPeriod(c.first_date, c.last_date), gaps),
    agEl("td", { className: "num", textContent: fmtNum(c.history_days) }),
    agEl("td", { textContent: c.direction === "down" ? "fallend (1 − p)" : "steigend" }),
    agEl("td", { className: "num", textContent: fmtNum(c.weight * 100, 1) + " %" }), agEl("td", { textContent: c.reason || "–" }));
}

function agKpi(label, value) {
  return agEl("div", { className: "kpi" }, label, agEl("strong", { textContent: value }));
}

function agChain(ch) {
  const head = ch.label + " · " + ch.link_count + " Glieder · " + (ch.rolls ? ch.rolls + " Rolls" : "parallel, noch kein Roll") + " · " + agPeriod(ch.first_date, ch.last_date) + " · " + fmtNum(ch.history_days) + " Tage";
  const rows = ch.links.map((l) => agEl("tr", null, agEl("td", { className: "cat-title", textContent: l.title }), agEl("td", { textContent: l.source }),
    agEl("td", { textContent: agPeriod(l.first_date, l.last_date) }), agEl("td", { textContent: agDate(l.roll_on) })));
  const table = agEl("table", null, agEl("thead", null, agEl("tr", null, ...["Glied", "Quelle", "Gespeichert", "Roll am"].map((h) => agEl("th", { textContent: h })))),
    agEl("tbody", null, ...rows));
  const details = agEl("details", { className: "ag-chain" }, agEl("summary", { textContent: head }), agEl("div", { className: "table-scroll" }, table));
  details.addEventListener("toggle", () => { if (details.open) logEvent("info", "agent.chain.open", { key: ch.key, links: ch.link_count }); });
  return details;
}

function agRender(body) {
  const bp = body.blueprint;
  $("ag-name").textContent = bp.name || "Index-Entwurf";
  $("ag-thesis").textContent = bp.thesis || "";
  const h = bp.history;
  $("ag-kpis").replaceChildren(agKpi("Bausteine", fmtNum(bp.components.length)), agKpi("davon Ketten", fmtNum(h.chained)),
    agKpi("Historie ab", agDate(h.start)), agKpi("alle Bausteine ab", agDate(h.common_start)), agKpi("bis", agDate(h.end)));
  $("ag-components").replaceChildren(...bp.components.map(agComponentRow));
  $("ag-rules").replaceChildren(...bp.harmonization.map((r) => agEl("li", null, agEl("strong", { textContent: r.topic + ": " }), r.rule)));
  $("ag-caveats").replaceChildren(...(bp.caveats.length ? bp.caveats : ["keine"]).map((c) => agEl("li", { textContent: c })));
  $("ag-chain-meta").textContent = fmtNum(body.chain_total) + " Ketten aus " + fmtNum(body.candidates) + " angenommenen Märkten · Suchbegriffe: " + body.terms.join(", ");
  const hc = body.history_check;
  if (hc && hc.checked) $("ag-chain-meta").textContent += " · Historie bei der Quelle geprüft: " + fmtNum(hc.checked) + " Märkte, " +
    fmtNum(hc.extended) + " mit längerem Verlauf, " + fmtNum(hc.rows_added) + " Tageswerte nachgeladen";
  $("ag-chains").replaceChildren(...body.chains.map(agChain));
  $("ag-metric").disabled = !bp.components.length;
  $("ag-model").textContent = "Modell: " + body.model;
  $("ag-result").classList.remove("hidden");
}

async function agSend() {
  const text = $("ag-input").value.trim();
  if (!text || agState.busy) return;
  const topic = agTopic();
  if (!topic) {
    $("ag-status").textContent = "Bitte oben zuerst ein Thema wählen.";
    logEvent("warning", "agent.send.no_topic");
    return;
  }
  agState.busy = true;
  $("ag-send").disabled = true;
  agState.messages.push({ role: "user", content: text });
  agRenderChat();
  $("ag-input").value = "";
  $("ag-status").textContent = "Der Agent nutzt die angenommenen Märkte von „" + topic.name + "“, sucht Ketten und Historie …";
  logEvent("info", "agent.send", { topic: topic.id, turn: agState.messages.length, chars: text.length });
  try {
    const res = await fetch("/agent/blueprint", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic_id: topic.id, messages: agState.messages, blueprint: agState.blueprint }) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "HTTP " + res.status);
    agState.blueprint = body.blueprint;
    agState.messages.push({ role: "assistant", content: body.reply || "(keine Antwort)" });
    agRenderChat();
    agRender(body);
    $("ag-status").textContent = "";
    logEvent("info", "agent.reply", { model: body.model, components: body.blueprint.components.length, chains: body.chain_total,
      history_rows: body.history_check ? body.history_check.rows_added : 0 });
  } catch (err) {
    agState.messages.pop();
    agRenderChat();
    $("ag-input").value = text;
    $("ag-status").textContent = "Fehler: " + err.message;
    logEvent("error", "agent.failed", { message: err.message });
  } finally {
    agState.busy = false;
    $("ag-send").disabled = false;
  }
}

async function agCreateMetric() {
  const topic = agTopic();
  if (!agState.blueprint || !topic) return;
  $("ag-metric").disabled = true;
  logEvent("info", "agent.metric.create", { topic: topic.id, name: agState.blueprint.name, components: agState.blueprint.components.length });
  try {
    const res = await fetch("/agent/metric", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic_id: topic.id, blueprint: agState.blueprint }) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "HTTP " + res.status);
    $("ag-metric-status").textContent = "Metrik-Entwurf " + body.key + " mit " + body.markets + " Märkten angelegt (Status " + body.status + ", Tab „Metriken“).";
    logEvent("info", "agent.metric.created", { key: body.key, markets: body.markets });
  } catch (err) {
    $("ag-metric-status").textContent = "Fehler: " + err.message;
    $("ag-metric").disabled = false;
    logEvent("error", "agent.metric.failed", { message: err.message });
  }
}

function agReset() {
  Object.assign(agState, { messages: [], blueprint: null });
  agRenderChat();
  $("ag-result").classList.add("hidden");
  $("ag-status").textContent = $("ag-metric-status").textContent = $("ag-model").textContent = "";
  logEvent("info", "agent.reset");
}

$("ag-send").addEventListener("click", agSend);
$("ag-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) agSend(); });
$("ag-metric").addEventListener("click", agCreateMetric);
$("ag-reset").addEventListener("click", agReset);
