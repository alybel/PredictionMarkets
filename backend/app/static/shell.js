// Page shell: client-side logging, backend health line in the topbar, error banner and
// the tab navigation. Loaded first; the other scripts use the globals below.

const HEALTH_POLL_MS = 30000;
const TAB_KEY = "pmi.view";

// Writes to the console and forwards the event to the backend log (never breaks the UI).
function logEvent(level, event, detail) {
  const fn = level === "error" ? console.error : level === "warning" ? console.warn : console.log;
  fn("[" + new Date().toISOString() + "] " + event, detail ?? "");
  fetch("/client-log", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ level, event, detail: detail ?? null }),
  }).catch(() => undefined);
}

// Global catcher: uncaught JS errors (incl. script load errors like duplicate declarations) and unhandled
// promise rejections go to the backend log with message, file and line. Capped to avoid flooding.
const JS_ERROR_LIMIT = 50;
let jsErrorCount = 0;
function reportJsError(event, detail) {
  if (++jsErrorCount <= JS_ERROR_LIMIT) logEvent("error", event, detail);
}
window.onerror = (message, file, line, column, error) => {
  reportJsError("js.error", { message: String(message), file: file || null, line: line ?? null, column: column ?? null,
    stack: error?.stack ? String(error.stack).slice(0, 2000) : null });
  return false;
};
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  reportJsError("js.unhandledrejection", { message: String(r?.message ?? r), stack: r?.stack ? String(r.stack).slice(0, 2000) : null });
});

function showBanner(text) {
  const el = document.getElementById("banner");
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

// ---- health line -------------------------------------------------------------------

function renderHealth(h) {
  const el = document.getElementById("health");
  el.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "dot " + (h ? "ok" : "down");
  el.appendChild(dot);
  if (!h) { el.appendChild(document.createTextNode(" Backend nicht verbunden")); return; }
  const parts = ["Backend " + h.version, h.indices + " Indizes"];
  parts.push(h.markets_cached == null ? "Marktdaten on-demand" : h.markets_cached.toLocaleString("de-DE") + " Märkte im Cache");
  el.appendChild(document.createTextNode(" " + parts.join(" · ")));
}

async function refreshHealth() {
  try {
    const res = await fetch("/health");
    if (!res.ok) throw new Error("HTTP " + res.status);
    renderHealth(await res.json());
    showBanner("");
  } catch (err) {
    renderHealth(null);
    showBanner("Backend antwortet nicht: " + err.message);
    logEvent("error", "health.failed", { message: err.message });
  }
}

// ---- tabs ------------------------------------------------------------------------------

function selectView(view, { log = true } = {}) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("active", active);
    if (active) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.viewPanel !== view);
  });
  try { localStorage.setItem(TAB_KEY, view); } catch {}
  if (log) logEvent("info", "tab.select", { view });
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => selectView(tab.dataset.view)));

let initialView = "indices";
try { initialView = localStorage.getItem(TAB_KEY) || initialView; } catch {}
if (!document.querySelector(`.tab[data-view="${initialView}"]`)) initialView = "indices";
selectView(initialView, { log: false });

logEvent("info", "app.start", { view: initialView });
refreshHealth();
setInterval(refreshHealth, HEALTH_POLL_MS);
