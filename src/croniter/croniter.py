#!/usr/bin/env python
import copy
import datetime
import math
import platform
import struct
import sys
import traceback as _traceback
from time import time
from typing import Any, Literal, Optional, Union

from dateutil.relativedelta import relativedelta
from dateutil.tz import tzutc

from . import _fields, _match
from ._calendar import (
    DAYS,
    EPOCH,
    UTC_DT,
    _is_leap,
    _last_day_of_month,
    datetime_to_timestamp,
)
from ._errors import (
    CroniterBadCronError,
    CroniterBadDateError,
    CroniterBadTypeRangeError,
    CroniterError,
    CroniterNotAlphaError,
    CroniterUnsupportedSyntaxError,
)
from ._fields import (
    CRON_FIELDS,
    DAY_FIELD,
    DOW_ALPHAS,
    DOW_FIELD,
    EXPANDERS,
    HOUR_FIELD,
    M_ALPHAS,
    MINUTE_FIELD,
    MONTH_FIELD,
    MONTHS,
    SECOND_CRON_LEN,
    SECOND_FIELD,
    SECOND_FIELDS,
    UNIX_CRON_LEN,
    UNIX_FIELDS,
    VALID_LEN_EXPRESSION,
    WEEKDAYS,
    YEAR_CRON_LEN,
    YEAR_FIELD,
    YEAR_FIELDS,
    HashExpander,
    hash_expression_re,
    nearest_weekday_re,
    only_int_re,
    re_star,
    special_dow_re,
    star_or_int_re,
    step_search_re,
)
from ._match import DateMatcher
from ._tzstep import _add_tzinfo, _is_successor, _timezone_delta

ExpandedExpression = list[Union[int, Literal["*", "l"]]]


def is_32bit() -> bool:
    """
    Detect if Python is running in 32-bit mode.
    Returns True if running on 32-bit Python, False for 64-bit.
    """
    # Method 1: Check pointer size
    bits = struct.calcsize("P") * 8

    # Method 2: Check platform architecture string
    try:
        architecture = platform.architecture()[0]
    except RuntimeError:
        architecture = None

    # Method 3: Check maxsize
    is_small_maxsize = sys.maxsize <= 2**32

    # Evaluate all available methods
    is_32 = False

    if bits == 32:
        is_32 = True
    elif architecture and "32" in architecture:
        is_32 = True
    elif is_small_maxsize:
        is_32 = True

    return is_32


try:
    # https://github.com/python/cpython/issues/101069 detection
    if is_32bit():
        datetime.datetime.fromtimestamp(3999999999)
    OVERFLOW32B_MODE = False
except OverflowError:
    OVERFLOW32B_MODE = True


MARKER = object()


class croniter:
    MONTHS_IN_YEAR = 12

    # This helps with expanding `*` fields into `lower-upper` ranges. Each item
    # in this tuple maps to the corresponding field index
    RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6), (0, 59), (1970, 2099))

    ALPHACONV: tuple[dict[str, Union[int, str]], ...] = (
        {},  # 0: min
        {},  # 1: hour
        {"l": "l"},  # 2: dom
        # 3: mon
        copy.deepcopy(M_ALPHAS),
        # 4: dow
        copy.deepcopy(DOW_ALPHAS),
        # 5: second
        {},
        # 6: year
        {},
    )

    LOWMAP: tuple[dict[int, int], ...] = ({}, {}, {0: 1}, {0: 1}, {7: 0}, {}, {})

    LEN_MEANS_ALL = (60, 24, 31, 12, 7, 60, 130)

    def __init__(
        self,
        expr_format: str,
        start_time: Optional[Union[datetime.datetime, float]] = None,
        ret_type: type = float,
        day_or: bool = True,
        max_years_between_matches: Optional[int] = None,
        is_prev: bool = False,
        hash_id: Optional[Union[bytes, str]] = None,
        implement_cron_bug: bool = False,
        second_at_beginning: bool = False,
        expand_from_start_time: bool = False,
    ) -> None:
        self._ret_type = ret_type
        self._day_or = day_or
        self._implement_cron_bug = implement_cron_bug
        self.second_at_beginning = bool(second_at_beginning)
        self._expand_from_start_time = expand_from_start_time

        if hash_id is not None:
            if not isinstance(hash_id, (bytes, str)):
                raise TypeError("hash_id must be bytes or UTF-8 string")
            if not isinstance(hash_id, bytes):
                hash_id = hash_id.encode("UTF-8")

        self._max_years_btw_matches_explicitly_set = max_years_between_matches is not None
        if max_years_between_matches is None:
            max_years_between_matches = 50
        self._max_years_between_matches = max(int(max_years_between_matches), 1)

        if start_time is None:
            start_time = time()

        self.tzinfo: Optional[datetime.tzinfo] = None

        self.start_time = 0.0
        self.dst_start_time = 0.0
        self.cur = 0.0
        self.set_current(start_time, force=True)

        self.expanded, self.nth_weekday_of_month, self.expressions, self.nearest_weekday = self._expand(
            expr_format,
            hash_id=hash_id,
            from_timestamp=self.dst_start_time if self._expand_from_start_time else None,
            from_timestamp_tz=self.tzinfo if self._expand_from_start_time else None,
            second_at_beginning=second_at_beginning,
        )
        self.fields = CRON_FIELDS[len(self.expanded)]
        self._is_prev = is_prev

    @classmethod
    def _alphaconv(cls, index, key, expressions):
        return _fields.alphaconv(cls, index, key, expressions)

    def get_next(self, ret_type=None, start_time=None, update_current=True):
        if start_time and self._expand_from_start_time:
            raise ValueError(
                "start_time is not supported when using expand_from_start_time = True."
            )
        return self._get_next(
            ret_type=ret_type, start_time=start_time, is_prev=False, update_current=update_current
        )

    def get_prev(self, ret_type=None, start_time=None, update_current=True):
        return self._get_next(
            ret_type=ret_type, start_time=start_time, is_prev=True, update_current=update_current
        )

    def get_current(self, ret_type=None):
        ret_type = ret_type or self._ret_type
        if issubclass(ret_type, datetime.datetime):
            return self.timestamp_to_datetime(self.cur)
        return self.cur

    def set_current(
        self, start_time: Optional[Union[datetime.datetime, float]], force: bool = True
    ) -> float:
        if (force or (self.cur is None)) and start_time is not None:
            if isinstance(start_time, datetime.datetime):
                self.tzinfo = start_time.tzinfo
                start_time = self.datetime_to_timestamp(start_time)

            self.start_time = start_time
            self.dst_start_time = start_time
            self.cur = start_time
        return self.cur

    @staticmethod
    def datetime_to_timestamp(d: datetime.datetime) -> float:
        """
        Converts a `datetime` object `d` into a UNIX timestamp.
        """
        return datetime_to_timestamp(d)

    _datetime_to_timestamp = datetime_to_timestamp  # retrocompat

    def timestamp_to_datetime(self, timestamp: float, tzinfo: Any = MARKER) -> datetime.datetime:
        """
        Converts a UNIX `timestamp` into a `datetime` object.
        """
        if tzinfo is MARKER:  # allow to give tzinfo=None even if self.tzinfo is set
            tzinfo = self.tzinfo
        if OVERFLOW32B_MODE:
            # degraded mode to workaround Y2038
            # see https://github.com/python/cpython/issues/101069
            result = EPOCH.replace(tzinfo=None) + datetime.timedelta(seconds=timestamp)
        else:
            result = datetime.datetime.fromtimestamp(timestamp, tz=tzutc()).replace(tzinfo=None)
        if tzinfo:
            result = result.replace(tzinfo=UTC_DT).astimezone(tzinfo)
        return result

    _timestamp_to_datetime = timestamp_to_datetime  # retrocompat

    def _get_next(self, ret_type=None, start_time=None, is_prev=None, update_current=None):
        if update_current is None:
            update_current = True
        self.set_current(start_time, force=True)
        if is_prev is None:
            is_prev = self._is_prev
        self._is_prev = is_prev

        ret_type = ret_type or self._ret_type

        if not issubclass(ret_type, (float, datetime.datetime)):
            raise TypeError("Invalid ret_type, only 'float' or 'datetime' is acceptable.")

        result = self._calc_next(is_prev)
        timestamp = self.datetime_to_timestamp(result)
        if update_current:
            self.cur = timestamp
        if issubclass(ret_type, datetime.datetime):
            return result
        return timestamp

    # iterator protocol, to enable direct use of croniter
    # objects in a loop, like "for dt in croniter("5 0 * * *'): ..."
    # or for combining multiple croniters into single
    # dates feed using 'itertools' module
    def all_next(self, ret_type=None, start_time=None, update_current=None):
        """
        Returns a generator yielding consecutive dates.

        May be used instead of an implicit call to __iter__ whenever a
        non-default `ret_type` needs to be specified.
        """
        # In a Python 3.7+ world:  contextlib.suppress and contextlib.nullcontext could
        # be used instead
        try:
            while True:
                self._is_prev = False
                yield self._get_next(
                    ret_type=ret_type, start_time=start_time, update_current=update_current
                )
                start_time = None
        except CroniterBadDateError:
            if self._max_years_btw_matches_explicitly_set:
                return
            raise

    def all_prev(self, ret_type=None, start_time=None, update_current=None):
        """
        Returns a generator yielding previous dates.
        """
        try:
            while True:
                self._is_prev = True
                yield self._get_next(
                    ret_type=ret_type, start_time=start_time, update_current=update_current
                )
                start_time = None
        except CroniterBadDateError:
            if self._max_years_btw_matches_explicitly_set:
                return
            raise

    def iter(self, *args, **kwargs):
        return self.all_prev if self._is_prev else self.all_next

    def __iter__(self):
        return self

    __next__ = next = _get_next

    def _calc_next(self, is_prev: bool) -> datetime.datetime:
        current = self.timestamp_to_datetime(self.cur)
        return self._build_matcher().calc_next(current, is_prev)

    def _build_matcher(self) -> DateMatcher:
        """Assemble the date-matching component from the current state."""
        return DateMatcher(
            self.expanded,
            self.nth_weekday_of_month,
            self.nearest_weekday,
            day_or=self._day_or,
            implement_cron_bug=self._implement_cron_bug,
            expressions=self.expressions,
            max_years_between_matches=self._max_years_between_matches,
            months_in_year=self.MONTHS_IN_YEAR,
            next_nearest_diff=self._get_next_nearest_diff,
            prev_nearest_diff=self._get_prev_nearest_diff,
            nth_weekday_of_month_fn=self._get_nth_weekday_of_month,
            nearest_weekday_fn=self._get_nearest_weekday,
        )
    def _calc(
        self,
        now: datetime.datetime,
        expanded: list[ExpandedExpression],
        nth_weekday_of_month: dict[int, set[int]],
        is_prev: bool,
    ) -> datetime.datetime:
        return self._build_matcher().calc(now, expanded, nth_weekday_of_month, is_prev)
    @staticmethod
    def _get_next_nearest_diff(x, to_check, range_val):
        """
        `range_val` is the range of a field.
        If no available time, we can move to next loop(like next month).
        `range_val` can also be set to `None` to indicate that there is no loop.
        ( Currently, should only used for `year` field )
        """
        return _match.get_next_nearest_diff(x, to_check, range_val)

    @staticmethod
    def _get_prev_nearest_diff(x, to_check, range_val):
        """
        `range_val` is the range of a field.
        If no available time, we can move to previous loop(like previous month).
        Range_val can also be set to `None` to indicate that there is no loop.
        ( Currently should only used for `year` field )
        """
        return _match.get_prev_nearest_diff(x, to_check, range_val)

    @staticmethod
    def _get_nth_weekday_of_month(year: int, month: int, day_of_week: int) -> tuple[int, ...]:
        """For a given year/month return a list of days in nth-day-of-month order.
        The last weekday of the month is always [-1].
        """
        return _match.get_nth_weekday_of_month(year, month, day_of_week)

    @staticmethod
    def _get_nearest_weekday(year, month, day):
        """Get the nearest weekday (Mon-Fri) to the given day in the given month.

        Rules:
        - If the day is a weekday, return it.
        - If Saturday, return Friday (day-1), unless that crosses into previous month,
          then return Monday (day+2).
        - If Sunday, return Monday (day+1), unless that crosses into next month,
          then return Friday (day-2).
        """
        return _match.get_nearest_weekday(year, month, day)
    @classmethod
    def value_alias(cls, val, field_index, len_expressions=UNIX_CRON_LEN):
        return _fields.value_alias(cls, val, field_index, len_expressions)
    # Maximum days in each month (non-leap year for Feb)
    DAYS_IN_MONTH = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

    @classmethod
    def _expand(cls, expr_format, hash_id=None, second_at_beginning=False, from_timestamp=None, from_timestamp_tz=None, strict=False, strict_year=None):
        return _fields.expand_expression_cached(
            cls,
            expr_format,
            hash_id=hash_id,
            second_at_beginning=second_at_beginning,
            from_timestamp=from_timestamp,
            from_timestamp_tz=from_timestamp_tz,
            strict=strict,
            strict_year=strict_year,
        )
    @classmethod
    def expand(
        cls,
        expr_format: str,
        hash_id: Optional[Union[bytes, str]] = None,
        second_at_beginning: bool = False,
        from_timestamp: Optional[float] = None,
        from_timestamp_tz: Optional[datetime.tzinfo] = None,
        strict: bool = False,
        strict_year: Optional[Union[int, list[int]]] = None,
    ) -> tuple[list[ExpandedExpression], dict[int, set[int]]]:
        """
        Expand a cron expression format into a noramlized format of
        list[list[int | 'l' | '*']]. The first list representing each element
        of the epxression, and each sub-list representing the allowed values
        for that expression component.

        A tuple is returned, the first value being the expanded epxression
        list, and the second being a `nth_weekday_of_month` mapping.

        Examples:

        # Every minute
        >>> croniter.expand('* * * * *')
        ([['*'], ['*'], ['*'], ['*'], ['*']], {})

        # On the hour
        >>> croniter.expand('0 0 * * *')
        ([[0], [0], ['*'], ['*'], ['*']], {})

        # Hours 0-5 and 10 monday through friday
        >>> croniter.expand('0-5,10 * * * mon-fri')
        ([[0, 1, 2, 3, 4, 5, 10], ['*'], ['*'], ['*'], [1, 2, 3, 4, 5]], {})

        Note that some special values such as nth day of week are expanded to a
        special mapping format for later processing:

        # Every minute on the 3rd tuesday of the month
        >>> croniter.expand('* * * * 2#3')
        ([['*'], ['*'], ['*'], ['*'], [2]], {2: {3}})

        # Every hour on the last day of the month
        >>> croniter.expand('0 * l * *')
        ([[0], ['*'], ['l'], ['*'], ['*']], {})

        # On the hour every 15 seconds
        >>> croniter.expand('0 0 * * * */15')
        ([[0], [0], ['*'], ['*'], ['*'], [0, 15, 30, 45]], {})
        """
        try:
            expanded, nth_weekday_of_month, _expressions, _nearest_weekday = cls._expand(
                expr_format,
                hash_id=hash_id,
                second_at_beginning=second_at_beginning,
                from_timestamp=from_timestamp,
                from_timestamp_tz=from_timestamp_tz,
                strict=strict,
                strict_year=strict_year,
            )
            return expanded, nth_weekday_of_month
        except (ValueError,) as exc:
            if isinstance(exc, CroniterError):
                raise
            trace = _traceback.format_exc()
            raise CroniterBadCronError(trace)

    @classmethod
    def _get_low_from_current_date_number(cls, field_index, step, from_timestamp, tzinfo=None):
        return _fields.get_low_from_current_date_number(
            cls, field_index, step, from_timestamp, tzinfo
        )

    @classmethod
    def is_valid(cls, expression, hash_id=None, encoding="UTF-8", second_at_beginning=False, strict=False, strict_year=None):
        if hash_id:
            if not isinstance(hash_id, (bytes, str)):
                raise TypeError("hash_id must be bytes or UTF-8 string")
            if not isinstance(hash_id, bytes):
                hash_id = hash_id.encode(encoding)
        try:
            cls.expand(expression, hash_id=hash_id, second_at_beginning=second_at_beginning, strict=strict, strict_year=strict_year)
        except CroniterError:
            return False
        return True

    @classmethod
    def match(
        cls,
        cron_expression,
        testdate,
        day_or=True,
        second_at_beginning=False,
        precision_in_seconds=None,
    ):
        return cls.match_range(
            cron_expression, testdate, testdate, day_or, second_at_beginning, precision_in_seconds
        )

    @classmethod
    def match_range(
        cls,
        cron_expression,
        from_datetime,
        to_datetime,
        day_or=True,
        second_at_beginning=False,
        precision_in_seconds=None,
    ):
        cron = cls(
            cron_expression,
            to_datetime,
            ret_type=datetime.datetime,
            day_or=day_or,
            second_at_beginning=second_at_beginning,
        )
        tdp = cron.get_current(datetime.datetime)
        if not tdp.microsecond:
            tdp += relativedelta(microseconds=1)
        cron.set_current(tdp, force=True)
        try:
            tdt = cron.get_prev()
        except CroniterBadDateError:
            return False
        if precision_in_seconds is None:
            precision_in_seconds = 1 if len(cron.expanded) > UNIX_CRON_LEN else 60
        duration_in_second = (to_datetime - from_datetime).total_seconds() + precision_in_seconds
        return (max(tdp, tdt) - min(tdp, tdt)).total_seconds() < duration_in_second


def croniter_range(
    start,
    stop,
    expr_format,
    ret_type=None,
    day_or=True,
    exclude_ends=False,
    _croniter=None,
    second_at_beginning=False,
    expand_from_start_time=False,
):
    """
    Generator that provides all times from start to stop matching the given cron expression.
    If the cron expression matches either 'start' and/or 'stop', those times will be returned as
    well unless 'exclude_ends=True' is passed.

    You can think of this function as sibling to the builtin range function for datetime objects.
    Like range(start,stop,step), except that here 'step' is a cron expression.
    """
    _croniter = _croniter or croniter
    auto_rt = datetime.datetime
    # type is used in first if branch for perfs reasons
    if type(start) is not type(stop) and not (
        isinstance(start, type(stop)) or isinstance(stop, type(start))
    ):
        raise CroniterBadTypeRangeError(
            f"The start and stop must be same type.  {type(start)} != {type(stop)}"
        )
    if isinstance(start, (float, int)):
        start, stop = (
            datetime.datetime.fromtimestamp(t, tzutc()).replace(tzinfo=None) for t in (start, stop)
        )
        auto_rt = float
    if ret_type is None:
        ret_type = auto_rt
    if not exclude_ends:
        ms1 = relativedelta(microseconds=1)
        if start < stop:  # Forward (normal) time order
            start -= ms1
            stop += ms1
        else:  # Reverse time order
            start += ms1
            stop -= ms1
    year_span = math.floor(abs(stop.year - start.year)) + 1
    ic = _croniter(
        expr_format,
        start,
        ret_type=datetime.datetime,
        day_or=day_or,
        max_years_between_matches=year_span,
        second_at_beginning=second_at_beginning,
        expand_from_start_time=expand_from_start_time,
    )
    # define a continue (cont) condition function and step function for the main while loop
    if start < stop:  # Forward

        def cont(v):
            return v < stop

        step = ic.get_next
    else:  # Reverse

        def cont(v):
            return v > stop

        step = ic.get_prev
    try:
        dt = step()
        while cont(dt):
            if ret_type is float:
                yield ic.get_current(float)
            else:
                yield dt
            dt = step()
    except CroniterBadDateError:
        # Stop iteration when this exception is raised; no match found within the given year range
        return


