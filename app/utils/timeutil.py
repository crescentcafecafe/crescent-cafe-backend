from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def local_day_bounds(d: date, tzname: str) -> tuple[datetime, datetime]:
    z = tz(tzname)
    start = datetime.combine(d, time.min, tzinfo=z)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def range_bounds(start: date, end: date, tzname: str) -> tuple[datetime, datetime]:
    """Inclusive date range [start, end] in cafe-local time -> UTC half-open interval."""
    return local_day_bounds(start, tzname)[0], local_day_bounds(end, tzname)[1]


def to_local(dt: datetime, tzname: str) -> datetime:
    return dt.astimezone(tz(tzname))
