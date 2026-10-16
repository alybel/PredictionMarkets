"""Deleting indices (1015-2231-indizes-loschen-konnen): metric indices and archive rule indices,
404 for unknown names, logging, frontend wiring. No network, no LLM."""

import logging
from pathlib import Path

from backend.app.builder.models import MetricIndex
from backend.app.engine import get_rule
from backend.tests.test_metric_index_builder import env, two_metric_index  # noqa: F401 (fixture)

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_metric_index_can_be_deleted(env, caplog):  # noqa: F811
    client, session = env
    caplog.set_level(logging.INFO)
    assert two_metric_index(client).status_code == 201
    assert two_metric_index(client, "Zweiter").status_code == 201
    assert client.delete("/builder/indices/Makro").status_code == 204
    assert [i["name"] for i in client.get("/builder/indices").json()["indices"]] == ["Zweiter"]
    assert client.get("/builder/indices/Makro").status_code == 404
    assert session.query(MetricIndex).count() == 1
    assert "metric index 'Makro' deleted" in caplog.text
    assert two_metric_index(client).status_code == 201  # the name is free again


def test_deleting_an_unknown_metric_index_is_404(env):  # noqa: F811
    client, _ = env
    assert client.delete("/builder/indices/missing").status_code == 404


def test_archive_index_can_be_deleted(env, caplog):  # noqa: F811
    client, session = env
    caplog.set_level(logging.INFO)
    assert two_metric_index(client).status_code == 201
    assert client.delete("/indices/legacy").status_code == 204
    assert get_rule(session, "legacy") is None
    assert client.get("/indices").json() == []
    assert client.get("/indices/legacy").status_code == 404
    assert client.delete("/indices/legacy").status_code == 404
    assert [i["name"] for i in client.get("/builder/indices").json()["indices"]] == ["Makro"]  # metric indices untouched
    assert "archive index 'legacy' deleted" in caplog.text
    assert "archive index 'legacy' delete refused (404)" in caplog.text


def test_frontend_offers_delete_with_confirmation_and_logging():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="mi-delete"' in html and 'id="delete-archive-index"' in html
    js = (STATIC / "indices.js").read_text(encoding="utf-8")
    assert "confirm(" in js and '{ method: "DELETE" }' in js
    assert '"/builder/indices/"' in js and '"/indices/"' in js
    for event in ("index.deleted", "index.delete.failed", "index.delete.cancelled"):
        assert event in js
    assert 'deleteIndex(mi.current.name, false)' in (STATIC / "metricindex.js").read_text(encoding="utf-8")
