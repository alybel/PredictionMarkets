"""Probe merge tests: daily JSON Lines files, deduplication, day status, GitHub artifact fetch.

No live API calls: the GitHub REST API is a fake session.
"""

import io
import json
import zipfile
from datetime import datetime, timezone

from backend.app.probe.merge import (append_records, artifact_key, day_status, github_records, local_records, main,
                                     run_key, stored_keys)


def record(ts: str, run_id: str | None = "1", status: str = "ok", kalshi_status: int = 200) -> dict:
    return {"schema": 1, "timestamp": ts, "run_id": run_id, "run_attempt": "1", "status": status,
            "sources": {"kalshi": {"http_status": kalshi_status, "error": "rate_limited" if kalshi_status == 429 else None},
                        "polymarket": {"http_status": 200, "error": None}},
            "markets": []}


def test_run_and_artifact_keys_match():
    assert run_key(record("2026-09-22T10:00:00+00:00", "123")) == "run:123:1"
    assert run_key(record("2026-09-22T10:00:00+00:00", None)) == "ts:2026-09-22T10:00:00+00:00"
    assert artifact_key("probe-run-123-1") == "run:123:1" and artifact_key("probe-run-123") == "run:123:1"
    assert artifact_key("coverage-report") is None


def test_append_groups_by_day_and_skips_known(tmp_path):
    records = [record("2026-09-22T23:55:00+00:00", "1"), record("2026-09-23T00:00:00+00:00", "2"),
               record("2026-09-22T23:50:00+00:00", "3")]
    assert append_records(tmp_path, records, set()) == {"2026-09-22": 2, "2026-09-23": 1}
    lines = (tmp_path / "2026-09-22.jsonl").read_text().splitlines()
    assert [json.loads(l)["run_id"] for l in lines] == ["3", "1"]  # sorted by timestamp
    assert stored_keys(tmp_path) == {"run:1:1", "run:2:1", "run:3:1"}
    again = append_records(tmp_path, records + [record("2026-09-23T00:05:00+00:00", "4")], stored_keys(tmp_path))
    assert again == {"2026-09-23": 1}
    assert len((tmp_path / "2026-09-23.jsonl").read_text().splitlines()) == 2
    status = json.loads((tmp_path / "2026-09-23.status.json").read_text())
    assert status["runs"] == 2 and status["gaps"] == []


def test_day_status_counts_failures_gaps_and_429():
    records = [record("2026-09-22T10:00:00+00:00", "1"), record("2026-09-22T10:05:00+00:00", "2", "partial", 429),
               record("2026-09-22T10:25:00+00:00", "3", "failed"), record("2026-09-22T10:30:00+00:00", "4")]
    status = day_status(list(reversed(records)))
    assert (status["runs"], status["ok"], status["partial"], status["failed"]) == (4, 2, 1, 1)
    assert status["http_429"] == 1 and status["source_errors"] == 1
    assert status["gaps"] == [{"from": "2026-09-22T10:05:00+00:00", "to": "2026-09-22T10:25:00+00:00", "minutes": 20.0}]
    assert status["first"] == "2026-09-22T10:00:00+00:00" and status["last"] == "2026-09-22T10:30:00+00:00"
    assert day_status([]) == {"runs": 0, "ok": 0, "partial": 0, "failed": 0, "http_429": 0, "source_errors": 0,
                              "gaps": [], "first": None, "last": None}


def test_local_merge_via_main(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "a.json").write_text(json.dumps(record("2026-09-22T10:00:00+00:00", "1")))
    (runs / "broken.json").write_text("{not json")
    (runs / "other.json").write_text(json.dumps({"no": "timestamp"}))
    assert len(local_records(runs)) == 1
    assert main(["--local", str(runs), "--data-dir", str(tmp_path / "data")]) == 0
    assert (tmp_path / "data" / "2026-09-22.jsonl").exists()
    assert main(["--local", str(runs), "--data-dir", str(tmp_path / "data")]) == 0  # idempotent
    assert len((tmp_path / "data" / "2026-09-22.jsonl").read_text().splitlines()) == 1


def test_main_without_github_env_returns_2(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert main(["--data-dir", str(tmp_path)]) == 2


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b""):
        self._payload, self.content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def zipped(records: list[dict]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for i, r in enumerate(records):
            archive.writestr(f"run-{i}.json", json.dumps(r))
    return buffer.getvalue()


class FakeGitHub:
    def __init__(self, artifacts: list[dict], archives: dict[str, bytes]):
        self._artifacts, self._archives = artifacts, archives
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith("/actions/artifacts"):
            page = params["page"]
            return FakeResponse({"artifacts": self._artifacts if page == 1 else []})
        return FakeResponse(content=self._archives[url])


def artifact(name: str, created: str, expired: bool = False) -> dict:
    return {"name": name, "created_at": created, "expired": expired, "archive_download_url": f"dl/{name}"}


def test_github_records_skip_known_expired_old_and_foreign_artifacts():
    since = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
    artifacts = [artifact("probe-run-5-1", "2026-09-22T10:05:00Z"), artifact("probe-run-4-1", "2026-09-22T10:00:00Z"),
                 artifact("probe-run-3-1", "2026-09-22T09:55:00Z", expired=True), artifact("build-log", "2026-09-22T09:50:00Z"),
                 artifact("probe-run-1-1", "2026-09-22T07:00:00Z")]
    api = FakeGitHub(artifacts, {"dl/probe-run-5-1": zipped([record("2026-09-22T10:05:00+00:00", "5")]),
                                 "dl/probe-run-4-1": b"not a zip"})
    records = github_records(api, "alex/pmi", "tok", since, known={"run:4:1"})
    assert [r["run_id"] for r in records] == ["5"]
    assert api.calls == ["https://api.github.com/repos/alex/pmi/actions/artifacts", "dl/probe-run-5-1"]
    bad = FakeGitHub([artifact("probe-run-4-1", "2026-09-22T10:00:00Z")], {"dl/probe-run-4-1": b"not a zip"})
    assert github_records(bad, "alex/pmi", "tok", since, known=set()) == []  # unreadable archive is skipped, not fatal
