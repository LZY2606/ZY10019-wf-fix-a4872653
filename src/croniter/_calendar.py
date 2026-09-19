"""Shared date/time primitives used by the internal croniter components."""

import datetime

UTC_DT = datetime.timezone.utc
EPOCH = datetime.datetime.fromtimestamp(0, UTC_DT)

DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def datetime_to_timestamp(d):
    if d.tzinfo is not None:
        d = d.replace(tzinfo=None) - d.utcoffset()

    return (d - datetime.datetime(1970, 1, 1)).total_seconds()


def _is_leap(year: int) -> bool:
    return year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)


def _last_day_of_month(year: int, month: int) -> int:
    """Calculate the last day of the given month (honor leap years)."""
    last_day = DAYS[month - 1]
    if month == 2 and _is_leap(year):
        last_day += 1
    return last_day
