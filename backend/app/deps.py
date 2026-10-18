"""FastAPI dependencies shared by all routers (overridable in tests)."""

from backend.app.db import make_engine, make_session_factory

_session_factory = None


def session_factory():
    """The app's session factory (created on first use), also for work outside a request."""
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(make_engine())
    return _session_factory


def get_session():
    with session_factory()() as session:
        yield session
