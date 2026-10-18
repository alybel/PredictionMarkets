// Tab "KI-Agent", mode switch and mode "Metriken finden" (POST /agent/find): catalog metrics for an idea with reason,
// trading days, checks and status; blocked metrics and short histories stay visible with warnings. Gaps go with one click
// to mode "Metriken anlegen", which lives in tab "Risiko-Themen" below the review list (creator.js proposals and agent.js chat). "Vormerken" stores a bookmark for the index builder (POST/DELETE /agent/bookmarks).
// Uses $, fmtNum, logEvent (shell.js) and agEl, agDate (agent.js). Every request, mode switch and click goes to app.log.

const AF_STATUS = { draft: "Entwurf", validated: "freigegeben", blocked: "gesperrt" };
const AF_BADGE = { draft: "info", validated: "ok", blocked: "bad" };
const af = { idea: "", gaps: [], busy: false };

function afSetMode(mode, how) {
  if (mode === "create") { // 1017-1107 (4/7): proposals only from the accepted markets of a topic -> tab "Risiko-Themen"
    if (how) logEvent("info", "agent.mode", { mode, how, to: "risk" });
    document.querySelector('.tab[data-view="risk"]').click();
    $("ag-create-panel").scrollIntoView({ block: "start" });
    return;
  }
  for (const btn of document.querySelectorAll("[data-ag-mode]")) {
    btn.classList.toggle("active", btn.dataset.agMode === mode);
    btn.setAttribute("aria-selected", btn.dataset.agMode === mode);
  }
  $("af-panel").classList.remove("hidden");
  if (how) logEvent("info", "agent.mode", { mode, how });
}

function afChecks(m) {
  if (!m.checks.length) return "noch nicht geprüft";
  const failed = m.checks.filter((c) => c.status !== "ok").map((c) => c.label);
  return failed.length ? "rot: " + failed.join(", ") : "alle " + m.checks.length + " grün";
}

function afBookmarkButton(m) {
  const btn = agEl("button", { type: "button", className: m.bookmarked ? "ghost" : "", textContent: m.bookmarked ? "★ Vorgemerkt" : "Vormerken" });
  btn.addEventListener("click", () => afToggleBookmark(m, btn));
  return btn;
}

function afRow(m) {
  const status = agEl("span", { className: "badge " + (AF_BADGE[m.status] || "info"), textContent: AF_STATUS[m.status] || m.status });
  const warn = m.warnings.length ? agEl("ul", { className: "ag-list af-warn" }, ...m.warnings.map((w) => agEl("li", { textContent: "⚠ " + w }))) : "–";
  return agEl("tr", { className: (m.warnings.length ? "af-flagged" : "") + (m.blocked ? " af-blocked" : "") },
    agEl("td", { className: "cat-title", textContent: m.name, title: m.key }), agEl("td", null, status),
    agEl("td", { className: "num", textContent: fmtNum(m.history_days) }),
    agEl("td", { textContent: m.first_date ? agDate(m.first_date) + " – " + agDate(m.last_date) : "keine Historie" }),
    agEl("td", { textContent: afChecks(m) }), agEl("td", { textContent: m.reason || "–" }), agEl("td", null, warn),
    agEl("td", null, afBookmarkButton(m)));
}

function afRender(body) {
  $("af-reply").textContent = body.reply || "";
  $("af-rows").replaceChildren(...body.matches.map(afRow));
  if (!body.matches.length) $("af-rows").replaceChildren(agEl("tr", null, agEl("td", { colSpan: 8, textContent: "Keine passende Metrik im Katalog." })));
  $("af-meta").textContent = fmtNum(body.matches.length) + " Treffer aus " + fmtNum(body.catalog_size) + " Katalog-Metriken · ausreichende Historie ab " +
    fmtNum(body.min_history_days) + " Handelstagen" + (body.dropped.length ? " · " + body.dropped.length + " unbekannte Metrik(en) des Agenten verworfen" : "");
  af.gaps = body.gaps;
  $("af-gaps").replaceChildren(...(body.gaps.length ? body.gaps : ["Alle Aspekte sind durch vorhandene Metriken abgedeckt."]).map((g) => agEl("li", { textContent: g })));
  $("af-handover").disabled = !body.gaps.length;
  $("af-model").textContent = "Modell: " + body.model;
  $("af-result").classList.remove("hidden");
}

async function afSearch() {
  const idea = $("af-input").value.trim();
  if (!idea || af.busy) return;
  af.busy = true;
  $("af-send").disabled = true;
  $("af-status").textContent = "Der Agent durchsucht den Metrik-Katalog …";
  logEvent("info", "agent.find.send", { chars: idea.length });
  try {
    const res = await fetch("/agent/find", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idea }) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "HTTP " + res.status);
    af.idea = idea;
    afRender(body);
    $("af-status").textContent = "";
    logEvent("info", "agent.find.reply", { model: body.model, matches: body.matches.length, gaps: body.gaps.length, dropped: body.dropped,
      flagged: body.matches.filter((m) => m.warnings.length).map((m) => m.key) });
  } catch (err) {
    $("af-status").textContent = "Fehler: " + err.message;
    logEvent("error", "agent.find.failed", { message: err.message });
  } finally {
    af.busy = false;
    $("af-send").disabled = false;
  }
}

async function afToggleBookmark(m, btn) {
  const add = !m.bookmarked;
  btn.disabled = true;
  logEvent("info", "agent.bookmark.click", { key: m.key, add, status: m.status });
  try {
    const res = add ? await fetch("/agent/bookmarks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: m.key }) })
      : await fetch("/agent/bookmarks/" + encodeURIComponent(m.key), { method: "DELETE" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "HTTP " + res.status);
    m.bookmarked = add;
    btn.replaceWith(afBookmarkButton(m));
    $("af-status").textContent = add ? "„" + m.name + "“ vorgemerkt" + (m.status === "validated" ? " – im Index-Builder oben auswählbar." :
      " – im Index-Builder auswählbar, sobald sie freigegeben ist.") : "Vormerkung für „" + m.name + "“ entfernt.";
  } catch (err) {
    btn.disabled = false;
    $("af-status").textContent = "Fehler beim Vormerken: " + err.message;
    logEvent("error", "agent.bookmark.failed", { key: m.key, message: err.message });
  }
}

function afHandover() {
  if (!af.gaps.length) return;
  mcHandover(af.idea, af.gaps); // creator.js: proposals from the accepted markets of the chosen topic
  $("ag-input").value = "Lege Metriken für Aspekte an, die der Katalog noch nicht abdeckt.\nIdee: " + af.idea + "\nNicht abgedeckt:\n" +
    af.gaps.map((g) => "- " + g).join("\n"); // blueprint chat below keeps receiving the gaps (1016-2013-ki-agent-schritt-1)
  logEvent("info", "agent.find.handover", { gaps: af.gaps });
  afSetMode("create", "handover");
  $("mc-input").focus();
}

for (const btn of document.querySelectorAll("[data-ag-mode]")) btn.addEventListener("click", () => afSetMode(btn.dataset.agMode, "click"));
$("af-send").addEventListener("click", afSearch);
$("af-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) afSearch(); });
$("af-handover").addEventListener("click", afHandover);
afSetMode("find");
