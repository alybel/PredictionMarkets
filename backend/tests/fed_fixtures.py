"""Fixture payloads of the Fed decision markets (shapes copied from real Kalshi / Polymarket answers), no network."""

from datetime import date, datetime, time, timezone

SESSION = date(2025, 12, 10)
NOW = datetime(2025, 12, 12, 12, tzinfo=timezone.utc)
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"


def ts(d: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(d, time(hour, minute), tzinfo=timezone.utc).timestamp())


KALSHI_EVENT = {"event_ticker": "KXFEDDECISION-25DEC", "title": "Fed decision in Dec 2025?", "strike_date": "2025-12-10T19:00:00Z"}


def kalshi_market(suffix: str, strike: dict, label: str) -> dict:
    return {"ticker": f"KXFEDDECISION-25DEC-{suffix}", "event_ticker": "KXFEDDECISION-25DEC", "custom_strike": strike,
            "yes_sub_title": label, "title": f"Will the Federal Reserve {label} at their December 2025 meeting?",
            "open_time": "2025-12-01T14:00:00Z", "close_time": "2025-12-10T18:59:00Z", "status": "finalized"}


KALSHI_MARKETS = [kalshi_market("C26", {"Cut": ">25"}, "Cut >25bps"), kalshi_market("C25", {"Cut": "25"}, "Cut 25bps"),
                  kalshi_market("H0", {"Hike": "0"}, "Fed maintains rate"), kalshi_market("H25", {"Hike": "25"}, "Hike 25bps")]


def live_candle(d: date, close: str, volume: str = "100.00", hour: int = 5) -> dict:
    return {"end_period_ts": ts(d, hour), "price": {"close_dollars": close, "previous_dollars": close},
            "volume_fp": volume, "yes_bid": {"close_dollars": close}, "yes_ask": {"close_dollars": close}}


def historical_candle(d: date, close: str | None, volume: str = "10.00", bid: str = "0.40", ask: str = "0.50") -> dict:
    return {"end_period_ts": ts(d, 5), "price": {"close": close, "previous": None}, "volume": volume,
            "yes_bid": {"close": bid}, "yes_ask": {"close": ask}}


# prices per day and outcome: Dec 8 sums to 100 %, Dec 9 to 108 %
KALSHI_PRICES = {"C26": ("0.05", "0.05"), "C25": ("0.25", "0.30"), "H0": ("0.70", "0.72"), "H25": ("0.00", "0.01")}
DAYS = (date(2025, 12, 8), date(2025, 12, 9))


def kalshi_candles(suffix: str) -> dict:
    return {"ticker": f"KXFEDDECISION-25DEC-{suffix}",
            "candlesticks": [live_candle(d, KALSHI_PRICES[suffix][i]) for i, d in enumerate(DAYS)]}


POLY_EVENT = {
    "id": "35090", "slug": "fed-decision-in-december", "title": "Fed decision in December?", "endDate": "2025-12-10T00:00:00Z",
    "markets": [
        {"id": "570360", "groupItemTitle": "50+ bps decrease", "question": "Fed decreases interest rates by 50+ bps after December 2025 meeting?",
         "clobTokenIds": '["tok-m50", "tok-m50-no"]'},
        {"id": "570361", "groupItemTitle": "25 bps decrease", "question": "Fed decreases interest rates by 25 bps after December 2025 meeting?",
         "clobTokenIds": '["tok-m25", "tok-m25-no"]'},
        {"id": "570362", "groupItemTitle": "No change", "question": "No change in Fed interest rates after December 2025 meeting?",
         "clobTokenIds": '["tok-0", "tok-0-no"]'},
        {"id": "570363", "groupItemTitle": "25+ bps increase", "question": "Fed increases interest rates by 25+ bps after December 2025 meeting?",
         "clobTokenIds": '["tok-p25", "tok-p25-no"]'},
        {"id": "507418", "groupItemTitle": "Other", "question": "Other?", "clobTokenIds": '["tok-x", "tok-x-no"]'},
    ],
}
POLY_PRICES = {"tok-m50": (0.06, 0.05), "tok-m25": (0.30, 0.25), "tok-0": (0.64, 0.70), "tok-p25": (0.00, 0.00)}


def poly_history(token: str) -> dict:
    return {"history": [{"t": ts(d, 0, 1), "p": POLY_PRICES[token][i]} for i, d in enumerate(DAYS)]}


class FakeApi:
    """JSON getter for ``HttpFedSource``: answers the listing and history URLs from the fixtures."""

    def __init__(self, broken: set[str] = frozenset()):
        self.broken = broken
        self.calls: list[str] = []

    def __call__(self, url: str, params: dict | None = None):
        self.calls.append(url)
        if any(b in url or b == (params or {}).get("market") for b in self.broken):
            raise ValueError("broken answer")
        if url.endswith("/events") and "kalshi" in url:
            return {"events": [KALSHI_EVENT], "cursor": ""}
        if url.endswith("/markets") and "kalshi" in url:
            return {"markets": KALSHI_MARKETS, "cursor": ""}
        if "/candlesticks" in url and "/series/" in url:
            return kalshi_candles(url.split("/markets/")[1].split("/")[0].rsplit("-", 1)[1])
        if "gamma-api" in url:
            return [POLY_EVENT] if (params or {}).get("offset", 0) == 0 else []
        if "prices-history" in url:
            return poly_history(params["market"])
        raise AssertionError(f"unexpected url {url}")
