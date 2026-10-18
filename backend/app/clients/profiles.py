"""Client settings per use: "index" (dashboard population, global liquidity floor) and "snapshot" (daily
uncertainty snapshot: every market, more pages). The same profile drives the raw job's requests and the
later reading of their answers, so both run the identical request sequence."""

from collections.abc import Callable

from backend.app.clients.base import BaseClient, GetJson
from backend.app.clients.kalshi import KalshiClient
from backend.app.clients.polymarket import PolymarketClient
from backend.app.models import MIN_LIQUIDITY_USD

SNAPSHOT_DELAY_SECONDS = 0.2
PROFILES = {
    "index": {"floor": MIN_LIQUIDITY_USD, "pages": {}, "delay": 0.0},
    "snapshot": {"floor": None, "pages": {"polymarket": 50, "kalshi": 30}, "delay": SNAPSHOT_DELAY_SECONDS},
}


def make_clients(profile: str, getter_for: Callable[[str], GetJson], fetching: bool = False) -> list[BaseClient]:
    """Both clients of a profile, each with the getter ``getter_for(source)``; the rate-limit pause applies only
    while ``fetching`` from the sources."""
    p = PROFILES[profile]
    clients = []
    for cls in (PolymarketClient, KalshiClient):
        client = cls(liquidity_floor=p["floor"], request_delay=p["delay"] if fetching else 0.0, get_json=getter_for(cls.source))
        client.max_pages = p["pages"].get(client.source, client.max_pages)
        clients.append(client)
    return clients
