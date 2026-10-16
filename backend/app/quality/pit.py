"""Point-in-time rules: no stored value may reach an analysis before (or on another day than) it was observable.

* A market snapshot row of day D belongs to D only if it was observed on D (UTC) and before the US close
  of D — the correlation pairs the index of D with the stock return close(D) → close(next day), so
  anything seen after that close would be look-ahead. Live rows are observed at ``fetched_at``.
  Backfilled rows come from the sources' daily history points; the parsers keep only points observed on
  their UTC day before the cutoff (Polymarket ~00:00, Kalshi 04:00 UTC), so they satisfy the rule by construction.
* A stock close of day D is final only once it was fetched after the close of D; earlier fetches store an
  intraday price as "close" (provisional) and never enter an analysis until the next refresh repairs them.
* Nothing dated after today exists for an analysis.
"""

from datetime import date, datetime, time, timedelta, timezone

BACKFILL = "backfill"  # exclusion_reason of backfilled snapshot rows
US_CLOSE_CUTOFF_UTC = time(20)  # 16:00 New York in summer (EDT); in winter the close is 21:00 UTC, so always at or before it
FINAL_CLOSE_UTC = time(22)      # a close fetched from 22:00 UTC on is final all year (>= 1 h after the close)

FUTURE_DATE = "future_date"
OUTSIDE_DAY = "outside_day"
AFTER_CLOSE = "after_close"
PROVISIONAL_CLOSE = "provisional_close"


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; every stored timestamp is UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def at_utc(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=timezone.utc)


def observation_cutoff(day: date) -> datetime:
    """Latest moment (exclusive) at which a value may be observed to count for ``day``."""
    return at_utc(day, US_CLOSE_CUTOFF_UTC)


def is_observable(observed: datetime, day: date) -> bool:
    """Observed on ``day`` (UTC) and before its US-close cutoff."""
    observed = as_utc(observed)
    return at_utc(day, time.min) <= observed < observation_cutoff(day)


def snapshot_violation(snapshot_date: date, fetched_at: datetime | None, exclusion_reason: str | None,
                       today: date) -> str | None:
    """Why a snapshot row must not enter an analysis for its day, or None if it may."""
    if snapshot_date > today:
        return FUTURE_DATE
    if exclusion_reason == BACKFILL or fetched_at is None:
        return None  # observation time guaranteed by the backfill parsers (see module doc)
    fetched = as_utc(fetched_at)
    if fetched.date() != snapshot_date:
        return OUTSIDE_DAY
    return None if fetched < observation_cutoff(snapshot_date) else AFTER_CLOSE


def is_final_close(price_date: date, fetched_at: datetime | None) -> bool:
    return fetched_at is None or as_utc(fetched_at) >= at_utc(price_date, FINAL_CLOSE_UTC)


def price_violation(price_date: date, fetched_at: datetime | None, today: date) -> str | None:
    if price_date > today:
        return FUTURE_DATE
    return None if is_final_close(price_date, fetched_at) else PROVISIONAL_CLOSE


def last_final_day(now: datetime) -> date:
    """Latest price date whose close is final at ``now``."""
    now = as_utc(now)
    return now.date() if now >= at_utc(now.date(), FINAL_CLOSE_UTC) else now.date() - timedelta(days=1)
