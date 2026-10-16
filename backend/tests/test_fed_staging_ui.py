"""Fed rate chains: buttons "Abrufen → Staging" / "Staging → Raw" and server error messages in the UI
(1015-2120, part 3). Endpoints with fixture payloads (no network); frontend script in node with a stub DOM."""

import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import app, get_session
from backend.app.fed.api import get_raw_getter, get_staging_dir
from backend.tests.eui_helpers import memory_session
from backend.tests.test_staging_raw import raw_getter

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

# Runs fedstaging.js against stub $, fetch and logEvent, clicks one button, prints the resulting UI state.
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [file, button, status, body] = process.argv.slice(1);
const els = {}, events = [], calls = [];
const classList = () => { const s = new Set(); return { add: (c) => s.add(c), remove: (c) => s.delete(c), has: (c) => s.has(c) }; };
const el = (id) => els[id] ??= { id, textContent: "", disabled: false, handlers: {}, classList: classList(),
  addEventListener(type, fn) { this.handlers[type] = fn; } };
const ctx = { console, JSON, $: el, logEvent: (level, event, detail) => events.push([level, event, detail]),
  fetch: async (url, opts) => { calls.push([url, opts.method]); return { ok: +status < 400, status: +status,
    text: async () => body, json: async () => JSON.parse(body) }; } };
vm.runInNewContext(fs.readFileSync(file, "utf8"), ctx);
(async () => {
  await els[button].handlers.click();
  const out = els["fed-staging-status"];
  console.log(JSON.stringify({ text: out.textContent, error: out.classList.has("error"), disabled: els[button].disabled, calls, events }));
})();
"""


def click(button: str, status: int, body: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    out = subprocess.run(["node", "-e", HARNESS, str(STATIC / "fedstaging.js"), button, str(status), body],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture()
def client(tmp_path):
    session = memory_session()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_raw_getter] = raw_getter
    app.dependency_overrides[get_staging_dir] = lambda: tmp_path / "staging"
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---- rendering ---------------------------------------------------------------------------------

def test_both_buttons_render_in_the_fed_section(client):
    html = client.get("/").text
    card = html[html.index('id="fed-card"'):html.index('id="fed-chart"')]
    assert 'id="fed-stage-fetch"' in card and "Abrufen → Staging</button>" in card
    assert 'id="fed-stage-load"' in card and "Staging → Raw</button>" in card
    assert 'id="fed-staging-status"' in card
    assert html.index("/static/fedstaging.js") < html.index("/static/fedchains.js")
    assert client.get("/static/fedstaging.js").status_code == 200


# ---- click -> request + log ----------------------------------------------------------------------

def test_click_sends_the_request_and_logs_the_click():
    summary = {"found": 3, "new": 3, "skipped": 0, "failed": 0, "duration_s": 0.1, "failures": []}
    r = click("fed-stage-load", 200, json.dumps(summary))
    assert r["calls"] == [["/fed/staging/load", "POST"]]
    assert [e[1] for e in r["events"]] == ["fed.staging.load.click", "fed.staging.load.done"]
    r = click("fed-stage-fetch", 200, json.dumps({"markets": 2, "requests": 5, "written": 5, "unchanged": 0, "failed": 0, "duration_s": 1}))
    assert r["calls"] == [["/fed/staging/fetch", "POST"]] and r["events"][0][1] == "fed.staging.fetch.click"


def test_client_click_event_lands_in_the_server_log(client, caplog):
    caplog.set_level(logging.INFO, logger="backend.app.client")
    assert client.post("/client-log", json={"level": "info", "event": "fed.staging.fetch.click", "detail": {}}).status_code == 204
    assert any("fed.staging.fetch.click" in r.getMessage() for r in caplog.records)


# ---- error message -----------------------------------------------------------------------------

def test_server_500_message_is_shown_in_the_ui():
    r = click("fed-stage-load", 500, json.dumps({"detail": "Staging -> Raw fehlgeschlagen: OperationalError: disk I/O error"}))
    assert r["text"] == "Staging → Raw fehlgeschlagen: HTTP 500: Staging -> Raw fehlgeschlagen: OperationalError: disk I/O error"
    assert r["error"] and not r["disabled"]
    assert r["events"][-1][:2] == ["error", "fed.staging.load.failed"]


def test_plain_text_error_body_is_shown_too():
    r = click("fed-stage-fetch", 502, "Bad Gateway")
    assert r["text"].endswith("HTTP 502: Bad Gateway") and r["error"]


def test_endpoint_error_returns_500_with_the_cause(client, caplog):
    def broken(url, params=None):
        raise ConnectionError("kalshi unreachable")
    app.dependency_overrides[get_raw_getter] = lambda: broken
    res = client.post("/fed/staging/fetch")
    assert res.status_code == 500
    assert res.json()["detail"] == "Abrufen -> Staging fehlgeschlagen: ConnectionError: kalshi unreachable"
    assert any("fed staging Abrufen -> Staging failed" in r.getMessage() for r in caplog.records)


# ---- successful run ------------------------------------------------------------------------------

def test_success_shows_found_new_skipped_failed():
    summary = {"found": 7, "new": 4, "skipped": 3, "failed": 0, "duration_s": 0.25, "failures": []}
    r = click("fed-stage-load", 200, json.dumps(summary))
    assert r["text"] == "Staging → Raw: gefunden 7 · neu 4 · übersprungen 3 · fehlgeschlagen 0 (0.25 s)"
    assert not r["error"] and not r["disabled"]


def test_fetch_then_second_load_brings_zero_files(client):
    fetched = client.post("/fed/staging/fetch").json()
    assert fetched["written"] > 0 and fetched["failed"] == 0
    first = client.post("/fed/staging/load").json()
    assert first["found"] == fetched["written"] and first["new"] == first["found"] and first["failed"] == 0
    second = client.post("/fed/staging/load").json()
    assert second["new"] == 0 and second["skipped"] == second["found"] == first["found"]
