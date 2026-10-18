"""Quality grade of one stored series: each issue found lowers the grade (good -> fair -> poor)."""

from datetime import date

MIN_DAYS = 7          # shorter histories carry too little for an index or a correlation
MIN_COVERAGE = 0.9    # share of calendar days between first and last stored day that have a row


def grade(issues: list[str]) -> str:
    return "good" if not issues else "fair" if len(issues) == 1 else "poor"


def coverage(days: int, first: date | None, last: date | None) -> float | None:
    """Stored days / calendar days of the stored period."""
    if not first or not last:
        return None
    return days / ((last - first).days + 1)


def assess(issues: list[str]) -> dict:
    return {"grade": grade(issues), "issues": issues}
