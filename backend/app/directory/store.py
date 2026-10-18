"""Store directory entries of one source page by page (one transaction per page).

A known market is changed only when the source reports different data (missing new values never erase stored
ones), so a page without news touches nothing. The first run of a source is its baseline: nothing is marked new.
Afterwards a market seen for the first time is marked ``is_new`` when it was listed after the baseline (minus one
day of grace) or has no listing date; older markets reached later by the history crawl are not "new".
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.directory.models import DirectoryMarket
from backend.app.directory.parse import Entry
from backend.app.quality.pit import as_utc

NEW_GRACE = timedelta(days=1)


def changes(row: DirectoryMarket, e: Entry) -> dict:
    """Fields the source now reports differently."""
    new = {"title": e.title, "tags": list(e.tags) or None, "listed_at": e.listed_at, "end_date": e.end_date, "status": e.status,
           "group_key": e.group_key, "group_title": e.group_title}
    out = {}
    for key, value in new.items():
        old = getattr(row, key)
        same = as_utc(old) == as_utc(value) if isinstance(value, datetime) else old == value
        if value is not None and not same:
            out[key] = value
    return out


class Store:
    def __init__(self, session: Session, source: str, run_id: int):
        self.session, self.source, self.run_id = session, source, run_id
        d = DirectoryMarket
        self.known = {r.external_id: r for r in session.scalars(select(d).where(d.source == source))}
        self.baseline_at = as_utc(session.scalar(select(func.min(d.first_seen_at)).where(d.source == source)))
        self.seen: set[str] = set()
        self.counts = {"new": 0, "marked_new": 0, "changed": 0, "unchanged": 0, "duplicates": 0, "baseline": self.baseline_at is None}

    def _is_new(self, e: Entry) -> bool:
        if self.baseline_at is None:
            return False
        return e.listed_at is None or as_utc(e.listed_at) >= self.baseline_at - NEW_GRACE

    def add(self, entries: list[Entry], now: datetime) -> int:
        """Store one page; returns the number of new or changed markets on it."""
        before = self.counts["new"] + self.counts["changed"]
        for e in entries:
            duplicate = e.external_id in self.seen  # seen in another listing of this run: may still add tags or a status
            self.seen.add(e.external_id)
            row = self.known.get(e.external_id)
            if row is None:
                is_new = self._is_new(e)
                row = self.known[e.external_id] = DirectoryMarket(
                    source=self.source, external_id=e.external_id, title=e.title, tags=list(e.tags), listed_at=e.listed_at,
                    end_date=e.end_date, status=e.status, group_key=e.group_key, group_title=e.group_title, first_seen_at=now, first_seen_run=self.run_id, is_new=is_new)
                self.session.add(row)
                self.counts["new"] += 1
                self.counts["marked_new"] += is_new
                continue
            diff = changes(row, e)
            if diff and row.first_seen_run == self.run_id:  # created by another listing of this run: just complete it
                for key, value in diff.items():
                    setattr(row, key, value)
                self.counts["duplicates"] += 1
            elif diff:
                for key, value in diff.items():
                    setattr(row, key, value)
                row.updated_at = now
                self.counts["changed"] += 1
            else:
                self.counts["duplicates" if duplicate else "unchanged"] += 1
        self.session.commit()
        return self.counts["new"] + self.counts["changed"] - before
