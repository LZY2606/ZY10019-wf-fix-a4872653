"""Timezone stepping component.

Localizes naive candidate datetimes produced by the date matcher back into
the iterator's timezone, resolving DST gaps (nonexistent local times) and
overlaps (ambiguous local times). This component is deliberately free of any
cron-field knowledge so it can be tested in isolation.
"""

import datetime

from dateutil.tz import datetime_exists

from ._calendar import UTC_DT


def _is_successor(
    date: datetime.datetime, previous_date: datetime.datetime, is_prev: bool
) -> bool:
    """Check if the given date is a successor (after/before) of the previous date."""
    if is_prev:
        return date.astimezone(UTC_DT) < previous_date.astimezone(UTC_DT)
    return date.astimezone(UTC_DT) > previous_date.astimezone(UTC_DT)


def _timezone_delta(date1: datetime.datetime, date2: datetime.datetime) -> datetime.timedelta:
    """Calculate the timezone difference of the given dates."""
    offset1 = date1.utcoffset()
    offset2 = date2.utcoffset()
    assert offset1 is not None
    assert offset2 is not None
    return offset2 - offset1


def _add_tzinfo(
    date: datetime.datetime, previous_date: datetime.datetime, is_prev: bool
) -> tuple[datetime.datetime, bool]:
    """Add the tzinfo from the previous date to the given date.

    In case the new date is ambiguous, determine the correct date
    based on it being closer to the previous date but still a successor
    (after/before based on `is_prev`).

    In case the date does not exist, jump forward to the next existing date.
    """
    localize = getattr(previous_date.tzinfo, "localize", None)
    if localize is not None:
        # pylint: disable-next=import-outside-toplevel
        import pytz

        try:
            result = localize(date, is_dst=None)
        except pytz.NonExistentTimeError:
            while True:
                date += datetime.timedelta(minutes=1)
                try:
                    result = localize(date, is_dst=None)
                except pytz.NonExistentTimeError:
                    continue
                break
            return result, False
        except pytz.AmbiguousTimeError:
            closer = localize(date, is_dst=not is_prev)
            farther = localize(date, is_dst=is_prev)
            # TODO: Check negative DST
            assert (closer.astimezone(UTC_DT) > farther.astimezone(UTC_DT)) == is_prev
            if _is_successor(closer, previous_date, is_prev):
                result = closer
            else:
                assert _is_successor(farther, previous_date, is_prev)
                result = farther
        return result, True

    result = date.replace(fold=1 if is_prev else 0, tzinfo=previous_date.tzinfo)
    if not datetime_exists(result):
        while not datetime_exists(result):
            result += datetime.timedelta(minutes=1)
        return result, False

    # result is closer to the previous date
    farther = date.replace(fold=0 if is_prev else 1, tzinfo=previous_date.tzinfo)
    # Comparing the UTC offsets in the check for the date being ambiguous.
    if result.utcoffset() != farther.utcoffset():
        # TODO: Check negative DST
        assert (result.astimezone(UTC_DT) > farther.astimezone(UTC_DT)) == is_prev
        if not _is_successor(result, previous_date, is_prev):
            assert _is_successor(farther, previous_date, is_prev)
            result = farther
    return result, True


def resolve_aware_time(now, unaware_time, is_prev, wild_hours, recalc):
    """Attach the timezone of `now` to a naive candidate, handling DST changes.

    `recalc` re-runs the naive date matcher from a new naive start point; it
    is used both to skip past nonexistent local times and to look for an
    alternative candidate on the other side of a UTC-offset change.
    """
    aware_time, exists = _add_tzinfo(unaware_time, now, is_prev)

    if not exists and (not _is_successor(aware_time, now, is_prev) or wild_hours):
        # The calculated local date does not exist and moving the time forward
        # to the next valid time isn't the correct solution. Search for the
        # next matching cron time that exists.
        while not exists:
            unaware_time = recalc(unaware_time)
            aware_time, exists = _add_tzinfo(unaware_time, now, is_prev)

    offset_delta = _timezone_delta(now, aware_time)
    if not offset_delta:
        # There was no DST change.
        return aware_time

    # There was a DST change. So check if there is a alternative cron time
    # for the other UTC offset.
    alternative_unaware_time = now.replace(tzinfo=None) + offset_delta
    alternative_unaware_time = recalc(alternative_unaware_time)
    alternative_aware_time, exists = _add_tzinfo(alternative_unaware_time, now, is_prev)

    if not _is_successor(alternative_aware_time, now, is_prev):
        # The alternative time is an ancestor of now. Thus it is not an alternative.
        return aware_time

    if _is_successor(aware_time, alternative_aware_time, is_prev):
        return alternative_aware_time

    return aware_time
