"""Predefined indices for the dashboard.

Seeds only into an empty rule table: once any index exists (adjusted,
renamed or deliberately deleted), seeding changes nothing, so deleted
indices never come back. Run as a module: ``python -m backend.app.seed``.
"""

from sqlalchemy.orm import Session

from backend.app.db import make_engine, make_session_factory
from backend.app.engine import list_rules, upsert_rule
from backend.app.models import MIN_LIQUIDITY_USD

# Tags match the labels/categories delivered by the sources (Polymarket event
# tags, Kalshi event categories); keywords narrow the titles additionally.
PREDEFINED_INDICES = [
    {
        "index_name": "global-politics",
        "categories": ["Politics", "Geopolitics", "Elections", "World", "Global Elections", "US Election", "Middle East"],
        "keywords": [
            "election", "president", "nomination", "government", "war", "sanctions", "nato",
            "trump", "congress", "senate", "house", "ceasefire", "israel", "iran", "ukraine", "russia", "china",
        ],
        "min_liquidity": MIN_LIQUIDITY_USD,
    },
    {
        "index_name": "global-economics",
        "categories": ["Economics", "Economy", "Finance", "Financials", "Business", "Fed", "Commodities"],
        "keywords": [
            "fed", "inflation", "recession", "gdp", "interest rate", "tariff", "economy",
            "rate cut", "unemployment", "cpi", "treasury", "debt", "jobs",
        ],
        "min_liquidity": MIN_LIQUIDITY_USD,
    },
]


def seed_indices(session: Session) -> list[str]:
    """Create the predefined indices if no index exists yet; return the names created."""
    if list_rules(session):
        return []
    for spec in PREDEFINED_INDICES:
        upsert_rule(session, **spec)
    return [spec["index_name"] for spec in PREDEFINED_INDICES]


def main() -> None:
    with make_session_factory(make_engine())() as session:
        created = seed_indices(session)
    print(f"created: {created or 'nothing, indices already present'}")


if __name__ == "__main__":
    main()
