"""Static frontend scripts: no shared top-level names, attribution format, global JS error catcher (no browser)."""

import re
import shutil
import subprocess
from pathlib import Path

from backend.tests.test_dashboard import client  # noqa: F401  (fixture)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
TOP_LEVEL = re.compile(r"^(?:const|let|var|class|(?:async\s+)?function\s*\*?)\s*([A-Za-z_$][\w$]*)", re.M)


def top_level_names(files: list[Path]) -> dict[str, list[str]]:
    """Name -> files that declare it at top level (classic scripts share one global scope)."""
    names: dict[str, list[str]] = {}
    for f in files:
        for name in TOP_LEVEL.findall(f.read_text(encoding="utf-8")):
            names.setdefault(name, []).append(f.name)
    return names


def duplicates(files: list[Path]) -> dict[str, list[str]]:
    return {n: fs for n, fs in top_level_names(files).items() if len(fs) > 1}


def test_static_scripts_declare_no_shared_top_level_names():
    dups = duplicates(sorted(STATIC.glob("*.js")))
    assert not dups, "Top-level-Namen doppelt deklariert: " + "; ".join(f"{n} in {', '.join(fs)}" for n, fs in dups.items())


def test_duplicate_detection_reports_both_files(tmp_path):
    (tmp_path / "a.js").write_text("const fmtShare = (v) => v;\nfunction onlyA() {}\n")
    (tmp_path / "b.js").write_text("// const fmtShare in a comment is ignored\n  const inner = 1;\nconst fmtShare = 2;\nasync function onlyB() {}\n")
    assert duplicates(sorted(tmp_path.glob("*.js"))) == {"fmtShare": ["a.js", "b.js"]}


def test_attribution_has_no_fmtshare_and_keeps_format():
    src = (STATIC / "attribution.js").read_text(encoding="utf-8")
    assert "fmtShare" not in top_level_names([STATIC / "attribution.js"])
    line = next(l for l in src.splitlines() if l.startswith("const fmtContribution"))
    if not shutil.which("node"):
        assert '(v >= 0 ? "+" : "") + v.toFixed(3).replace(".", ",")' in line
        return
    js = line + "\nconsole.log([fmtContribution(0.1234), fmtContribution(-0.05), fmtContribution(null)].join('|'));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "+0,123|-0,050|–"


def test_trades_load_attribution_independently():
    src = (STATIC / "trades.js").read_text(encoding="utf-8")
    assert "startAttribution(ticker, rule);" in src and "\n  loadAttribution(" not in src
    assert 'try { Promise.resolve(loadAttribution(ticker, rule)).catch(fail); } catch (err) { fail(err); }' in src


def test_shell_registers_global_error_handlers():
    src = (STATIC / "shell.js").read_text(encoding="utf-8")
    assert "window.onerror = (message, file, line" in src
    assert 'window.addEventListener("unhandledrejection"' in src
    assert 'fetch("/client-log"' in src and "reportJsError(" in src


def test_client_log_accepts_js_error_event(client, caplog):  # noqa: F811
    detail = {"message": "Identifier 'fmtShare' has already been declared", "file": "/static/attribution.js", "line": 7}
    with caplog.at_level("ERROR", logger="backend.app.client"):
        res = client.post("/client-log", json={"level": "error", "event": "js.error", "detail": detail})
    assert res.status_code == 204
    msg = next(r.getMessage() for r in caplog.records if "js.error" in r.getMessage())
    assert "fmtShare" in msg and "attribution.js" in msg and "7" in msg
