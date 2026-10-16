// Section "Fed-Zinsketten": buttons "Abrufen → Staging" and "Staging → Raw" (POST /fed/staging/fetch|load) and
// fedServerError, which turns an error response into "HTTP <code>: <server message>". Uses $ and logEvent (shell.js).

async function fedServerError(res) {
  let msg = "";
  try {
    msg = await res.text();
    const body = JSON.parse(msg);
    msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
  } catch { /* not JSON: keep the plain text */ }
  return "HTTP " + res.status + (msg ? ": " + msg.slice(0, 500) : "");
}

const FED_STAGING_STEPS = {
  fetch: {
    button: "fed-stage-fetch", url: "/fed/staging/fetch", label: "Abrufen → Staging",
    text: (s) => s.markets + " Märkte · gefunden " + s.requests + " · neu " + s.written + " · übersprungen " + s.unchanged + " · fehlgeschlagen " + s.failed,
  },
  load: {
    button: "fed-stage-load", url: "/fed/staging/load", label: "Staging → Raw",
    text: (s) => "gefunden " + s.found + " · neu " + s.new + " · übersprungen " + s.skipped + " · fehlgeschlagen " + s.failed,
  },
};

async function runFedStaging(step) {
  const cfg = FED_STAGING_STEPS[step], btn = $(cfg.button), out = $("fed-staging-status");
  btn.disabled = true;
  out.classList.remove("error");
  out.textContent = cfg.label + " läuft …";
  logEvent("info", "fed.staging." + step + ".click", {});
  try {
    const res = await fetch(cfg.url, { method: "POST" });
    if (!res.ok) throw new Error(await fedServerError(res));
    const s = await res.json();
    out.textContent = cfg.label + ": " + cfg.text(s) + " (" + s.duration_s + " s)";
    logEvent("info", "fed.staging." + step + ".done", { summary: cfg.text(s), failures: s.failures });
  } catch (err) {
    out.textContent = cfg.label + " fehlgeschlagen: " + err.message;
    out.classList.add("error");
    logEvent("error", "fed.staging." + step + ".failed", { message: err.message });
  } finally {
    btn.disabled = false;
  }
}

$("fed-stage-fetch").addEventListener("click", () => runFedStaging("fetch"));
$("fed-stage-load").addEventListener("click", () => runFedStaging("load"));
