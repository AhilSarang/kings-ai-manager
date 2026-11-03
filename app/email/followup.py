"""Business-day arithmetic for follow-ups (America/New_York, weekends skipped)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.services.formatting import tz


def add_business_days(start: datetime, days: int) -> datetime:
    local = start.astimezone(tz())
    added = 0
    while added < days:
        local += timedelta(days=1)
        if local.weekday() < 5:
            added += 1
    while local.weekday() >= 5:  # started on a weekend with days == 0
        local += timedelta(days=1)
    return local


def business_days_between(start: datetime, end: datetime) -> int:
    a, b = start.astimezone(tz()).date(), end.astimezone(tz()).date()
    if b <= a:
        return 0
    count, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count
