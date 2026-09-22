"""Hourly merge: run files (workflow artifacts) -> daily JSON Lines file -> day status.

Reads the ``probe-run-*`` artifacts of the collect workflow through the
GitHub REST API (or JSON files from a local folder with ``--local``),
appends every run not stored yet to ``probe/data/<day>.jsonl`` and rewrites
``probe/data/<day>.status.json`` with run counts, HTTP 429 hits and gaps in
the five-minute cadence. A run is identified by its GitHub run id and
attempt (timestamp for local runs), so re-running the merge is harmless.
"""

import argparse
import io
import json
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from backend.app.logging_setup import configure_logging
from backend.app.probe import ARTIFACT_PREFIX, DATA_DIR

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GAP_MINUTES = 10  # twice the schedule: a longer distance between runs means a missed run
TIMEOUT = 30.0


def run_key(record: dict) -> str:
    if record.get("run_id"):
        return f"run:{record['run_id']}:{record.get('run_attempt') or 1}"
    return f"ts:{record['timestamp']}"


def artifact_key(name: str) -> str | None:
    """``probe-run-<run id>-<attempt>`` -> the matching run key; None for other artifacts."""
    if not name.startswith(ARTIFACT_PREFIX):
        return None
    parts = name[len(ARTIFACT_PREFIX):].split("-")
    return f"run:{parts[0]}:{parts[1] if len(parts) > 1 else 1}"


def read_day(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stored_keys(data_dir: Path, days: int = 3) -> set[str]:
    """Keys of the runs already stored in the newest daily files."""
    keys: set[str] = set()
    for path in sorted(Path(data_dir).glob("*.jsonl"))[-days:]:
        keys.update(run_key(r) for r in read_day(path))
    return keys


def day_status(records: list[dict]) -> dict:
    """Counts and cadence gaps for one day's runs (input in any order)."""
    records = sorted(records, key=lambda r: r["timestamp"])
    times = [datetime.fromisoformat(r["timestamp"]) for r in records]
    gaps = []
    for earlier, later in zip(times, times[1:]):
        minutes = (later - earlier).total_seconds() / 60
        if minutes > GAP_MINUTES:
            gaps.append({"from": earlier.isoformat(timespec="seconds"), "to": later.isoformat(timespec="seconds"), "minutes": round(minutes, 1)})
    sources = [s for r in records for s in (r.get("sources") or {}).values()]
    return {
        "runs": len(records),
        "ok": sum(r.get("status") == "ok" for r in records),
        "partial": sum(r.get("status") == "partial" for r in records),
        "failed": sum(r.get("status") == "failed" for r in records),
        "http_429": sum(s.get("http_status") == 429 for s in sources),
        "source_errors": sum(s.get("error") is not None for s in sources),
        "gaps": gaps,
        "first": times[0].isoformat(timespec="seconds") if times else None,
        "last": times[-1].isoformat(timespec="seconds") if times else None,
    }


def append_records(data_dir: Path, records: list[dict], known: set[str]) -> dict[str, int]:
    """Append unknown runs to their day's file and refresh that day's status; returns new runs per day."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    added: dict[str, int] = {}
    fresh: dict[str, list[dict]] = {}
    for record in sorted(records, key=lambda r: r["timestamp"]):
        key = run_key(record)
        if key in known:
            continue
        known.add(key)
        fresh.setdefault(record["timestamp"][:10], []).append(record)
    for day, day_records in fresh.items():
        path = data_dir / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for record in day_records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        status = day_status(read_day(path))
        (data_dir / f"{day}.status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        added[day] = len(day_records)
        logger.info("%s: +%d runs (total %d, gaps %d, 429s %d)", day, len(day_records), status["runs"], len(status["gaps"]), status["http_429"])
    return added


def local_records(folder: Path) -> list[dict]:
    records = []
    for path in sorted(Path(folder).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            logger.warning("skipping unreadable run file %s", path)
            continue
        if isinstance(record, dict) and "timestamp" in record:
            records.append(record)
    return records


def github_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def list_artifacts(session, repo: str, token: str, since: datetime, max_pages: int = 10) -> list[dict]:
    """Probe artifacts created after ``since`` (newest first as the API lists them)."""
    found: list[dict] = []
    for page in range(1, max_pages + 1):
        response = session.get(f"{GITHUB_API}/repos/{repo}/actions/artifacts", params={"per_page": 100, "page": page},
                               headers=github_headers(token), timeout=TIMEOUT)
        response.raise_for_status()
        artifacts = response.json().get("artifacts") or []
        if not artifacts:
            break
        for artifact in artifacts:
            created = datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
            if created < since:
                return found
            if artifact_key(artifact.get("name", "")) and not artifact.get("expired"):
                found.append(artifact)
    return found


def download_records(session, artifact: dict, token: str) -> list[dict]:
    """Download one artifact zip and return the run records inside it."""
    response = session.get(artifact["archive_download_url"], headers=github_headers(token), timeout=TIMEOUT)
    response.raise_for_status()
    records = []
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            if name.endswith(".json"):
                record = json.loads(archive.read(name).decode("utf-8"))
                if isinstance(record, dict) and "timestamp" in record:
                    records.append(record)
    return records


def github_records(session, repo: str, token: str, since: datetime, known: set[str]) -> list[dict]:
    """Records of all probe artifacts since ``since`` that are not stored yet; download errors are logged and skipped."""
    records: list[dict] = []
    artifacts = list_artifacts(session, repo, token, since)
    pending = [a for a in artifacts if artifact_key(a["name"]) not in known]
    logger.info("github: %d probe artifacts since %s, %d not stored yet", len(artifacts), since.isoformat(timespec="minutes"), len(pending))
    for artifact in pending:
        try:
            records.extend(download_records(session, artifact, token))
        except (requests.RequestException, zipfile.BadZipFile, ValueError) as exc:
            logger.warning("artifact %s could not be read: %s", artifact.get("name"), exc)
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Probe: merge run files into the daily JSON Lines files")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--local", type=Path, help="merge run files from this folder instead of GitHub artifacts")
    parser.add_argument("--since-hours", type=int, default=48)
    args = parser.parse_args(argv)
    configure_logging()
    known = stored_keys(args.data_dir)
    if args.local:
        records = local_records(args.local)
    else:
        repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        if not repo or not token:
            logger.error("GITHUB_REPOSITORY and GITHUB_TOKEN are required without --local")
            return 2
        since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
        records = github_records(requests.Session(), repo, token, since, known)
    added = append_records(args.data_dir, records, known)
    logger.info("merge finished: %s", added or "nothing new")
    print(json.dumps(added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
