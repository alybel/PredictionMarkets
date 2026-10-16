"""Database schema state: auto-migration to Alembic head and revision check.

The API lifespan and ``start_dev.sh`` (``python -m backend.app.schema_state``)
upgrade the database to head before serving, so existing rows survive and new
tables exist. ``/health`` uses ``schema_status`` to flag an outdated schema.
"""

import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from backend.app.db import get_database_url, make_engine

logger = logging.getLogger("backend.app.schema_state")  # stable name also under `python -m`

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def schema_status(connection) -> dict:
    """Current vs. head revision; ``warning`` is set when the schema is outdated."""
    current, head = current_revision(connection), head_revision()
    up_to_date = current == head
    warning = None if up_to_date else f"Schema veraltet: Revision {current or 'keine'}, erwartet {head} — Migration ausführen"
    return {"current": current, "head": head, "up_to_date": up_to_date, "warning": warning}


def migrate_to_head() -> dict:
    """Upgrade DATABASE_URL to head (no-op when current); logs before/after state."""
    engine = make_engine()
    try:
        with engine.connect() as conn:
            before = current_revision(conn)
        head = head_revision()
        if before != head:
            logger.info("Schema migration started: %s -> %s (%s)", before or "empty", head, _safe_url())
            command.upgrade(alembic_config(), "head")
        with engine.connect() as conn:
            status = schema_status(conn)
    finally:
        engine.dispose()
    logger.info("Schema state: revision %s, head %s, up_to_date=%s", status["current"], status["head"], status["up_to_date"])
    return status


def auto_migrate_enabled() -> bool:
    return os.environ.get("AUTO_MIGRATE", "1") != "0"


def _safe_url() -> str:
    """Database URL without credentials for the log."""
    url = get_database_url()
    return url.split("@", 1)[-1] if "@" in url else url


if __name__ == "__main__":
    from backend.app.logging_setup import configure_logging

    configure_logging()
    migrate_to_head()
