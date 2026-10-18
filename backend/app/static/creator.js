// Mode "Metriken anlegen" (tab "Risiko-Themen", below the review list; POST /agent/propose): metric proposals only from the
// markets accepted in the selected topic's review list (1017-1107, 4/7), single market or chain, each link with its reading
// from the review list (changed only there); rules only from the fixed catalog, free-text rules shown as "Hinweis, nicht
// automatisch umgesetzt". Each proposal shows trading days, gaps and liquidity (warning below 180 trading days); references
// the agent proposed outside the accepted markets are listed with their reason. "Als Metrik-Entwurf übernehmen" stores it
// structured (POST /agent/proposals/accept, status draft, checks run). Uses $, fmtNum, logEvent (shell.js), agEl, agDate,
// agKpi, agTopic (agent.js).

const mc = { busy: false };

// Step 1 (finder.js) hands over its idea and the uncovered aspects as editable text.
function mcHandover(idea, gaps) {
  $("mc-input").value = idea + "\nNicht abgedeckt:\n" + gaps.map((g) => "- " + g).join("\n");
}

function mcKpis(a, min) {
  const gaps = a.gap_days ? fmtNum(a.gap_days) + " Tage in " + a.gaps.length + (a.gaps.length >= 10 ? "+" : "") + " Lücke(n)" : "keine";
  return agEl("div", { className: "kpis" }, agKpi("Handelstage", fmtNum(a.trading_days)),
    agKpi("Zeitraum", a.first_date ? agDate(a.first_date) + " – " + agDate(a.last_date) : "keine Historie"), agKpi("Lücken", gaps),
    agKpi("Liquidität (Ø 24h)", a.liquidity_usd == null ? "unbekannt" : fmtNum(a.liquidity_usd) + " $"), agKpi("Rolls", fmtNum(a.rolls)),
    agKpi("Mindestens", fmtNum(min) + " Tage"));
}

function mcLinks(p) {
  const rows = p.links.map((l) => agEl("tr", null, agEl("td", { className: "cat-title", textContent: l.title }), agEl("td", { textContent: l.source }),
    agEl("td", { textContent: agDate(l.roll_on) }), agEl("td", { textContent: l.reading_label })));
  const head = agEl("tr", null, ...["Glied", "Quelle", "Roll am", "Lesart (Prüfliste)"].map((h) => agEl("th", { textContent: h })));
  return agEl("div", { className: "table-scroll" }, agEl("table", null, agEl("thead", null, head), agEl("tbody", null, ...rows)));
}

function mcProposal(p, min) {
  const a = p.assessment;
  const badge = agEl("span", { className: "badge " + (a.short_history ? "bad" : "ok"), textContent: a.short_history ? "kurze Historie" : "Historie ausreichend" });
  const title = agEl("h3", null, p.name + " ", badge);
  const kind = agEl("p", { className: "hint", textContent: (p.kind === "chain" ? "Kette aus " + p.links.length + " Gliedern" : "Einzelmarkt") +
    " · Thema: " + p.topic + " · Lesart: " + p.reading_label + (p.reason ? " · " + p.reason : "") });
  const warn = a.warnings.length ? agEl("ul", { className: "ag-list af-warn" }, ...a.warnings.map((w) => agEl("li", { textContent: "⚠ " + w }))) : "";
  const rules = agEl("ul", { className: "ag-list" }, ...p.rule_labels.map((r) => agEl("li", { textContent: "✓ " + r })));
  const notes = p.notes.length ? agEl("div", null, agEl("h4", { textContent: "Hinweis, nicht automatisch umgesetzt" }),
    agEl("ul", { className: "ag-list mc-notes" }, ...p.notes.map((n) => agEl("li", { textContent: n })))) : "";
  const btn = agEl("button", { type: "button", textContent: "Als Metrik-Entwurf übernehmen" });
  const status = agEl("p", { className: "hint", role: "status" });
  btn.addEventListener("click", () => mcAccept(p, btn, status));
  const links = mcLinks(p);
  return agEl("div", { className: "mc-proposal" + (a.short_history ? " af-flagged" : "") }, title, kind, mcKpis(a, min), warn,
    links,
    agEl("h4", { textContent: "Ausgeführte Regeln" }), rules, notes, agEl("div", { className: "controls" }, btn), status);
}

function mcReset() {
  $("mc-result").classList.add("hidden");
  $("mc-proposals").replaceChildren();
  $("mc-status").textContent = $("mc-model").textContent = "";
}

function mcRender(body) {
  $("mc-reply").textContent = body.reply || "";
  $("mc-proposals").replaceChildren(...body.proposals.map((p) => mcProposal(p, body.min_history_days)));
  $("mc-meta").textContent = fmtNum(body.proposals.length) + " Vorschläge aus " + fmtNum(body.pool.markets) + " angenommenen Märkten und " +
    fmtNum(body.pool.chains) + " Ketten von „" + body.topic.name + "“" + (body.dropped.length ? " · " + body.dropped.length +
    " Referenz(en) außerhalb der Prüfliste verworfen: " + body.dropped.map((d) => d.ref + " (" + d.reason + ")").join(", ") : "");
  $("mc-model").textContent = "Modell: " + body.model;
  $("mc-result").classList.remove("hidden");
}

async function mcSend() {
  const idea = $("mc-input").value.trim();
  if (!idea || mc.busy) return;
  const topic = agTopic();
  if (!topic) {
    $("mc-status").textContent = "Bitte oben zuerst ein Thema wählen.";
    logEvent("warning", "agent.propose.no_topic");
    return;
  }
  mc.busy = true;
  $("mc-send").disabled = true;
  $("mc-status").textContent = "Der Agent nutzt die angenommenen Märkte von „" + topic.name + "“ und prüft Historie und Liquidität …";
  logEvent("info", "agent.propose.send", { topic: topic.id, chars: idea.length });
  try {
    const res = await fetch("/agent/propose", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic_id: topic.id, idea }) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "HTTP " + res.status);
    mcRender(body);
    $("mc-status").textContent = "";
    logEvent("info", "agent.propose.reply", { model: body.model, topic: body.topic.id, proposals: body.proposals.length,
      dropped: body.dropped, short: body.proposals.filter((p) => p.assessment.short_history).map((p) => p.name) });
  } catch (err) {
    $("mc-status").textContent = "Fehler: " + err.message;
    logEvent("error", "agent.propose.failed", { message: err.message });
  } finally {
    mc.busy = false;
    $("mc-send").disabled = false;
  }
}

async function mcAccept(p, btn, status) {
  btn.disabled = true;
  logEvent("info", "agent.propose.accept.click", { topic: p.topic_id, name: p.name, kind: p.kind, readings: p.links.map((l) => l.direction), links: p.links.length, rules: p.rules, notes: p.notes.length });
  try {
    const res = await fetch("/agent/proposals/accept", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ proposal: p }) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "HTTP " + res.status);
    const red = body.checks.filter((c) => c.status !== "ok").map((c) => c.label);
    status.textContent = "Metrik-Entwurf " + body.key + " angelegt (Status " + body.status + "; Prüfungen " + (red.length ? "rot: " + red.join(", ") : "grün") +
      "). Freigabe im Tab „Metriken“.";
    logEvent("info", "agent.propose.accepted", { key: body.key, status: body.status, failing: red });
  } catch (err) {
    status.textContent = "Fehler: " + err.message;
    btn.disabled = false;
    logEvent("error", "agent.propose.accept.failed", { name: p.name, message: err.message });
  }
}

$("mc-send").addEventListener("click", mcSend);
$("mc-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) mcSend(); });
