"""Server-side cache of the raw market population.

Fetching both sources costs many HTTP requests, so repeated dashboard
calls and later rule changes reuse the last snapshot for ``ttl_seconds``.
``get(force=True)`` (the dashboard's refresh button) re-fetches at once.
"""

import threading
import time
from collections.abc import Callable

DEFAULT_TTL_SECONDS = 60.0


class MarketCache:
    def __init__(self, loader: Callable[[], list], ttl_seconds: float = DEFAULT_TTL_SECONDS, clock=time.monotonic):
        self._loader = loader
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._markets: list | None = None
        self._loaded_at: float | None = None

    def get(self, force: bool = False) -> list:
        with self._lock:
            if force or self._expired():
                self._markets = list(self._loader())
                self._loaded_at = self._clock()
            return self._markets

    def _expired(self) -> bool:
        return self._loaded_at is None or self._clock() - self._loaded_at >= self._ttl

    def clear(self) -> None:
        with self._lock:
            self._markets = None
            self._loaded_at = None
