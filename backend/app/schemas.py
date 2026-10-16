"""Request bodies for index rules (preview of the read-only archive) and dashboard log events.

Range rules raise HTTP 400 (invalid values); type errors stay FastAPI's 422.
"""

from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.app.models import DEFAULT_MAX_PRICE, DEFAULT_MIN_PRICE, MIN_LIQUIDITY_USD


class RuleSpec(BaseModel):
    """Parameters of an index rule as edited in the dashboard."""

    categories: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    min_liquidity: float = MIN_LIQUIDITY_USD
    min_price: float = DEFAULT_MIN_PRICE
    max_price: float = DEFAULT_MAX_PRICE

    def validated(self) -> dict:
        """Cleaned rule fields; raises 400 on values outside the allowed ranges."""
        if self.min_liquidity < MIN_LIQUIDITY_USD:
            raise HTTPException(400, f"min_liquidity must be at least {MIN_LIQUIDITY_USD:.0f} USD")
        if not 0 <= self.min_price < self.max_price <= 1:
            raise HTTPException(400, "price corridor must satisfy 0 <= min_price < max_price <= 1")
        return {
            "categories": _clean_list(self.categories),
            "keywords": _clean_list(self.keywords),
            "min_liquidity": self.min_liquidity,
            "min_price": self.min_price,
            "max_price": self.max_price,
        }


class ClientLog(BaseModel):
    """A dashboard event (button click, filter change, error) forwarded to the server log."""

    level: str = "info"
    event: str = Field(min_length=1, max_length=120)
    detail: dict | None = None


def _clean_list(values: list[str]) -> list[str]:
    """Trimmed, non-empty, case-insensitively unique entries in original order."""
    seen: set[str] = set()
    cleaned = []
    for value in values:
        item = value.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            cleaned.append(item)
    return cleaned
