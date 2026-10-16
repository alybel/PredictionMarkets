"""Staging folder: API answers stored unchanged as files under ``data/staging/<source>/<dataset>/<date>/``.

Each answer is two files with the same stem ``<request id>_<HHMMSSffffff>_<sha8>``:
``.json`` holds the body byte for byte, ``.meta.json`` the URL, parameters, fetch time and checksum.
Files are never overwritten (published via ``os.link``, which fails on an existing name). An answer whose
body equals the latest file of the same request is not written again. The meta file is published first,
so a body file always has its meta file.
"""

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from backend.app.logging_setup import PROJECT_ROOT

logger = logging.getLogger(__name__)

META_SUFFIX = ".meta.json"
BODY_SUFFIX = ".json"


class StagingError(ValueError):
    """A staging file that cannot be loaded (missing meta, wrong checksum, no JSON, ...)."""


def staging_root() -> Path:
    return Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data")) / "staging"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_key(url: str, params: dict | None, ignore: frozenset[str] = frozenset()) -> str:
    """Stable identity of a request: URL plus sorted parameters without the volatile ones (e.g. ``end_ts``)."""
    items = sorted((k, str(v)) for k, v in (params or {}).items() if k not in ignore)
    return f"{url}?{urlencode(items)}" if items else url


def is_body_file(path: Path) -> bool:
    return path.name.endswith(BODY_SUFFIX) and not path.name.endswith(META_SUFFIX) and not path.name.startswith(".")


def meta_path(body: Path) -> Path:
    return body.with_name(body.name[: -len(BODY_SUFFIX)] + META_SUFFIX)


def _publish(path: Path, data: bytes) -> bool:
    """Write ``data`` to ``path`` atomically and without overwriting; False when the name already exists."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class StageResult:
    path: Path
    written: bool
    checksum: str


class Stager:
    """Writes answers into the staging folder; remembers the latest file per request (one scan per dataset)."""

    def __init__(self, root: Path | None = None, ignore_params: frozenset[str] = frozenset()):
        self.root = Path(root) if root else staging_root()
        self.ignore = ignore_params
        self._latest: dict[tuple[str, str], dict[str, Path]] = {}

    def _index(self, source: str, dataset: str) -> dict[str, Path]:
        key = (source, dataset)
        if key not in self._latest:
            latest: dict[str, Path] = {}
            for body in sorted((self.root / source / dataset).glob(f"*/*{BODY_SUFFIX}"), key=lambda p: (p.parent.name, p.name)):
                if is_body_file(body):
                    latest[body.name.split("_", 1)[0]] = body  # sorted by date and time: the last one wins
            self._latest[key] = latest
        return self._latest[key]

    def write(self, source: str, dataset: str, url: str, params: dict | None, body: bytes, fetched_at: datetime) -> StageResult:
        checksum = sha256(body)
        rkey = request_key(url, params, self.ignore)
        rid = sha256(rkey.encode())[:16]
        index = self._index(source, dataset)
        last = index.get(rid)
        if last is not None and sha256(last.read_bytes()) == checksum:
            logger.debug("staging %s/%s unchanged: %s", source, dataset, last.name)
            return StageResult(last, False, checksum)
        day_dir = self.root / source / dataset / fetched_at.date().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{rid}_{fetched_at:%H%M%S%f}_{checksum[:8]}{BODY_SUFFIX}"
        meta = {"source": source, "dataset": dataset, "url": url, "params": params or {}, "request_key": rkey,
                "fetched_at": fetched_at.isoformat(), "checksum": checksum, "size": len(body)}
        _publish(meta_path(path), json.dumps(meta, indent=1, sort_keys=True).encode())
        written = _publish(path, body)  # False only for the very same body at the very same microsecond
        index[rid] = path
        logger.debug("staging %s/%s %s: %s", source, dataset, "written" if written else "exists", path.name)
        return StageResult(path, written, checksum)


@dataclass(frozen=True)
class StagedFile:
    rel_path: str
    source: str
    dataset: str
    url: str
    request_key: str
    fetched_at: datetime
    checksum: str
    content: str


def list_staged(root: Path) -> list[Path]:
    """All body files, oldest first."""
    return sorted((p for p in root.glob(f"*/*/*/*{BODY_SUFFIX}") if is_body_file(p)), key=lambda p: (p.parent.name, p.name, str(p)))


def read_staged(root: Path, body: Path) -> StagedFile:
    """Read and verify one staging file; raises ``StagingError`` when it is broken."""
    rel = body.relative_to(root).as_posix()
    parts = rel.split("/")
    if len(parts) != 4:
        raise StagingError(f"unexpected location {rel}")
    try:
        meta = json.loads(meta_path(body).read_bytes())
        data = body.read_bytes()
    except FileNotFoundError as exc:
        raise StagingError(f"missing file: {Path(exc.filename).name}") from exc
    except json.JSONDecodeError as exc:
        raise StagingError(f"meta is no JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise StagingError("meta is no JSON object")
    try:
        url, rkey, checksum = str(meta["url"]), str(meta["request_key"]), str(meta["checksum"])
        fetched_at = datetime.fromisoformat(meta["fetched_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StagingError(f"meta incomplete: {exc!r}") from exc
    if fetched_at.tzinfo is None:
        raise StagingError("fetched_at without time zone")
    if sha256(data) != checksum:
        raise StagingError("checksum mismatch")
    try:
        content = data.decode("utf-8")
        json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagingError(f"body is no JSON: {exc}") from exc
    return StagedFile(rel, parts[0], parts[1], url, rkey, fetched_at, checksum, content)
