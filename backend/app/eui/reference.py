"""Reference values for range markets ("Fed funds 4.25-4.50 % after December?").

Whether such a range means an increase or a decrease depends on the current
value of the indicator. The values are maintained by hand (admin endpoint);
the indicator list is seeded once so the editor is never empty.
"""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.eui.models import ReferenceValue

logger = logging.getLogger(__name__)

DEFAULT_INDICATORS = [
    ("fed_funds_upper", "Fed funds target range, upper bound", "%"),
    ("ecb_deposit_rate", "ECB deposit facility rate", "%"),
    ("boe_bank_rate", "Bank of England bank rate", "%"),
    ("boj_policy_rate", "Bank of Japan policy rate", "%"),
    ("us_cpi_yoy", "US CPI inflation, year over year", "%"),
    ("us_core_pce_yoy", "US core PCE inflation, year over year", "%"),
    ("us_unemployment", "US unemployment rate", "%"),
    ("us_10y_yield", "US 10-year Treasury yield", "%"),
    ("us_gdp_growth_qoq_ann", "US real GDP growth, annualized quarter over quarter", "%"),
    ("brent_usd", "Brent crude oil price", "USD"),
]


def seed_reference_indicators(session: Session) -> int:
    """Insert the default indicators without a value once; returns the number added."""
    known = {r.indicator for r in session.scalars(select(ReferenceValue))}
    added = 0
    for indicator, label, unit in DEFAULT_INDICATORS:
        if indicator not in known:
            session.add(ReferenceValue(indicator=indicator, label=label, unit=unit))
            added += 1
    if added:
        session.commit()
        logger.info("seeded %d reference indicators (values still empty)", added)
    return added


def list_reference_values(session: Session) -> list[ReferenceValue]:
    return list(session.scalars(select(ReferenceValue).order_by(ReferenceValue.indicator)))


def upsert_reference_value(session: Session, indicator: str, value: float | None, unit: str | None = None,
                           as_of: date | None = None, note: str | None = None, label: str | None = None) -> ReferenceValue:
    row = session.scalar(select(ReferenceValue).where(ReferenceValue.indicator == indicator))
    if row is None:
        row = ReferenceValue(indicator=indicator, label=label or indicator, unit=unit or "%")
        session.add(row)
    row.value = value
    if unit:
        row.unit = unit
    if label:
        row.label = label
    row.as_of = as_of
    row.note = note
    session.commit()
    logger.info("reference value %s = %s %s (as of %s)", indicator, value, row.unit, as_of)
    return row


def reference_payload(session: Session) -> list[dict]:
    """Indicators with a value, in the form handed to the model (and the API)."""
    return [
        {"indicator": r.indicator, "label": r.label, "value": r.value, "unit": r.unit,
         "as_of": r.as_of.isoformat() if r.as_of else None}
        for r in list_reference_values(session)
        if r.value is not None
    ]
