"""Date candidate matching component.

Given the expanded cron fields produced by ``_fields``, finds the next (or
previous) datetime matching every field. The naive local-time search lives
here; DST gap/overlap resolution is delegated to the ``_tzstep`` component.
``DateMatcher`` is intentionally decoupled from the ``croniter`` class so it
can be instantiated and tested on its own.
"""

import calendar
import datetime

from dateutil.relativedelta import relativedelta

from . import _tzstep
from ._calendar import _last_day_of_month
from ._errors import CroniterBadDateError
from ._fields import (
    DAY_FIELD,
    DOW_FIELD,
    HOUR_FIELD,
    MINUTE_FIELD,
    MONTH_FIELD,
    SECOND_FIELD,
    UNIX_CRON_LEN,
    YEAR_CRON_LEN,
    YEAR_FIELD,
    re_star,
)

def get_next_nearest_diff(x, to_check, range_val):
    """
    `range_val` is the range of a field.
    If no available time, we can move to next loop(like next month).
    `range_val` can also be set to `None` to indicate that there is no loop.
    ( Currently, should only used for `year` field )
    """
    for i, d in enumerate(to_check):
        if range_val is not None:
            if d == "l":
                # if 'l' then it is the last day of month
                # => its value of range_val
                d = range_val
            elif d > range_val:
                continue
        if d >= x:
            return d - x
    # When range_val is None and x not exists in to_check,
    # `None` will be returned to suggest no more available time
    if range_val is None:
        return None
    return to_check[0] - x + range_val

def get_prev_nearest_diff(x, to_check, range_val):
    """
    `range_val` is the range of a field.
    If no available time, we can move to previous loop(like previous month).
    Range_val can also be set to `None` to indicate that there is no loop.
    ( Currently should only used for `year` field )
    """
    candidates = to_check[:]
    candidates.reverse()
    for d in candidates:
        if d != "l" and d <= x:
            return d - x
    if "l" in candidates:
        return -x
    # When range_val is None and x not exists in to_check,
    # `None` will be returned to suggest no more available time
    if range_val is None:
        return None
    candidate = candidates[0]
    for c in candidates:
        # fixed: c < range_val
        # this code will reject all 31 day of month, 12 month, 59 second,
        # 23 hour and so on.
        # if candidates has just a element, this will not harmful.
        # but candidates have multiple elements, then values equal to
        # range_val will rejected.
        if c <= range_val:
            candidate = c
            break
    # fix crontab "0 6 30 3 *" condidates only a element, then get_prev error
    # return 2021-03-02 06:00:00
    if candidate > range_val:
        return -range_val
    return candidate - x - range_val

def get_nth_weekday_of_month(year: int, month: int, day_of_week: int) -> tuple[int, ...]:
    """For a given year/month return a list of days in nth-day-of-month order.
    The last weekday of the month is always [-1].
    """
    w = (day_of_week + 6) % 7
    c = calendar.Calendar(w).monthdayscalendar(year, month)
    if c[0][0] == 0:
        c.pop(0)
    return tuple(i[0] for i in c)

def get_nearest_weekday(year, month, day):
    """Get the nearest weekday (Mon-Fri) to the given day in the given month.

    Rules:
    - If the day is a weekday, return it.
    - If Saturday, return Friday (day-1), unless that crosses into previous month,
      then return Monday (day+2).
    - If Sunday, return Monday (day+1), unless that crosses into next month,
      then return Friday (day-2).
    """
    last_day = _last_day_of_month(year, month)
    day = min(day, last_day)
    weekday = calendar.weekday(year, month, day)  # 0=Mon, 6=Sun
    if weekday < 5:  # Mon-Fri
        return day
    if weekday == 5:  # Saturday
        if day > 1:
            return day - 1  # Friday
        else:
            return day + 2  # Monday (1st is Sat, so 3rd is Mon)
    # Sunday
    if day < last_day:
        return day + 1  # Monday
    else:
        return day - 2  # Friday (last day is Sun, go back to Fri)

class DateMatcher:
    """Matches datetimes against expanded cron fields.

    Constructed from the expansion results of ``_fields.expand_expression``
    plus the behavioural options that influence matching. ``calc_next``
    reproduces the day-of-month/day-of-week union semantics; ``calc`` is the
    per-candidate field matcher (and the recursion unit for DST retries).
    """

    def __init__(
        self,
        expanded,
        nth_weekday_of_month,
        nearest_weekday,
        *,
        day_or=True,
        implement_cron_bug=False,
        expressions=(),
        max_years_between_matches=50,
        months_in_year=12,
        next_nearest_diff=None,
        prev_nearest_diff=None,
        nth_weekday_of_month_fn=None,
        nearest_weekday_fn=None,
    ):
        self.expanded = expanded
        self.nth_weekday_of_month = nth_weekday_of_month
        self.nearest_weekday = nearest_weekday
        self.day_or = day_or
        self.implement_cron_bug = implement_cron_bug
        self.expressions = expressions
        self.max_years_between_matches = max_years_between_matches
        self.months_in_year = months_in_year
        self._next_nearest_diff = next_nearest_diff or get_next_nearest_diff
        self._prev_nearest_diff = prev_nearest_diff or get_prev_nearest_diff
        self._nth_weekday_of_month_fn = nth_weekday_of_month_fn or get_nth_weekday_of_month
        self._nearest_weekday_fn = nearest_weekday_fn or get_nearest_weekday

    def calc_next(self, current, is_prev: bool) -> datetime.datetime:
        expanded = self.expanded[:]
        nth_weekday_of_month = self.nth_weekday_of_month.copy()

        # exception to support day of month and day of week as defined in cron
        if (expanded[DAY_FIELD][0] != "*" and expanded[DOW_FIELD][0] != "*") and self.day_or:
            # If requested, handle a bug in vixie cron/ISC cron where day_of_month and
            # day_of_week form an intersection (AND) instead of a union (OR) if either
            # field is an asterisk or starts with an asterisk (https://crontab.guru/cron-bug.html)
            if self.implement_cron_bug and (
                re_star.match(self.expressions[DAY_FIELD])
                or re_star.match(self.expressions[DOW_FIELD])
            ):
                # To produce a schedule identical to the cron bug, we'll bypass the code
                # that makes a union of DOM and DOW, and instead skip to the code that
                # does an intersect instead
                pass
            else:
                # Under OR semantics an unsatisfiable side -- the 31st in a month
                # that has no 31st, say -- contributes no dates rather than ruling
                # out the expression, so the other side must still be able to
                # match. Only both sides failing means there is no such date.
                #
                # That only holds while each side is a clean operand. '#' and 'W'
                # are not carried by the DAY/DOW fields but by nth_weekday_of_month
                # and self.nearest_weekday, so blanking the fields above does not
                # remove them and each side stays an intersection (the semantics
                # test_issue_k33 pins down). Swallowing a failure there would drop
                # the other field from the schedule instead, so let it propagate.
                clean_split = not nth_weekday_of_month and not self.nearest_weekday

                bak = expanded[DOW_FIELD]
                expanded[DOW_FIELD] = ["*"]
                try:
                    t1 = self.calc(current, expanded, nth_weekday_of_month, is_prev)
                except CroniterBadDateError:
                    if not clean_split:
                        raise
                    t1 = None
                expanded[DOW_FIELD] = bak
                expanded[DAY_FIELD] = ["*"]

                try:
                    t2 = self.calc(current, expanded, nth_weekday_of_month, is_prev)
                except CroniterBadDateError:
                    if not clean_split:
                        raise
                    t2 = None

                if t1 is None:
                    if t2 is None:
                        raise CroniterBadDateError(
                            "failed to find prev date" if is_prev else "failed to find next date"
                        )
                    return t2
                if t2 is None:
                    return t1
                if is_prev:
                    return t1 if t1 > t2 else t2
                return t1 if t1 < t2 else t2

        return self.calc(current, expanded, nth_weekday_of_month, is_prev)
    def calc(
        self,
        now: datetime.datetime,
        expanded,
        nth_weekday_of_month,
        is_prev: bool,
    ) -> datetime.datetime:
        if is_prev:
            nearest_diff_method = self._prev_nearest_diff
            offset = relativedelta(microseconds=-1)
        else:
            nearest_diff_method = self._next_nearest_diff
            if len(expanded) > UNIX_CRON_LEN:
                offset = relativedelta(seconds=1)
            else:
                offset = relativedelta(minutes=1)
        # Calculate the next cron time in local time a.k.a. timezone unaware time.
        unaware_time = now.replace(tzinfo=None) + offset
        if len(expanded) > UNIX_CRON_LEN:
            unaware_time = unaware_time.replace(microsecond=0)
        else:
            unaware_time = unaware_time.replace(second=0, microsecond=0)

        month = unaware_time.month
        year = current_year = unaware_time.year

        def proc_year(d):
            if len(expanded) == YEAR_CRON_LEN:
                try:
                    expanded[YEAR_FIELD].index("*")
                except ValueError:
                    # use None as range_val to indicate no loop
                    diff_year = nearest_diff_method(d.year, expanded[YEAR_FIELD], None)
                    if diff_year is None:
                        return None, d
                    if diff_year != 0:
                        if is_prev:
                            d += relativedelta(
                                years=diff_year, month=12, day=31, hour=23, minute=59, second=59
                            )
                        else:
                            d += relativedelta(
                                years=diff_year, month=1, day=1, hour=0, minute=0, second=0
                            )
                        return True, d
            return False, d

        def proc_month(d):
            try:
                expanded[MONTH_FIELD].index("*")
            except ValueError:
                diff_month = nearest_diff_method(
                    d.month, expanded[MONTH_FIELD], self.months_in_year
                )
                reset_day = 1

                if diff_month is not None and diff_month != 0:
                    if is_prev:
                        d += relativedelta(months=diff_month)
                        reset_day = _last_day_of_month(d.year, d.month)
                        d += relativedelta(day=reset_day, hour=23, minute=59, second=59)
                    else:
                        d += relativedelta(
                            months=diff_month, day=reset_day, hour=0, minute=0, second=0
                        )
                    return True, d
            return False, d

        def proc_day_of_month(d):
            try:
                expanded[DAY_FIELD].index("*")
            except ValueError:
                days = _last_day_of_month(year, month)
                if "l" in expanded[DAY_FIELD] and days == d.day:
                    return False, d

                if is_prev:
                    prev_month = (month - 2) % self.months_in_year + 1
                    prev_year = year - 1 if month == 1 else year
                    days_in_prev_month = _last_day_of_month(prev_year, prev_month)
                    diff_day = nearest_diff_method(d.day, expanded[DAY_FIELD], days_in_prev_month)
                else:
                    diff_day = nearest_diff_method(d.day, expanded[DAY_FIELD], days)

                if diff_day is not None and diff_day != 0:
                    if is_prev:
                        d += relativedelta(days=diff_day, hour=23, minute=59, second=59)
                    else:
                        d += relativedelta(days=diff_day, hour=0, minute=0, second=0)
                    return True, d
            return False, d

        def proc_day_of_week(d):
            try:
                expanded[DOW_FIELD].index("*")
            except ValueError:
                diff_day_of_week = nearest_diff_method(d.isoweekday() % 7, expanded[DOW_FIELD], 7)
                if diff_day_of_week is not None and diff_day_of_week != 0:
                    if is_prev:
                        d += relativedelta(days=diff_day_of_week, hour=23, minute=59, second=59)
                    else:
                        d += relativedelta(days=diff_day_of_week, hour=0, minute=0, second=0)
                    return True, d
            return False, d

        def proc_day_of_week_nth(d):
            if "*" in nth_weekday_of_month:
                s = nth_weekday_of_month["*"]
                for i in range(0, 7):
                    if i in nth_weekday_of_month:
                        nth_weekday_of_month[i].update(s)
                    else:
                        nth_weekday_of_month[i] = s
                del nth_weekday_of_month["*"]

            candidates = []
            for wday, nth in nth_weekday_of_month.items():
                c = self._nth_weekday_of_month_fn(d.year, d.month, wday)
                for n in nth:
                    if n == "l":
                        candidate = c[-1]
                    elif len(c) < n:
                        continue
                    else:
                        candidate = c[n - 1]
                    if (is_prev and candidate <= d.day) or (not is_prev and d.day <= candidate):
                        candidates.append(candidate)

            if not candidates:
                if is_prev:
                    d += relativedelta(days=-d.day, hour=23, minute=59, second=59)
                else:
                    days = _last_day_of_month(year, month)
                    d += relativedelta(days=(days - d.day + 1), hour=0, minute=0, second=0)
                return True, d

            candidates.sort()
            diff_day = (candidates[-1] if is_prev else candidates[0]) - d.day
            if diff_day != 0:
                if is_prev:
                    d += relativedelta(days=diff_day, hour=23, minute=59, second=59)
                else:
                    d += relativedelta(days=diff_day, hour=0, minute=0, second=0)
                return True, d
            return False, d

        def proc_nearest_weekday(d):
            """Process W (nearest weekday) day-of-month entries."""
            candidates = []
            for w_day in self.nearest_weekday:
                candidate = self._nearest_weekday_fn(d.year, d.month, w_day)
                if (is_prev and candidate <= d.day) or (not is_prev and d.day <= candidate):
                    candidates.append(candidate)

            if not candidates:
                if is_prev:
                    d += relativedelta(days=-d.day, hour=23, minute=59, second=59)
                else:
                    days = _last_day_of_month(year, month)
                    d += relativedelta(days=(days - d.day + 1), hour=0, minute=0, second=0)
                return True, d

            candidates.sort()
            diff_day = (candidates[-1] if is_prev else candidates[0]) - d.day
            if diff_day != 0:
                if is_prev:
                    d += relativedelta(days=diff_day, hour=23, minute=59, second=59)
                else:
                    d += relativedelta(days=diff_day, hour=0, minute=0, second=0)
                return True, d
            return False, d

        def proc_hour(d):
            try:
                expanded[HOUR_FIELD].index("*")
            except ValueError:
                diff_hour = nearest_diff_method(d.hour, expanded[HOUR_FIELD], 24)
                if diff_hour is not None and diff_hour != 0:
                    if is_prev:
                        d += relativedelta(hours=diff_hour, minute=59, second=59)
                    else:
                        d += relativedelta(hours=diff_hour, minute=0, second=0)
                    return True, d
            return False, d

        def proc_minute(d):
            try:
                expanded[MINUTE_FIELD].index("*")
            except ValueError:
                diff_min = nearest_diff_method(d.minute, expanded[MINUTE_FIELD], 60)
                if diff_min is not None and diff_min != 0:
                    if is_prev:
                        d += relativedelta(minutes=diff_min, second=59)
                    else:
                        d += relativedelta(minutes=diff_min, second=0)
                    return True, d
            return False, d

        def proc_second(d):
            if len(expanded) > UNIX_CRON_LEN:
                try:
                    expanded[SECOND_FIELD].index("*")
                except ValueError:
                    diff_sec = nearest_diff_method(d.second, expanded[SECOND_FIELD], 60)
                    if diff_sec is not None and diff_sec != 0:
                        d += relativedelta(seconds=diff_sec)
                        return True, d
            else:
                d += relativedelta(second=0)
            return False, d

        procs = [
            proc_year,
            proc_month,
            (proc_nearest_weekday if self.nearest_weekday else proc_day_of_month),
            (proc_day_of_week_nth if nth_weekday_of_month else proc_day_of_week),
            proc_hour,
            proc_minute,
            proc_second,
        ]

        while abs(year - current_year) <= self.max_years_between_matches:
            next = False
            stop = False
            for proc in procs:
                (changed, unaware_time) = proc(unaware_time)
                # `None` can be set mostly for year processing
                # so please see proc_year / _get_prev_nearest_diff / _get_next_nearest_diff
                if changed is None:
                    stop = True
                    break
                if changed:
                    month, year = unaware_time.month, unaware_time.year
                    next = True
                    break
            if stop:
                break
            if next:
                continue

            unaware_time = unaware_time.replace(microsecond=0)
            if now.tzinfo is None:
                return unaware_time

            # Add timezone information back and handle DST changes
            return _tzstep.resolve_aware_time(
                now,
                unaware_time,
                is_prev,
                wild_hours="*" in expanded[HOUR_FIELD],
                recalc=lambda ut: self.calc(ut, expanded, nth_weekday_of_month, is_prev),
            )

        if is_prev:
            raise CroniterBadDateError("failed to find prev date")
        raise CroniterBadDateError("failed to find next date")