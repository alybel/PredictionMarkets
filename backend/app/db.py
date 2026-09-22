"""Database engine and session setup.

DATABASE_URL points to PostgreSQL in production; defaults to a local
SQLite file so development and tests need no running server.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///backend/dev.db"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    return create_engine(url or get_database_url())


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
