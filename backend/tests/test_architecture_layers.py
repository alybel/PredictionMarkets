"""Layered architecture (1017-1426): only the raw data layer (``backend.app.rawdata``) may talk to external APIs.

Static scan of the source code (AST, no import, no network, no local config): every module outside the raw data
layer is checked for HTTP / network libraries. The AI client (``anthropic``) is a service, not a data source, and
stays allowed."""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCANNED = [BACKEND / "app", BACKEND / "migrations"]
RAW_LAYER = BACKEND / "app" / "rawdata"
FORBIDDEN = {"requests", "httpx", "urllib", "urllib3", "aiohttp", "http", "socket", "websocket", "websockets",
             "pycurl", "grequests", "treq"}
ALLOWED = {"urllib.parse", "urllib.error", "http.HTTPStatus"}  # pure helpers, no network


def _forbidden(name: str) -> bool:
    """``name`` is a module path (``urllib.request``) or an imported member (``http.HTTPStatus``)."""
    def under(prefixes: set[str]) -> bool:
        return any(name == p or name.startswith(p + ".") for p in prefixes)
    return under(FORBIDDEN) and not under(ALLOWED)


def violations(source: str) -> list[str]:
    """Network imports of one module: ``import x``, ``from x import y``, ``importlib.import_module("x")``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in {"import_module", "__import__"} and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
        found.extend(f"line {node.lineno}: {name}" for name in names if _forbidden(name))
    return found


def outside_raw_layer(roots: list[Path] = SCANNED, raw_layer: Path = RAW_LAYER) -> list[Path]:
    return [p for root in roots for p in sorted(root.rglob("*.py")) if raw_layer not in p.parents]


def test_no_network_library_outside_the_raw_data_layer():
    files = outside_raw_layer()
    assert len(files) > 50  # the scan really covers the business processes
    report = {str(p.relative_to(BACKEND)): v for p in files if (v := violations(p.read_text(encoding="utf-8")))}
    assert report == {}, f"external API access outside backend/app/rawdata: {report}"


def test_raw_data_layer_owns_the_transport():
    assert violations((RAW_LAYER / "http.py").read_text(encoding="utf-8")) == ["line 6: requests"]


def test_injected_http_call_in_a_business_process_is_detected(tmp_path):
    process = BACKEND / "app" / "builder" / "service.py"
    injected = process.read_text(encoding="utf-8") + "\n\nimport requests\nrequests.get('https://example.com')\n"
    planted = tmp_path / "app" / "builder" / "service.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(injected, encoding="utf-8")
    assert outside_raw_layer([tmp_path / "app"], tmp_path / "app" / "rawdata") == [planted]
    assert violations(process.read_text(encoding="utf-8")) == []
    assert violations(injected)[-1].endswith(": requests")


def test_every_http_import_form_is_a_violation():
    for code in ["import requests", "import httpx", "import urllib.request", "from urllib import request",
                 "from urllib.request import urlopen", "import aiohttp", "from http import client",
                 "import http.client", "import socket", "import urllib3", "from httpx import AsyncClient",
                 "import importlib\nimportlib.import_module('requests')", "__import__('httpx')"]:
        assert violations(code), code


def test_harmless_imports_are_allowed():
    for code in ["from urllib.parse import urlencode", "from http import HTTPStatus", "import anthropic",
                 "from backend.app.rawdata import http", "import json", "from urllib.error import URLError"]:
        assert violations(code) == [], code
