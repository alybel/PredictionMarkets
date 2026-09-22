"""Alembic environment: uses DATABASE_URL (SQLite fallback for dev/tests)."""

from alembic import context
from sqlalchemy import create_engine

from backend.app.db import Base, get_database_url
from backend.app import models  # noqa: F401  (registers tables on Base.metadata)
from backend.app.eui import models as eui_models  # noqa: F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
