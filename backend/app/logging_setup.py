"""Application-wide logging: console plus a rotating file under ``logs/``.

Every process entry point (API server, daily job) calls ``configure_logging``
once; repeated calls are no-ops so tests and reloads never duplicate handlers.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_MARKER = "pmi_configured"


def configure_logging(level: int | None = None) -> Path:
    """Attach console and file handlers to the root logger (idempotent); returns the log file path."""
    root = logging.getLogger()
    log_file = LOG_DIR / "app.log"
    if getattr(root, _MARKER, False):
        return log_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root.setLevel(level or getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console)
    setattr(root, _MARKER, True)
    return log_file
