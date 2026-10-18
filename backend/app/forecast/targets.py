"""Target series of the forecast report: S&P 500, 10-year US yield, TLT and VIX (Yahoo tickers).

Their daily closes live in ``stock_prices`` next to the top-100 stocks (same loader, same point-in-time rules);
every top-100 analysis filters by ``TOP_US_STOCKS``, so these rows never reach it.
"""

from dataclasses import dataclass

RETURN, LEVEL = "return", "level"  # outcome: relative change of a price, or difference of a level


@dataclass(frozen=True)
class Target:
    ticker: str
    name: str
    kind: str      # RETURN | LEVEL
    scale: float   # multiplies the outcome (LEVEL: ^TNX quotes percent -> ×100 = bp)
    unit: str
    crash: bool    # crash hit rate applies (price series only)


TARGETS = (
    Target("^GSPC", "S&P 500", RETURN, 1.0, "Rendite", True),
    Target("^TNX", "10-jährige US-Rendite", LEVEL, 100.0, "bp", False),
    Target("TLT", "US-Staatsanleihen 20+ J. (TLT)", RETURN, 1.0, "Rendite", True),
    Target("^VIX", "VIX (Volatilität)", LEVEL, 1.0, "Pkt.", False),
)
TICKERS = [t.ticker for t in TARGETS]
