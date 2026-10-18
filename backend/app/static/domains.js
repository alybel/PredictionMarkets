// Tab "Domänen": create, change, delete and load domains (GET/POST/PUT/DELETE /domains, POST /domains/{id}/load).
// Topic and subtopic choices come from GET /topics. The table shows members (active/expired), history reach, gaps
// and the last load run. Uses $, fmtNum and logEvent; loads on first view.

const dmState = { loaded: false, topics: [], editId: null, armedDelete: null };

const dmDate = (d) => (d ? new Date(d.length === 10 ? d + "T00:00:00" : d).toLocaleDateString("de-DE") : "–");

function dmEl(tag, props, ...children) {
  const el = Object.assign(document.createElement(tag), props || {});
  el.append(...children);
  return el;
}

async function dmFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = "HTTP " + res.status;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

function dmStatus(text) { $("dm-status").textContent = text; }

// ---- form ----------------------------------------------------------------------------------

function dmFillSubtopics(selected) {
  const topic = dmState.topics.find((t) => t.name === $("dm-topic").value);
  const subs = topic ? topic.subtopics.map((s) => s.name) : [];
  $("dm-subtopic").replaceChildren(dmEl("option", { value: "", textContent: "– ganzes Thema –" }),
    ...subs.map((s) => dmEl("option", { value: s, textContent: s })));
  $("dm-subtopic").value = selected && subs.includes(selected) ? selected : "";
}

function dmFillTopics(selected, subtopic) {
  const names = dmState.topics.map((t) => t.name);
  if (selected && !names.includes(selected)) names.unshift(selected); // keep the stored topic selectable while editing
  $("dm-topic").replaceChildren(...names.map((n) => dmEl("option", { value: n, textContent: n })));
  if (selected) $("dm-topic").value = selected;
  dmFillSubtopics(subtopic);
}

function dmResetForm() {
  dmState.editId = dmState.prefill = null;
  $("dm-name").value = ""; $("dm-terms").value = "";
  dmFillTopics();
  $("dm-form-title").textContent = "Domäne anlegen";
  $("dm-save").textContent = "Domäne anlegen";
  $("dm-cancel").classList.add("hidden");
}

// Domain suggestion of the KI-Agent (creator.js): prefill the form, the user saves it.
function dmPrefill(s) {
  dmResetForm();
  dmState.prefill = s;
  $("dm-name").value = s.name || ""; $("dm-terms").value = (s.terms || []).join(", ");
  dmFillTopics(s.topic || undefined, s.subtopic || undefined);
  dmStatus("Vorschlag des KI-Agenten übernommen – bitte prüfen und „Domäne anlegen“ klicken.");
  logEvent("info", "domains.prefill", { name: s.name, topic: s.topic, subtopic: s.subtopic });
}

function dmEdit(d) {
  logEvent("info", "domains.edit", { id: d.id });
  dmState.editId = d.id;
  $("dm-name").value = d.name; $("dm-terms").value = d.terms.join(", ");
  dmFillTopics(d.topic, d.subtopic);
  $("dm-form-title").textContent = "Domäne ändern: " + d.name;
  $("dm-save").textContent = "Änderungen speichern";
  $("dm-cancel").classList.remove("hidden");
  $("dm-name").focus();
}

async function dmSave() {
  const body = { name: $("dm-name").value, topic: $("dm-topic").value, subtopic: $("dm-subtopic").value || null,
    terms: $("dm-terms").value.split(",").map((t) => t.trim()).filter(Boolean) };
  const id = dmState.editId;
  logEvent("info", id ? "domains.update" : "domains.create", { id, ...body });
  try {
    await dmFetch(id ? "/domains/" + id : "/domains", { method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    dmStatus(id ? "Domäne gespeichert." : "Domäne angelegt. Mit „Jetzt laden“ wird die Historie geholt, sonst beim täglichen Lauf.");
    dmResetForm();
    await dmLoadList();
  } catch (err) {
    dmStatus("Speichern fehlgeschlagen: " + err.message);
    logEvent("warning", "domains.save.failed", { id, message: err.message });
  }
}

// ---- table ---------------------------------------------------------------------------------

function dmRunText(d) {
  if (!d.last_run_at) return "noch nie";
  const r = d.last_run || {};
  const errors = Object.values(r.sources || {}).reduce((n, s) => n + (s.errors || 0), 0);
  return dmDate(d.last_run_at) + " · " + fmtNum(r.new_markets || 0) + " neu · " + fmtNum(r.rows_added || 0) + " Zeilen" + (errors ? " · " + errors + " Fehler" : "");
}

async function dmLoadRun(d, button) {
  logEvent("info", "domains.load", { id: d.id });
  button.disabled = true;
  dmStatus("Lade Märkte und Historie für „" + d.name + "“ … (kann einige Minuten dauern)");
  try {
    const res = await dmFetch("/domains/" + d.id + "/load", { method: "POST" });
    dmStatus("„" + d.name + "“: " + res.run.new_markets + " neue Märkte, " + res.run.loaded + " mit Historie geladen, " + res.run.rows_added + " Tageszeilen ergänzt.");
    logEvent("info", "domains.load.done", { id: d.id, rows: res.run.rows_added, loaded: res.run.loaded });
    await dmLoadList();
  } catch (err) {
    dmStatus("Ladelauf fehlgeschlagen: " + err.message);
    logEvent("error", "domains.load.failed", { id: d.id, message: err.message });
  } finally { button.disabled = false; }
}

async function dmDelete(d, button) {
  if (dmState.armedDelete !== d.id) { // two-step delete instead of a blocking dialog
    dmState.armedDelete = d.id;
    button.textContent = "Wirklich löschen?";
    logEvent("info", "domains.delete.arm", { id: d.id });
    setTimeout(() => { if (dmState.armedDelete === d.id) { dmState.armedDelete = null; button.textContent = "Löschen"; } }, 4000);
    return;
  }
  dmState.armedDelete = null;
  logEvent("info", "domains.delete", { id: d.id });
  try {
    await dmFetch("/domains/" + d.id, { method: "DELETE" });
    dmStatus("Domäne „" + d.name + "“ gelöscht (gespeicherte Historie bleibt erhalten).");
    if (dmState.editId === d.id) dmResetForm();
    await dmLoadList();
  } catch (err) {
    dmStatus("Löschen fehlgeschlagen: " + err.message);
    logEvent("error", "domains.delete.failed", { id: d.id, message: err.message });
  }
}

function dmRow(d) {
  const edit = dmEl("button", { type: "button", className: "ghost", textContent: "Ändern" });
  const load = dmEl("button", { type: "button", className: "ghost", textContent: "Jetzt laden" });
  const del = dmEl("button", { type: "button", className: "ghost", textContent: "Löschen" });
  edit.addEventListener("click", () => dmEdit(d));
  load.addEventListener("click", () => dmLoadRun(d, load));
  del.addEventListener("click", () => dmDelete(d, del));
  const gaps = d.gap_days ? fmtNum(d.gap_days) + " (" + d.markets_with_gaps + " Märkte)" : "0";
  const cells = [d.name, d.topic + " › " + (d.subtopic || "ganzes Thema"), d.terms.join(", ") || "–"].map((t) => dmEl("td", { textContent: t }));
  cells.push(...[fmtNum(d.markets_active), fmtNum(d.markets_expired)].map((t) => dmEl("td", { className: "num", textContent: t })),
    dmEl("td", { textContent: dmDate(d.first_date) }), dmEl("td", { textContent: dmDate(d.last_date) }),
    dmEl("td", { className: "num", textContent: gaps }), dmEl("td", { textContent: dmRunText(d) }),
    dmEl("td", { className: "dm-actions" }, edit, load, del));
  return dmEl("tr", null, ...cells);
}

async function dmLoadList() {
  const list = await dmFetch("/domains");
  $("dm-rows").replaceChildren(...(list.length ? list.map(dmRow) : [dmEl("tr", null, dmEl("td", { colSpan: 10, className: "muted", textContent: "Noch keine Domänen angelegt." }))]));
}

async function dmLoad(reason) {
  logEvent("info", "domains.view.load", { reason });
  dmState.loaded = true;
  try {
    dmState.topics = (await dmFetch("/topics")).topics;
    if (!dmState.editId) dmFillTopics(dmState.prefill && dmState.prefill.topic, dmState.prefill && dmState.prefill.subtopic);
    await dmLoadList();
    if (!dmState.topics.length) dmStatus("Keine Themen verfügbar – der Markt-Cache ist leer.");
  } catch (err) {
    dmStatus("Domänen konnten nicht geladen werden: " + err.message);
    logEvent("error", "domains.view.failed", { message: err.message });
  }
}

$("dm-topic").addEventListener("change", () => { logEvent("info", "domains.topic.select", { topic: $("dm-topic").value }); dmFillSubtopics(); });
$("dm-save").addEventListener("click", dmSave);
$("dm-cancel").addEventListener("click", () => { logEvent("info", "domains.edit.cancel", { id: dmState.editId }); dmResetForm(); dmStatus(""); });
$("dm-refresh").addEventListener("click", () => dmLoad("button"));
document.querySelector('.tab[data-view="domains"]').addEventListener("click", () => { if (!dmState.loaded) dmLoad("tab"); });
if (!document.querySelector('[data-view-panel="domains"]').classList.contains("hidden")) dmLoad("start");
