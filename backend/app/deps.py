"""FastAPI dependencies shared by all routers (overridable in tests)."""

from backend.app.db import make_engine, make_session_factory

_session_factory = None


def get_session():
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(make_engine())
    with _session_factory() as session:
        yield session
