// Tab "Metriken", panel "Neue Metrik bauen" (1015-2351): name, description, market search over the catalog
// (GET /manufacture/catalog) and selection; button "+ Neue Metrik" in the catalog head opens the panel; "Metrik anlegen" posts to POST /metrics (status draft) and reloads the
// catalog. Selection (1016-0005): opening the panel lists the most liquid markets right away, source filter, "Mehr laden",
// toggle per row (Hinzufügen/Entfernen) kept in sync with the chips, "Auswahl leeren".
// Uses $, logEvent (shell.js), fedServerError (fedstaging.js) and loadMetrics (metrics.js).

const MN_LIMIT = 20;
const mnSelected = new Map(); // "source:external_id" -> {source, external_id, title}
const mnRows = new Map(); // "source:external_id" -> toggle button of the shown result row
const mnPage = { q: "", source: "", offset: 0, total: 0, browsed: false };

const mnKey = (m) => m.source + ":" + m.external_id;
const mnPct = (p) => (p == null ? "–" : (p * 100).toFixed(1) + " %");
const mnUsd = (v) => (v == null ? "–" : Math.round(v).toLocaleString("de-DE") + " USD");

function mnUpdate() {
  $("mn-selected").replaceChildren(...[...mnSelected.values()].map((m) => {
    const chip = Object.assign(document.createElement("span"), { className: "chip", textContent: m.source + ": " + m.title + " " });
    const rm = Object.assign(document.createElement("button"), { type: "button", textContent: "×", title: "Entfernen" });
    rm.dataset.action = "remove";
    rm.addEventListener("click", () => mnToggle(m, false));
    chip.appendChild(rm);
    return chip;
  }));
  for (const [key, btn] of mnRows) mnMark(btn, mnSelected.has(key));
  $("mn-clear").disabled = !mnSelected.size;
  $("mn-create").disabled = !mnSelected.size || !$("mn-name").value.trim();
}

function mnMark(btn, selected) {
  btn.textContent = selected ? "Entfernen" : "Hinzufügen";
  btn.className = selected ? "" : "ghost";
}

function mnToggle(m, add) {
  const key = mnKey(m);
  if (add) mnSelected.set(key, { source: m.source, external_id: m.external_id, title: m.title });
  else mnSelected.delete(key);
  logEvent("info", "metrics.new." + (add ? "add" : "remove") + ".click", { market: key });
  mnUpdate();
}

function mnResultRow(m) {
  const row = Object.assign(document.createElement("div"), { className: "mn-result" });
  const text = Object.assign(document.createElement("span"), {
    textContent: m.source + " · " + m.title + " · Preis " + mnPct(m.price) + " · Liquidität " + mnUsd(m.liquidity) });
  const add = Object.assign(document.createElement("button"), { type: "button" });
  add.dataset.action = "add";
  mnMark(add, mnSelected.has(mnKey(m)));
  mnRows.set(mnKey(m), add);
  add.addEventListener("click", () => mnToggle(m, !mnSelected.has(mnKey(m))));
  row.append(text, add);
  return row;
}

// more=false: new list from the search field and source filter; more=true: append the next page ("Mehr laden").
async function mnSearch(more = false, reason = "search") {
  if (!more) Object.assign(mnPage, { q: $("mn-q").value.trim(), source: $("mn-source").value || "", offset: 0, total: 0 });
  const { q, source, offset } = mnPage;
  logEvent("info", "metrics.new." + (more ? "more" : reason) + ".click", { q, source, offset });
  $("mn-status").textContent = "Suche Märkte …";
  try {
    const res = await fetch("/manufacture/catalog?sort=liquidity&limit=" + MN_LIMIT + "&offset=" + offset + "&q=" + encodeURIComponent(q) +
      (source ? "&source=" + encodeURIComponent(source) : ""));
    if (!res.ok) throw new Error(await fedServerError(res));
    const data = await res.json();
    if (!more) { mnRows.clear(); $("mn-results").replaceChildren(); }
    $("mn-results").append(...data.items.map(mnResultRow));
    mnPage.offset = offset + data.items.length;
    mnPage.total = data.total;
    $("mn-more").disabled = mnPage.offset >= data.total;
    $("mn-status").textContent = data.total + " Treffer" + (data.total > mnPage.offset ? ", die " + mnPage.offset + " liquidesten angezeigt" : "");
    logEvent("info", "metrics.new.search.done", { q, source, total: data.total, shown: mnPage.offset });
  } catch (err) {
    $("mn-status").textContent = "Marktsuche fehlgeschlagen: " + err.message;
    logEvent("error", "metrics.new.search.failed", { q, source, message: err.message });
  }
}

function mnClear() {
  logEvent("info", "metrics.new.clear.click", { markets: [...mnSelected.keys()] });
  mnSelected.clear();
  mnUpdate();
}

async function mnCreate() {
  const body = { name: $("mn-name").value.trim(), calculation: $("mn-calc").value.trim(), markets: [...mnSelected.values()] };
  logEvent("info", "metrics.new.create.click", { name: body.name, markets: body.markets.map(mnKey) });
  try {
    const res = await fetch("/metrics", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(await fedServerError(res));
    const data = await res.json();
    logEvent("info", "metrics.new.create.done", { key: data.key, status: data.status });
    mnSelected.clear();
    $("mn-name").value = $("mn-calc").value = "";
    $("mn-results").replaceChildren();
    mnRows.clear();
    mnUpdate();
    $("mn-status").textContent = "Metrik „" + data.name + "“ angelegt (Status " + data.status + ").";
    await loadMetrics("create");
  } catch (err) {
    $("mn-status").textContent = "Anlegen fehlgeschlagen: " + err.message;
    logEvent("error", "metrics.new.create.failed", { name: body.name, message: err.message });
  }
}

function mnOpen() {
  logEvent("info", "metrics.new.open.click", {});
  const panel = $("metric-new");
  panel.open = true;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  $("mn-name").focus();
}

$("metrics-new").addEventListener("click", mnOpen);
$("metric-new").addEventListener("toggle", () => {
  logEvent("info", "metrics.new.toggle", { open: $("metric-new").open });
  if ($("metric-new").open && !mnPage.browsed) { mnPage.browsed = true; mnSearch(false, "browse"); } // selectable list without typing
});
$("mn-search").addEventListener("click", () => mnSearch());
$("mn-q").addEventListener("keydown", (e) => { if (e.key === "Enter") mnSearch(); });
$("mn-source").addEventListener("change", () => mnSearch(false, "source"));
$("mn-more").addEventListener("click", () => mnSearch(true));
$("mn-clear").addEventListener("click", mnClear);
$("mn-name").addEventListener("input", mnUpdate);
$("mn-create").addEventListener("click", mnCreate);
