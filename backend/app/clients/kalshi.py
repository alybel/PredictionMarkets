"""Kalshi client (public trade API v2).

Markets are loaded via ``/events`` with nested markets, so every market
inherits its event's category as tag. The endpoint has no sort order and
no category filter, so paging is capped by ``max_pages``.

Field notes: the API quotes prices, volume and liquidity in dollar fields
(``*_dollars`` / ``*_fp``); legacy cent fields are still accepted. The
public API currently reports ``liquidity_dollars`` as 0 for all markets,
so open interest (contracts x 1 USD notional) serves as liquidity proxy
whenever the reported liquidity is missing or zero.

``volume_24h`` is converted from contracts to USD with the current price
(a contract pays 1 USD, so contracts x price is the dollar turnover);
``best_bid``/``best_ask`` come from the yes side in dollars (legacy cents / 100).
"""

from backend.app.clients.base import BaseClient, NormalizedMarket, clean_tags, parse_datetime, parse_float

KALSHI_EVENTS_URL = "https://api.elections.kalshi.com/trade-api/v2/events"
KALSHI_WEB_URL = "https://kalshi.com"
PAGE_SIZE = 200


class KalshiClient(BaseClient):
    source = "kalshi"
    max_pages = 10

    def _fetch_raw(self) -> list:
        items: list = []
        cursor = None
        for _ in range(self.max_pages):
            params = {"status": "open", "limit": PAGE_SIZE, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            data = self._get_json(KALSHI_EVENTS_URL, params=params)
            if not isinstance(data, dict):
                break
            events = data.get("events")
            if not isinstance(events, list) or not events:
                break
            for event in events:
                if isinstance(event, dict):
                    category = event.get("category")
                    for market in event.get("markets") or []:
                        if isinstance(market, dict):
                            items.append({**market, "category": category})
            cursor = data.get("cursor")
            if not cursor:
                break
        return items

    def _normalize(self, item: dict) -> NormalizedMarket | None:
        if not isinstance(item, dict):
            return None
        external_id = item.get("ticker")
        title = item.get("title")
        if not external_id or not title:
            return None
        tags = clean_tags([item.get("category")])
        return NormalizedMarket(
            source=self.source,
            external_id=str(external_id),
            title=str(title),
            category=tags[0] if tags else None,
            tags=tags,
            price=self._yes_price(item),
            volume=self._first_float(item, "volume_fp", "volume"),
            liquidity=self._liquidity(item),
            end_date=parse_datetime(item.get("close_time")),
            url=f"{KALSHI_WEB_URL}/markets/{external_id}",
            volume_24h=self._volume_24h_usd(item),
            best_bid=self._price_field(item, "yes_bid_dollars", "yes_bid"),
            best_ask=self._price_field(item, "yes_ask_dollars", "yes_ask"),
            active=(item["status"] == "active") if isinstance(item.get("status"), str) else None,
        )

    @classmethod
    def _price_field(cls, item: dict, dollar_key: str, cent_key: str) -> float | None:
        dollars = parse_float(item.get(dollar_key))
        if dollars is not None:
            return dollars
        cents = parse_float(item.get(cent_key))
        return cents / 100.0 if cents is not None else None

    @classmethod
    def _volume_24h_usd(cls, item: dict) -> float | None:
        """24h turnover in USD: contracts traded x current price (1 USD payout per contract)."""
        contracts = cls._first_float(item, "volume_24h_fp", "volume_24h")
        price = cls._yes_price(item)
        if contracts is None or price is None:
            return None
        return contracts * price

    @staticmethod
    def _first_float(item: dict, *keys: str) -> float | None:
        for key in keys:
            value = parse_float(item.get(key))
            if value is not None:
                return value
        return None

    @classmethod
    def _yes_price(cls, item: dict) -> float | None:
        """Dollar price (0-1) when present; legacy cent fields are normalized to 0-1."""
        dollars = cls._first_float(item, "last_price_dollars", "yes_bid_dollars")
        if dollars is not None:
            return dollars
        cents = cls._first_float(item, "last_price", "yes_bid")
        return cents / 100.0 if cents is not None else None

    @classmethod
    def _liquidity(cls, item: dict) -> float | None:
        """Reported liquidity in USD, else open interest as proxy (see module docstring)."""
        dollars = parse_float(item.get("liquidity_dollars"))
        if dollars is None:
            cents = parse_float(item.get("liquidity"))
            dollars = cents / 100.0 if cents is not None else None
        if dollars:
            return dollars
        open_interest = parse_float(item.get("open_interest_fp"))
        return open_interest if open_interest is not None else dollars
