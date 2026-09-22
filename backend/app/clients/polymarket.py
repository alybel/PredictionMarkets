"""Polymarket client (Gamma API).

Markets are loaded via ``/events`` because only events carry tags. Events
are paged by liquidity descending; paging stops as soon as an event's
liquidity falls below the client's liquidity floor (an event's liquidity
is the sum of its markets, so no later market can pass the floor either);
without a floor every active event is loaded.
"""

import json

from backend.app.clients.base import BaseClient, NormalizedMarket, clean_tags, parse_datetime, parse_float

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
POLYMARKET_WEB_URL = "https://polymarket.com"
PAGE_SIZE = 100


class PolymarketClient(BaseClient):
    source = "polymarket"

    def _fetch_raw(self) -> list:
        items: list = []
        for page in range(self.max_pages):
            events = self._get_json(
                GAMMA_EVENTS_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "liquidity",
                    "ascending": "false",
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                },
            )
            if not isinstance(events, list) or not events:
                break
            if not self._collect(events, items) or len(events) < PAGE_SIZE:
                break
        return items

    def _collect(self, events: list, items: list) -> bool:
        """Append the events' markets (with tags); False once liquidity drops below the floor."""
        floor = self.liquidity_floor or 0.0
        for event in events:
            if not isinstance(event, dict):
                continue
            if (parse_float(event.get("liquidity")) or 0.0) < floor:
                return False
            tags = clean_tags(event.get("tags"))
            parent = [{"slug": event.get("slug")}]
            for market in event.get("markets") or []:
                if isinstance(market, dict):
                    items.append({**market, "tags": tags, "events": parent})
        return True

    def _normalize(self, item: dict) -> NormalizedMarket | None:
        if not isinstance(item, dict):
            return None
        external_id = item.get("id")
        title = item.get("question")
        if not external_id or not title:
            return None
        tags = clean_tags(item.get("tags"))
        return NormalizedMarket(
            source=self.source,
            external_id=str(external_id),
            title=str(title),
            category=tags[0] if tags else (item.get("category") or None),
            tags=tags,
            price=self._yes_price(item),
            volume=parse_float(item.get("volumeNum", item.get("volume"))),
            liquidity=parse_float(item.get("liquidityNum", item.get("liquidity"))),
            end_date=parse_datetime(item.get("endDate")),
            url=self._market_url(item),
            volume_24h=parse_float(item.get("volume24hr")),
            best_bid=parse_float(item.get("bestBid")),
            best_ask=parse_float(item.get("bestAsk")),
            active=self._active(item),
        )

    @staticmethod
    def _active(item: dict) -> bool | None:
        """Gamma flags: tradeable when ``active`` and not ``closed``; None if neither is reported."""
        active, closed = item.get("active"), item.get("closed")
        if active is None and closed is None:
            return None
        return bool(active if active is not None else True) and not bool(closed)

    @staticmethod
    def _market_url(item: dict) -> str | None:
        """Web link to the market; falls back to the parent event page."""
        slug = item.get("slug")
        if slug:
            return f"{POLYMARKET_WEB_URL}/market/{slug}"
        events = item.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            event_slug = events[0].get("slug")
            if event_slug:
                return f"{POLYMARKET_WEB_URL}/event/{event_slug}"
        return None

    @staticmethod
    def _yes_price(item: dict) -> float | None:
        """Price of the first (Yes) outcome; outcomePrices is a JSON-encoded list."""
        prices = item.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except ValueError:
                return None
        if isinstance(prices, list) and prices:
            return parse_float(prices[0])
        return parse_float(item.get("lastTradePrice"))
