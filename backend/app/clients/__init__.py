"""Market data clients for Polymarket and Kalshi."""

from backend.app.clients.base import NormalizedMarket
from backend.app.clients.kalshi import KalshiClient
from backend.app.clients.polymarket import PolymarketClient

__all__ = ["NormalizedMarket", "KalshiClient", "PolymarketClient"]
