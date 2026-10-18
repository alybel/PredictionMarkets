"""Group markets: all markets of one market group (source event, e.g. the hundreds of S&P 500 brackets) as one row.

A market without a stored group stands alone: its key is ``market:<external id>``, so it is a group of one.
The group market lists the number of members, their statuses and the span from the first listing to the last end date.
"""

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Session

from backend.app.directory.models import ACTIVE, EXPIRED, RESOLVED, DirectoryMarket
from backend.app.quality.pit import as_utc

SINGLE = "market:"
d = DirectoryMarket
GROUP_KEY = func.coalesce(d.group_key, literal(SINGLE).concat(d.external_id))


def _iso(value) -> str | None:
    value = as_utc(value)
    return value.isoformat() if value else None


def filtered(q: str = "", source: str = "", status: str = "", group: str = ""):
    """Directory markets matching search text (title, id, group title), source, status and group key."""
    query = select(d)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(or_(func.lower(d.title).like(like), func.lower(d.external_id).like(like), func.lower(d.group_title).like(like)))
    if source:
        query = query.where(d.source == source)
    if status:
        query = query.where(d.status == status)
    if group.startswith(SINGLE):
        query = query.where(d.group_key.is_(None), d.external_id == group[len(SINGLE):])
    elif group:
        query = query.where(d.group_key == group)
    return query


def list_groups(session: Session, q: str = "", source: str = "", status: str = "", limit: int = 50, offset: int = 0) -> dict:
    """Group markets of all matching markets, newest listing first."""
    m = filtered(q, source, status).add_columns(GROUP_KEY.label("gkey")).subquery().c
    counts = [func.sum(case((m.status == s, 1), else_=0)) for s in (ACTIVE, EXPIRED, RESOLVED)]
    query = select(m.source, m.gkey, func.max(m.group_title), func.min(m.title), func.count(), *counts, func.min(m.listed_at),
                   func.max(m.end_date)).group_by(m.source, m.gkey)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(query.order_by(func.max(m.listed_at).desc().nulls_last(), m.source, m.gkey).limit(limit).offset(offset))
    groups = [{"source": src, "key": key, "title": gtitle or title, "markets": n, "single": key.startswith(SINGLE),
               "statuses": {ACTIVE: a or 0, EXPIRED: e or 0, RESOLVED: r or 0}, "listed_at": _iso(first), "end_date": _iso(end)}
              for src, key, gtitle, title, n, a, e, r, first, end in rows]
    return {"total": total or 0, "limit": limit, "offset": offset, "groups": groups}
