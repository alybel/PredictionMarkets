// Parameter panel: edit the rule of the selected index with a live preview
// (POST /indices/{name}/preview, DB untouched), save it (PUT), discard the
// draft, create a new index (POST /indices) or delete one (DELETE).
// Relies on the globals of index.html: $, state, render, loadIndex, loadIndices,
// selectIndex, activeButton, setStatus.

const DEFAULT_RULE = { categories: [], keywords: [], min_liquidity: 10000, min_price: 0.03, max_price: 0.97 };
const MAX_TAG_ROWS = 150;
const cloneRule = (r) => ({
  categories: [...r.categories], keywords: [...r.keywords],
  min_liquidity: r.min_liquidity, min_price: r.min_price, max_price: r.max_price,
});
const params = { name: null, draft: null, tags: null, timer: null };
const JSON_HEADERS = { "Content-Type": "application/json" };

const savedRule = (name) => cloneRule(state.rules[name] || DEFAULT_RULE);
const isDirty = () => params.name != null && JSON.stringify(params.draft) !== JSON.stringify(savedRule(params.name));
const encName = () => encodeURIComponent(params.name);

async function errorText(res) {
  try { const body = await res.json(); return body.detail || ("HTTP " + res.status); }
  catch { return "HTTP " + res.status; }
}

document.addEventListener("index-loaded", (e) => {
  if (e.detail.name !== params.name) {
    params.name = e.detail.name;
    params.draft = savedRule(params.name);
  }
  renderPanel();
  ensureTags();
});

// ---- rendering -------------------------------------------------------------

function renderPanel() {
  const d = params.draft;
  const chips = $("keyword-chips");
  chips.replaceChildren();
  d.keywords.forEach((kw, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = kw + " ";
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.title = "Keyword entfernen";
    x.addEventListener("click", () => { d.keywords.splice(i, 1); onChange(); });
    chip.appendChild(x);
    chips.appendChild(chip);
  });
  if (document.activeElement !== $("min-liquidity")) $("min-liquidity").value = d.min_liquidity;
  if (document.activeElement !== $("min-price")) $("min-price").value = d.min_price;
  if (document.activeElement !== $("max-price")) $("max-price").value = d.max_price;
  renderTagList();
  markDirty();
}

function renderTagList() {
  const list = $("tag-list");
  list.replaceChildren();
  const chosen = new Set(params.draft.categories.map((c) => c.toLowerCase()));
  const q = $("tag-search").value.trim().toLowerCase();
  const delivered = params.tags || [];
  const known = new Set(delivered.map((t) => t.tag.toLowerCase()));
  // Saved tags no source currently delivers stay visible so they can be removed.
  const rows = params.draft.categories.filter((c) => !known.has(c.toLowerCase())).map((c) => ({ tag: c, count: 0 }))
    .concat(delivered)
    .filter((t) => !q || t.tag.toLowerCase().includes(q))
    .sort((a, b) => chosen.has(b.tag.toLowerCase()) - chosen.has(a.tag.toLowerCase()));
  for (const t of rows.slice(0, MAX_TAG_ROWS)) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = chosen.has(t.tag.toLowerCase());
    box.addEventListener("change", () => toggleTag(t.tag, box.checked));
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = t.count ? String(t.count) : "–";
    label.append(box, document.createTextNode(t.tag), count);
    list.appendChild(label);
  }
  if (rows.length > MAX_TAG_ROWS) {
    const more = document.createElement("p");
    more.className = "hint";
    more.textContent = (rows.length - MAX_TAG_ROWS) + " weitere Tags – bitte Suche eingrenzen.";
    list.appendChild(more);
  } else if (!rows.length) {
    const none = document.createElement("p");
    none.className = "hint";
    none.textContent = params.tags ? "Keine passenden Tags." : "Tags werden geladen …";
    list.appendChild(none);
  }
}

function markDirty() {
  const dirty = isDirty();
  $("dirty-badge").classList.toggle("hidden", !dirty);
  $("save-rule").disabled = !dirty;
  $("discard-rule").disabled = !dirty;
}

function showError(text) {
  $("param-error").textContent = text;
  $("param-error").classList.toggle("hidden", !text);
}

async function ensureTags() {
  if (params.tags) return;
  try {
    const res = await fetch("/tags");
    if (!res.ok) throw new Error("HTTP " + res.status);
    params.tags = await res.json();
  } catch {
    params.tags = [];
  }
  renderTagList();
}

// ---- editing + preview ------------------------------------------------------

function onChange() {
  renderPanel();
  clearTimeout(params.timer);
  params.timer = setTimeout(() => preview(false), 250);
}

async function preview(refresh) {
  if (!params.name) return;
  setStatus("Vorschau wird berechnet …", false);
  try {
    const res = await fetch("/indices/" + encName() + "/preview" + (refresh ? "?refresh=1" : ""), {
      method: "POST", headers: JSON_HEADERS, body: JSON.stringify(params.draft),
    });
    if (!res.ok) { showError(await errorText(res)); setStatus("", false); return; }
    render(await res.json());
    showError("");
    setStatus("", false);
  } catch (err) {
    showError("Vorschau fehlgeschlagen: " + err.message);
    setStatus("", false);
  }
}

function toggleTag(tag, on) {
  const cats = params.draft.categories;
  const idx = cats.findIndex((c) => c.toLowerCase() === tag.toLowerCase());
  if (on && idx < 0) cats.push(tag);
  if (!on && idx >= 0) cats.splice(idx, 1);
  onChange();
}

function addKeyword() {
  const input = $("keyword-input");
  const kw = input.value.trim();
  input.value = "";
  if (!kw || params.draft.keywords.some((k) => k.toLowerCase() === kw.toLowerCase())) return;
  params.draft.keywords.push(kw);
  onChange();
}

function numberField(id, key) {
  $(id).addEventListener("input", (e) => {
    const v = Number(e.target.value);
    if (e.target.value === "" || Number.isNaN(v)) return;
    params.draft[key] = v;
    onChange();
  });
}

// ---- persistence -------------------------------------------------------------

async function saveRule() {
  const res = await fetch("/indices/" + encName(), {
    method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(params.draft),
  });
  if (!res.ok) { showError(await errorText(res)); return; }
  const saved = await res.json();
  state.rules[params.name] = saved;
  params.draft = cloneRule(saved);
  showError("");
  renderPanel();
  setStatus("Regel für „" + params.name + "“ dauerhaft gespeichert.", false);
}

function discardDraft() {
  clearTimeout(params.timer);
  params.draft = savedRule(params.name);
  showError("");
  renderPanel();
  loadIndex(params.name, activeButton());
}

async function createIndex() {
  const name = $("new-index-name").value.trim();
  const body = { index_name: name, ...(params.draft || DEFAULT_RULE) };
  const res = await fetch("/indices", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) });
  const errEl = $("new-index-error");
  if (!res.ok) {
    errEl.textContent = await errorText(res);
    errEl.classList.remove("hidden");
    return;
  }
  errEl.classList.add("hidden");
  $("new-index-form").classList.add("hidden");
  $("new-index-name").value = "";
  await loadIndices();
  selectIndex(name);
}

async function deleteIndex() {
  const name = params.name;
  const res = await fetch("/indices/" + encName(), { method: "DELETE" });
  if (!res.ok) { showError(await errorText(res)); return; }
  params.name = null;
  params.draft = null;
  $("delete-confirm").classList.add("hidden");
  const remaining = await loadIndices();
  setStatus("Index „" + name + "“ gelöscht.", false);
  if (remaining.length) selectIndex(remaining[0]);
}

// ---- wiring ----------------------------------------------------------------------

$("keyword-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addKeyword(); }
});
$("keyword-input").addEventListener("blur", addKeyword);
$("tag-search").addEventListener("input", renderTagList);
numberField("min-liquidity", "min_liquidity");
numberField("min-price", "min_price");
numberField("max-price", "max_price");
$("save-rule").addEventListener("click", saveRule);
$("discard-rule").addEventListener("click", discardDraft);
$("delete-index").addEventListener("click", () => $("delete-confirm").classList.remove("hidden"));
$("delete-no").addEventListener("click", () => $("delete-confirm").classList.add("hidden"));
$("delete-yes").addEventListener("click", deleteIndex);
$("new-index").addEventListener("click", () => {
  $("new-index-form").classList.toggle("hidden");
  $("new-index-name").focus();
});
$("cancel-new-index").addEventListener("click", () => $("new-index-form").classList.add("hidden"));
$("create-index").addEventListener("click", createIndex);
$("new-index-name").addEventListener("keydown", (e) => { if (e.key === "Enter") createIndex(); });
document.addEventListener("refresh-requested", (e) => {
  if (isDirty()) { e.preventDefault(); preview(true); }
});
