// Tab "Marktverzeichnis": every market of Kalshi and Polymarket (incl. expired/resolved), the inbox "Neu seit …",
// coverage per year and source and the last load run (GET /directory/*, POST /directory/load, POST /directory/inbox/reviewed).
// The list shows group markets by default: all markets of one market group (source event) as one row, members on demand.
// Uses $, fmtNum and logEvent; renders its panel itself and loads on first view.

const mdState = { loaded: false, offset: 0, limit: 50, view: "groups" };
const MD_STATUS = { active: "aktiv", expired: "abgelaufen", resolved: "aufgelöst" };
const MD_SOURCE = { polymarket: "Polymarket", kalshi: "Kalshi" };

const mdDate = (d) => (d ? new Date(d).toLocaleDateString("de-DE") : "–");

function mdEl(tag, props, ...children) {
  const el = Object.assign(document.createElement(tag), props || {});
  el.append(...children);
  return el;
}

async function mdFetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = "HTTP " + res.status;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

function mdStatus(text) { $("md-status").textContent = text; }

$("md-panel").innerHTML = `
  <div class="card"><div class="card-head"><div><h2>Marktverzeichnis</h2>
    <p class="hint">Alle Märkte von Kalshi und Polymarket, die die Schnittstellen heute liefern – auch abgelaufene und aufgelöste.
      Rohantworten laufen über Staging → Raw; neue Märkte erkennt der tägliche Lauf.</p></div>
    <div class="controls"><button id="md-load" type="button">Jetzt laden</button></div></div>
    <p id="md-run" class="hint"></p><p id="md-status" class="hint" role="status" aria-live="polite"></p></div>
  <div class="card"><div class="card-head"><div><h2 id="md-inbox-title">Neue Märkte</h2>
    <p class="hint">Markt bleibt hier, bis er als gesehen markiert ist. Der erste Lauf je Quelle gilt als Grundbestand.</p></div>
    <div class="controls"><label class="field"><span>Neu seit</span><input id="md-since" type="date"></label>
      <button id="md-seen-all" type="button" class="ghost">Alle als gesehen markieren</button></div></div>
    <div class="table-scroll"><table><thead><tr><th>Quelle</th><th>Titel</th><th>Tags</th><th>Gelistet</th><th>Ende</th><th>Status</th>
      <th>Erstmals gesehen</th><th></th></tr></thead><tbody id="md-inbox"></tbody></table></div></div>
  <div class="card"><div class="card-head"><div><h2>Abdeckung je Jahr und Quelle</h2>
    <p class="hint">Märkte, die im jeweiligen Jahr existierten (Listung bis Enddatum) – gemessen aus dem Verzeichnis, nicht von den Quellen versprochen.</p></div></div>
    <div class="table-scroll"><table><thead id="md-cov-head"></thead><tbody id="md-cov"></tbody></table></div></div>
  <div class="card"><div class="card-head"><div><h2>Verzeichnis</h2>
    <p class="hint">Gruppenmärkte: alle Märkte einer Markt-Gruppe (z.&nbsp;B. die vielen S&amp;P-500-Spannen eines Termins) als eine Zeile.</p></div>
    <div class="controls md-filter"><select id="md-view" aria-label="Ansicht"><option value="groups">Gruppenmärkte</option><option value="markets">Einzelmärkte</option></select>
      <input id="md-q" type="search" maxlength="100" placeholder="Titel oder ID">
      <select id="md-source"><option value="">alle Quellen</option><option value="polymarket">Polymarket</option><option value="kalshi">Kalshi</option></select>
      <select id="md-filter-status"><option value="">alle Status</option><option value="active">aktiv</option><option value="expired">abgelaufen</option><option value="resolved">aufgelöst</option></select>
      <button id="md-search" type="button" class="ghost">Suchen</button></div></div>
    <div class="table-scroll"><table><thead id="md-head"></thead><tbody id="md-rows"></tbody></table></div>
    <div class="controls"><button id="md-prev" type="button" class="ghost">‹ zurück</button><span id="md-page" class="hint"></span>
      <button id="md-next" type="button" class="ghost">weiter ›</button></div></div>`;

const mdCells = (m) => [MD_SOURCE[m.source] || m.source, m.title, m.tags.join(", ") || "–"].map((t) => mdEl("td", { textContent: t }));
const mdEmpty = (cols, text) => mdEl("tr", null, mdEl("td", { colSpan: cols, className: "muted", textContent: text }));

// ---- last run ------------------------------------------------------------------------------

function mdRunText(run) {
  if (!run) return "Noch kein Ladelauf – „Jetzt laden“ oder der tägliche Lauf füllt das Verzeichnis.";
  const parts = Object.entries((run.summary || {}).sources || {}).map(([s, r]) =>
    `${MD_SOURCE[s] || s}: ${fmtNum(r.found)} gefunden, ${fmtNum(r.new)} neu im Verzeichnis (${fmtNum(r.marked_new || 0)} im Eingangskorb), ` +
    `${fmtNum(r.skipped)} übersprungen, ${r.errors} Fehler`);
  return "Letzter Lauf " + mdDate(run.finished_at || run.started_at) + " (" + run.trigger + ") · " + (parts.join(" · ") || "läuft …");
}

async function mdLoadRun() { $("md-run").textContent = mdRunText(await mdFetch("/directory/runs/last")); }

async function mdRunNow(button) {
  logEvent("info", "directory.load.click", {});
  button.disabled = true;
  mdStatus("Lade alle Märkte beider Quellen … (kann mehrere Minuten dauern)");
  try {
    const res = await mdFetch("/directory/load", { method: "POST" });
    const s = res.sources;
    mdStatus("Fertig: Polymarket " + fmtNum(s.polymarket.new) + " neu, Kalshi " + fmtNum(s.kalshi.new) + " neu im Verzeichnis. " +
      "Je Lauf wird ein Stück der Historie nachgeladen; der nächste Lauf macht dort weiter.");
    logEvent("info", "directory.load.done", { run: res.run_id, polymarket: s.polymarket.new, kalshi: s.kalshi.new });
    await mdRefresh();
  } catch (err) {
    mdStatus("Ladelauf fehlgeschlagen: " + err.message);
    logEvent("error", "directory.load.failed", { message: err.message });
  } finally { button.disabled = false; }
}

// ---- inbox ---------------------------------------------------------------------------------

async function mdReview(ids, label) {
  logEvent("info", "directory.inbox.reviewed.click", { ids, label });
  try {
    const res = await mdFetch("/directory/inbox/reviewed", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }) });
    mdStatus(res.marked + " Markt/Märkte als gesehen markiert.");
    await mdLoadInbox();
  } catch (err) {
    mdStatus("Markieren fehlgeschlagen: " + err.message);
    logEvent("error", "directory.inbox.reviewed.failed", { message: err.message });
  }
}

function mdInboxRow(m) {
  const seen = mdEl("button", { type: "button", className: "ghost", textContent: "Gesehen" });
  seen.addEventListener("click", () => mdReview([m.id], m.source + "/" + m.external_id));
  return mdEl("tr", null, ...mdCells(m), mdEl("td", { textContent: mdDate(m.listed_at) }), mdEl("td", { textContent: mdDate(m.end_date) }),
    mdEl("td", { textContent: MD_STATUS[m.status] || m.status }), mdEl("td", { textContent: mdDate(m.first_seen_at) }), mdEl("td", null, seen));
}

async function mdLoadInbox() {
  const since = $("md-since").value;
  const res = await mdFetch("/directory/inbox" + (since ? "?since=" + since : ""));
  $("md-inbox-title").textContent = "Neue Märkte" + (res.since ? " – neu seit " + mdDate(res.since) : "") + " (" + fmtNum(res.count) + ")";
  $("md-inbox").replaceChildren(...(res.markets.length ? res.markets.map(mdInboxRow) : [mdEmpty(8, "Keine neuen Märkte.")]));
}

// ---- coverage ------------------------------------------------------------------------------

async function mdLoadCoverage() {
  const cov = await mdFetch("/directory/coverage");
  $("md-cov-head").replaceChildren(mdEl("tr", null, mdEl("th", { textContent: "Quelle" }),
    ...cov.years.map((y) => mdEl("th", { className: "num", textContent: y })), mdEl("th", { className: "num", textContent: "Märkte gesamt" }),
    mdEl("th", { textContent: "aktiv / abgelaufen / aufgelöst" }), mdEl("th", { className: "num", textContent: "ohne Startdatum" })));
  $("md-cov").replaceChildren(...Object.entries(cov.sources).map(([s, per]) => { const st = cov.statuses[s] || {}; return mdEl("tr", null,
    mdEl("td", { textContent: MD_SOURCE[s] || s }), ...cov.years.map((y) => mdEl("td", { className: "num", textContent: fmtNum(per[y] || 0) })),
    mdEl("td", { className: "num", textContent: fmtNum(cov.totals[s] || 0) }),
    mdEl("td", { textContent: [st.active, st.expired, st.resolved].map((n) => fmtNum(n || 0)).join(" / ") }),
    mdEl("td", { className: "num", textContent: fmtNum(cov.undated[s] || 0) })); }));
}

// ---- directory -----------------------------------------------------------------------------

const MD_HEADS = {
  markets: ["Quelle", "ID", "Titel", "Tags", "Existiert von", "bis", "Status", "Erstmals gesehen"],
  groups: ["Quelle", "Gruppenmarkt", "Märkte", "aktiv / abgelaufen / aufgelöst", "Existiert von", "bis", "", ""],
};

function mdMarketRow(m) {
  return mdEl("tr", null, mdEl("td", { textContent: MD_SOURCE[m.source] || m.source }), mdEl("td", { textContent: m.external_id }),
    ...mdCells(m).slice(1), mdEl("td", { textContent: mdDate(m.listed_at) }), mdEl("td", { textContent: mdDate(m.end_date) }),
    mdEl("td", { textContent: MD_STATUS[m.status] || m.status }), mdEl("td", { textContent: mdDate(m.first_seen_at) }));
}

// Opens or closes the member markets right below the group row.
async function mdToggleGroup(g, row, button) {
  const open = row.nextSibling && row.nextSibling.dataset && row.nextSibling.dataset.group === g.key;
  logEvent("info", "directory.group." + (open ? "close" : "open"), { source: g.source, group: g.key, markets: g.markets });
  if (open) { row.nextSibling.remove(); button.textContent = "Märkte zeigen"; return; }
  button.disabled = true;
  try {
    const p = new URLSearchParams({ source: g.source, group: g.key, status: $("md-filter-status").value, limit: 500 });
    const res = await mdFetch("/directory/markets?" + p);
    const inner = mdEl("table", { className: "md-members" }, mdEl("tbody", null, ...res.markets.map(mdMarketRow)));
    const more = res.total > res.markets.length ? mdEl("p", { className: "hint", textContent: "… und " + fmtNum(res.total - res.markets.length) + " weitere" }) : "";
    const box = mdEl("tr", null, mdEl("td", { colSpan: 8 }, inner, more));
    box.dataset.group = g.key;
    row.after(box);
    button.textContent = "Märkte ausblenden";
  } catch (err) {
    mdStatus("Märkte der Gruppe konnten nicht geladen werden: " + err.message);
    logEvent("error", "directory.group.failed", { group: g.key, message: err.message });
  } finally { button.disabled = false; }
}

function mdGroupRow(g) {
  const st = g.statuses;
  const show = mdEl("button", { type: "button", className: "ghost", textContent: "Märkte zeigen" });
  const row = mdEl("tr", null, mdEl("td", { textContent: MD_SOURCE[g.source] || g.source }),
    mdEl("td", { textContent: g.title, title: g.single ? "Einzelmarkt ohne Gruppe" : "Gruppe " + g.key }),
    mdEl("td", { className: "num", textContent: fmtNum(g.markets) }),
    mdEl("td", { textContent: [st.active, st.expired, st.resolved].map(fmtNum).join(" / ") }),
    mdEl("td", { textContent: mdDate(g.listed_at) }), mdEl("td", { textContent: mdDate(g.end_date) }), mdEl("td", null, show), mdEl("td"));
  show.addEventListener("click", () => mdToggleGroup(g, row, show));
  return row;
}

async function mdLoadList() {
  const groups = mdState.view === "groups";
  const p = new URLSearchParams({ q: $("md-q").value.trim(), source: $("md-source").value, status: $("md-filter-status").value,
    limit: mdState.limit, offset: mdState.offset });
  const res = await mdFetch((groups ? "/directory/groups?" : "/directory/markets?") + p);
  const items = groups ? res.groups : res.markets;
  $("md-head").replaceChildren(mdEl("tr", null, ...MD_HEADS[mdState.view].map((t) => mdEl("th", { textContent: t }))));
  $("md-rows").replaceChildren(...(items.length ? items.map(groups ? mdGroupRow : mdMarketRow) : [mdEmpty(8, "Keine Märkte gefunden.")]));
  const last = Math.min(res.offset + res.limit, res.total);
  $("md-page").textContent = res.total ? (res.offset + 1) + "–" + last + " von " + fmtNum(res.total) + (groups ? " Gruppenmärkten" : " Märkten") : "";
  $("md-prev").disabled = res.offset === 0;
  $("md-next").disabled = last >= res.total;
}

async function mdRefresh(reason) {
  if (reason) logEvent("info", "directory.view.load", { reason });
  mdState.loaded = true;
  try {
    await Promise.all([mdLoadRun(), mdLoadInbox(), mdLoadCoverage(), mdLoadList()]);
  } catch (err) {
    mdStatus("Marktverzeichnis konnte nicht geladen werden: " + err.message);
    logEvent("error", "directory.view.failed", { message: err.message });
  }
}

function mdSearch(event) {
  mdState.offset = 0;
  logEvent("info", "directory.search", { q: $("md-q").value, source: $("md-source").value, status: $("md-filter-status").value, view: mdState.view, via: event });
  mdLoadList().catch((err) => mdStatus("Suche fehlgeschlagen: " + err.message));
}

function mdPage(step) {
  mdState.offset = Math.max(0, mdState.offset + step * mdState.limit);
  logEvent("info", "directory.page", { offset: mdState.offset });
  mdLoadList().catch((err) => mdStatus("Blättern fehlgeschlagen: " + err.message));
}

$("md-load").addEventListener("click", (e) => mdRunNow(e.currentTarget));
$("md-seen-all").addEventListener("click", () => mdReview(null, "all"));
$("md-since").addEventListener("change", () => { logEvent("info", "directory.inbox.since", { since: $("md-since").value }); mdLoadInbox().catch((err) => mdStatus(err.message)); });
$("md-search").addEventListener("click", () => mdSearch("button"));
$("md-q").addEventListener("keydown", (e) => { if (e.key === "Enter") mdSearch("enter"); });
$("md-source").addEventListener("change", () => mdSearch("source"));
$("md-view").addEventListener("change", () => { mdState.view = $("md-view").value; mdSearch("view"); });
$("md-filter-status").addEventListener("change", () => mdSearch("status"));
$("md-prev").addEventListener("click", () => mdPage(-1));
$("md-next").addEventListener("click", () => mdPage(1));
document.querySelector('.tab[data-view="directory"]').addEventListener("click", () => { if (!mdState.loaded) mdRefresh("tab"); });
if (!$("md-panel").classList.contains("hidden")) mdRefresh("start");
