"""Field normalization and expansion component.

FieldExpander turns a cron expression string into the normalized
``list[list[int | 'l' | '*']]`` structure consumed by
croniter._match.DateMatcher, resolving names, ranges, steps, 'l', 'W',
nth-weekday and hash/random (H/R) expressions.

Expansion results are cached so repeated construction with the same
expression and hash seed is cheap.  Random ('r') expressions are never
cached: each expansion must draw a fresh value.
"""
from __future__ import annotations

import binascii
import calendar
import copy
import datetime
import random
import re
from typing import Union

from ._tzstep import UTC_DT
from .croniter import (
    DAY_FIELD,
    DOW_FIELD,
    HOUR_FIELD,
    MINUTE_FIELD,
    MONTH_FIELD,
    SECOND_CRON_LEN,
    SECOND_FIELD,
    UNIX_CRON_LEN,
    VALID_LEN_EXPRESSION,
    YEAR_CRON_LEN,
    YEAR_FIELD,
    CroniterBadCronError,
    CroniterNotAlphaError,
    CroniterUnsupportedSyntaxError,
)

M_ALPHAS: dict[str, Union[int, str]] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
DOW_ALPHAS: dict[str, Union[int, str]] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


step_search_re = re.compile(r"^([^-]+)-([^-/]+)(/(\d+))?$")
only_int_re = re.compile(r"^\d+$")


WEEKDAYS = "|".join(DOW_ALPHAS.keys())
MONTHS = "|".join(M_ALPHAS.keys())
star_or_int_re = re.compile(r"^(\d+|\*)$")
special_dow_re = re.compile(
    rf"^(?P<pre>((?P<he>(({WEEKDAYS})(-({WEEKDAYS}))?)"
    rf"|(({MONTHS})(-({MONTHS}))?)|\w+)#)|l)(?P<last>\d+)$"
)
nearest_weekday_re = re.compile(r"^(?:(\d+)w|w(\d+))$")
re_star = re.compile("[*]")
hash_expression_re = re.compile(
    r"^(?P<hash_type>h|r)(\((?P<range_begin>\d+)-(?P<range_end>\d+)\))?(\/(?P<divisor>\d+))?$"
)


# Bound the cache so long-lived processes generating many distinct
# expressions cannot grow it without limit.
EXPR_ALIASES = {
    "@midnight": ("0 0 * * *", "h h(0-2) * * * h"),
    "@hourly": ("0 * * * *", "h * * * * h"),
    "@daily": ("0 0 * * *", "h h * * * h"),
    "@weekly": ("0 0 * * 0", "h h * * h h"),
    "@monthly": ("0 0 1 * *", "h h h * * h"),
    "@yearly": ("0 0 1 1 *", "h h h h * h"),
    "@annually": ("0 0 1 1 *", "h h h h * h"),
}


_EXPANSION_CACHE_MAXSIZE = 512
_expansion_cache: dict = {}


def _has_random_expression(expressions) -> bool:
    """True if any field is a random ('r') hash expression.

    Random expressions must be re-drawn on every expansion, so their
    results are never cached.
    """
    for expr in expressions:
        m = hash_expression_re.match(expr)
        if m and m.group("hash_type") == "r":
            return True
    return False


class FieldExpander:
    """Normalizes and expands cron expression fields."""

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

    # Maximum days in each month (non-leap year for Feb)
    DAYS_IN_MONTH = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

    @classmethod
    def _alphaconv(cls, index, key, expressions):
        try:
            return cls.ALPHACONV[index][key]
        except KeyError:
            raise CroniterNotAlphaError(f"[{' '.join(expressions)}] is not acceptable")


    @classmethod
    def value_alias(cls, val, field_index, len_expressions=UNIX_CRON_LEN):
        if isinstance(len_expressions, (list, dict, tuple, set)):
            len_expressions = len(len_expressions)
        if val in cls.LOWMAP[field_index] and not (
            # do not support 0 as a month either for classical 5 fields cron,
            # 6fields second repeat form or 7 fields year form
            # but still let conversion happen if day field is shifted
            (field_index in [DAY_FIELD, MONTH_FIELD] and len_expressions == UNIX_CRON_LEN)
            or (field_index in [MONTH_FIELD, DOW_FIELD] and len_expressions == SECOND_CRON_LEN)
            or (
                field_index in [DAY_FIELD, MONTH_FIELD, DOW_FIELD]
                and len_expressions == YEAR_CRON_LEN
            )
        ):
            val = cls.LOWMAP[field_index][val]
        return val



    @classmethod
    def _cache_key(
        cls, expr_format, hash_id, second_at_beginning, from_timestamp,
        from_timestamp_tz, strict, strict_year,
    ):
        """Return a cache key for this expansion, or None if not cacheable.

        Random ('r') hash expressions are never cacheable: caching them
        would pin a value that must stay random per expansion.
        """
        if not isinstance(expr_format, str):
            return None
        efl = expr_format.lower()
        try:
            efl = EXPR_ALIASES[efl][1 if hash_id is not None else 0]
        except KeyError:
            pass
        if _has_random_expression(efl.split()):
            return None
        if isinstance(strict_year, list):
            strict_year = tuple(strict_year)
        try:
            key = (
                expr_format, hash_id, bool(second_at_beginning), from_timestamp,
                from_timestamp_tz, bool(strict), strict_year,
            )
            hash(key)
        except TypeError:
            return None
        return key

    @classmethod
    def expand(
        cls, expr_format, hash_id=None, second_at_beginning=False,
        from_timestamp=None, from_timestamp_tz=None, strict=False, strict_year=None,
    ):
        """Expand a cron expression, reusing cached results when safe.

        Results for the same expression and hash seed are shared through a
        bounded cache; every caller receives a deep copy so cached state can
        never be mutated.  Random ('r') expressions bypass the cache.
        """
        key = cls._cache_key(
            expr_format, hash_id, second_at_beginning, from_timestamp,
            from_timestamp_tz, strict, strict_year,
        )
        if key is not None:
            cached = _expansion_cache.get(key)
            if cached is not None:
                return copy.deepcopy(cached)
        result = cls._expand_uncached(
            expr_format, hash_id=hash_id, second_at_beginning=second_at_beginning,
            from_timestamp=from_timestamp, from_timestamp_tz=from_timestamp_tz,
            strict=strict, strict_year=strict_year,
        )
        if key is not None:
            if len(_expansion_cache) >= _EXPANSION_CACHE_MAXSIZE:
                _expansion_cache.pop(next(iter(_expansion_cache)))
            _expansion_cache[key] = copy.deepcopy(result)
        return result

    @classmethod
    def _expand_uncached(cls, expr_format, hash_id=None, second_at_beginning=False, from_timestamp=None, from_timestamp_tz=None, strict=False, strict_year=None):
        # Split the expression in components, and normalize L -> l, MON -> mon,
        # etc. Keep expr_format untouched so we can use it in the exception
        # messages.
        expr_aliases = EXPR_ALIASES

        efl = expr_format.lower()
        hash_id_expr = 1 if hash_id is not None else 0
        try:
            efl = expr_aliases[efl][hash_id_expr]
        except KeyError:
            pass

        expressions = efl.split()

        if len(expressions) not in VALID_LEN_EXPRESSION:
            raise CroniterBadCronError(
                "Exactly 5, 6 or 7 columns has to be specified for iterator expression."
            )

        if len(expressions) > UNIX_CRON_LEN and second_at_beginning:
            # move second to it's own(6th) field to process by same logical
            expressions.insert(SECOND_FIELD, expressions.pop(0))

        expanded = []
        nth_weekday_of_month = {}
        nearest_weekday = set()

        for field_index, expr in enumerate(expressions):
            for expanderid, expander in EXPANDERS.items():
                expr = expander(cls).expand(
                    efl, field_index, expr, hash_id=hash_id, from_timestamp=from_timestamp
                )

            if "?" in expr:
                if expr != "?":
                    raise CroniterBadCronError(
                        f"[{expr_format}] is not acceptable."
                        f" Question mark can not used with other characters"
                    )
                if field_index not in [DAY_FIELD, DOW_FIELD]:
                    raise CroniterBadCronError(
                        f"[{expr_format}] is not acceptable. "
                        f"Question mark can only used in day_of_month or day_of_week"
                    )
                # currently just trade `?` as `*`
                expr = "*"

            e_list = expr.split(",")
            res = []
            seen = set()

            while len(e_list) > 0:
                e = e_list.pop()
                nth = None

                if field_index == DOW_FIELD:
                    # Handle special case in the dow expression: 2#3, l3
                    special_dow_rem = special_dow_re.match(str(e))
                    if special_dow_rem:
                        g = special_dow_rem.groupdict()
                        he, last = g.get("he", ""), g.get("last", "")
                        if he:
                            e = he
                            try:
                                nth = int(last)
                                assert 5 >= nth >= 1
                            except (KeyError, ValueError, AssertionError):
                                raise CroniterBadCronError(
                                    f"[{expr_format}] is not acceptable."
                                    f" Invalid day_of_week value: '{nth}'"
                                )
                        elif last:
                            e = last
                            nth = g["pre"]  # 'l'

                if field_index == DAY_FIELD:
                    # Handle W (nearest weekday) in day-of-month: 15w, w15
                    w_match = nearest_weekday_re.match(str(e))
                    if w_match:
                        w_day = int(w_match.group(1) or w_match.group(2))
                        if w_day < 1 or w_day > 31:
                            raise CroniterBadCronError(
                                f"[{expr_format}] is not acceptable,"
                                f" nearest weekday day value '{w_day}' out of range"
                            )
                        if len(e_list) > 0 or len(res) > 0:
                            raise CroniterBadCronError(
                                f"[{expr_format}] is not acceptable."
                                f" 'W' can only be used with a single day value,"
                                f" not in a list or range"
                            )
                        nearest_weekday.add(w_day)
                        res.append(w_day)
                        continue

                # Before matching step_search_re, normalize "*" to "{min}-{max}".
                # Example: in the minute field, "*/5" normalizes to "0-59/5"
                t = re.sub(
                    r"^\*(\/.+)$",
                    r"%d-%d\1" % (cls.RANGES[field_index][0], cls.RANGES[field_index][1]),
                    str(e),
                )
                m = step_search_re.search(t)

                # "{start}/{step}" is its own shape, not a range. It is normalized
                # below to "{start}-{max}/{step}" so that one regex parses both, but
                # the two must not be conflated afterwards: "Jan-Jan" is an explicit
                # equal range that croniter deliberately expands to the whole cycle,
                # whereas "DEC/3" is a start with a step and denotes just [DEC].
                start_with_step = False
                if not m:
                    # Before matching step_search_re,
                    # normalize "{start}/{step}" to "{start}-{max}/{step}".
                    # Example: in the minute field, "10/5" normalizes to "10-59/5"
                    t = re.sub(r"^(.+)\/(.+)$", r"\1-%d/\2" % (cls.RANGES[field_index][1]), str(e))
                    m = step_search_re.search(t)
                    start_with_step = bool(m)

                if m:
                    # early abort if low/high are out of bounds
                    (low, high, step) = m.group(1), m.group(2), m.group(4) or 1
                    if field_index == DAY_FIELD and high == "l":
                        high = "31"

                    if not only_int_re.search(low):
                        low = str(cls._alphaconv(field_index, low, expressions))

                    if not only_int_re.search(high):
                        high = str(cls._alphaconv(field_index, high, expressions))

                    # normally, it's already guarded by the RE that should not accept
                    # not-int values.
                    if not only_int_re.search(str(step)):
                        raise CroniterBadCronError(
                            f"[{expr_format}] step '{step}'"
                            f" in field {field_index} is not acceptable"
                        )
                    step = int(step)
                    if step == 0:
                        raise CroniterBadCronError(
                            f"[{expr_format}] step '{step}'"
                            f" in field {field_index} is not acceptable"
                        )

                    for band in low, high:
                        if not only_int_re.search(str(band)):
                            raise CroniterBadCronError(
                                f"[{expr_format}] bands '{low}-{high}'"
                                f" in field {field_index} are not acceptable"
                            )

                    low, high = (
                        cls.value_alias(int(_val), field_index, expressions)
                        for _val in (low, high)
                    )

                    if max(low, high) > max(
                        cls.RANGES[field_index][0], cls.RANGES[field_index][1]
                    ):
                        raise CroniterBadCronError(f"{expr_format} is out of bands")

                    # "{start}/{step}" normalizes to "{start}-{max}/{step}", so when
                    # the start IS the field maximum the two bounds collide and the
                    # token becomes indistinguishable from an explicitly written equal
                    # range. That is the whole bug: "59/15" arrived at the ``low ==
                    # high`` branch below -- which exists for "Jan-Jan" and expands to
                    # the whole cycle -- and so fired at :00/:15/:30/:45 instead of
                    # :59. Recognising the collision here is what keeps the two apart.
                    #
                    # Deliberately ``low == high`` and not the wider ``low + step >
                    # high``. Both fix the reported bug, but the wider form also
                    # suppresses the re-base below for starts that are not at the
                    # maximum, silently changing ~365 additional
                    # ``expand_from_start_time`` schedules that were never broken.
                    # That is a separate question about what an explicit lower bound
                    # should mean under that flag, and it is not this fix's to answer.
                    start_at_field_max = start_with_step and low == high

                    # ``from_timestamp`` re-bases the start of a *cycle* on the start
                    # time. A single point has no cycle to re-base, and rewriting its
                    # bound here would leave the bug reachable in
                    # ``expand_from_start_time`` mode while looking fixed by default.
                    if from_timestamp and not start_at_field_max:
                        low = cls._get_low_from_current_date_number(
                            field_index, int(step), int(from_timestamp), from_timestamp_tz
                        )

                    # Handle when the second bound of the range is in backtracking order:
                    # eg: X-Sun or X-7 (Sat-Sun) in DOW, or X-Jan (Apr-Jan) in MONTH
                    if start_at_field_max:
                        rng = [low]
                    elif low > high:
                        whole_field_range = list(
                            range(cls.RANGES[field_index][0], cls.RANGES[field_index][1] + 1, 1)
                        )
                        # Add FirstBound -> ENDRANGE, respecting step
                        rng = list(range(low, cls.RANGES[field_index][1] + 1, step))
                        # Then 0 -> SecondBound, but skipping n first occurences according to step
                        # EG to respect such expressions : Apr-Jan/3
                        to_skip = 0
                        if rng:
                            already_skipped = list(reversed(whole_field_range)).index(rng[-1])
                            curpos = whole_field_range.index(rng[-1])
                            if ((curpos + step) > len(whole_field_range)) and (
                                already_skipped < step
                            ):
                                to_skip = step - already_skipped
                        rng += list(range(cls.RANGES[field_index][0] + to_skip, high + 1, step))
                    # if we include a range type: Jan-Jan, or Sun-Sun,
                    #  it means the whole cycle (all days of week, # all monthes of year, etc)
                    elif low == high:
                        rng = list(
                            range(cls.RANGES[field_index][0], cls.RANGES[field_index][1] + 1, step)
                        )
                    else:
                        try:
                            rng = list(range(low, high + 1, step))
                        except ValueError as exc:
                            raise CroniterBadCronError(f"invalid range: {exc}")

                    if field_index == DOW_FIELD and nth and nth != "l":
                        rng = [f"{item}#{nth}" for item in rng]
                    e_list += [a for a in rng if a not in seen]
                    seen.update(rng)
                else:
                    if t.startswith("-"):
                        raise CroniterBadCronError(
                            f"[{expr_format}] is not acceptable, negative numbers not allowed"
                        )
                    if not star_or_int_re.search(t):
                        t = cls._alphaconv(field_index, t, expressions)

                    try:
                        t = int(t)
                    except ValueError:
                        pass

                    t = cls.value_alias(t, field_index, expressions)

                    if t not in ["*", "l"] and (
                        int(t) < cls.RANGES[field_index][0] or int(t) > cls.RANGES[field_index][1]
                    ):
                        raise CroniterBadCronError(
                            f"[{expr_format}] is not acceptable, out of range"
                        )

                    res.append(t)

                    if field_index == DOW_FIELD and nth:
                        if t not in nth_weekday_of_month:
                            nth_weekday_of_month[t] = set()
                        nth_weekday_of_month[t].add(nth)

            res = set(res)
            res = sorted(res, key=lambda i: f"{i:02}" if isinstance(i, int) else i)
            if len(res) == cls.LEN_MEANS_ALL[field_index]:
                # Make sure the wildcard is used in the correct way (avoid over-optimization)
                if (field_index == DAY_FIELD and "*" not in expressions[DOW_FIELD]) or (
                    field_index == DOW_FIELD and "*" not in expressions[DAY_FIELD]
                ):
                    pass
                else:
                    res = ["*"]

            expanded.append(["*"] if (len(res) == 1 and res[0] == "*") else res)

        # Check to make sure the dow combo in use is supported
        if nth_weekday_of_month:
            dow_expanded_set = set(expanded[DOW_FIELD])
            dow_expanded_set = dow_expanded_set.difference(nth_weekday_of_month.keys())
            dow_expanded_set.discard("*")
            # Skip: if it's all weeks instead of wildcard
            if dow_expanded_set and len(set(expanded[DOW_FIELD])) != cls.LEN_MEANS_ALL[DOW_FIELD]:
                raise CroniterUnsupportedSyntaxError(
                    f"day-of-week field does not support mixing literal values and nth"
                    f" day of week syntax.  Cron: '{expr_format}'"
                    f"    dow={dow_expanded_set} vs nth={nth_weekday_of_month}"
                )

        if strict:
            # Cross-validate day-of-month against month (and optionally year)
            # to reject impossible combinations like "0 0 31 2 *" (Feb 31st).
            days = expanded[DAY_FIELD]
            months = expanded[MONTH_FIELD]
            if days != ["*"] and days != ["l"] and months != ["*"]:
                int_days = [d for d in days if isinstance(d, int)]
                int_months = [m for m in months if isinstance(m, int)]
                if int_days and int_months:
                    # Determine max days per month, accounting for leap years
                    days_in_month = dict(cls.DAYS_IN_MONTH)
                    if 2 in int_months:
                        has_leap_year = True  # assume possible by default
                        if strict_year is not None:
                            # Year explicitly provided as parameter
                            if isinstance(strict_year, int):
                                has_leap_year = calendar.isleap(strict_year)
                            else:
                                has_leap_year = any(calendar.isleap(y) for y in strict_year)
                        elif len(expanded) > YEAR_FIELD:
                            years = expanded[YEAR_FIELD]
                            if years != ["*"]:
                                int_years = [y for y in years if isinstance(y, int)]
                                if int_years:
                                    has_leap_year = any(calendar.isleap(y) for y in int_years)
                        if has_leap_year:
                            days_in_month[2] = 29
                    min_day = min(int_days)
                    max_possible = max(days_in_month[m] for m in int_months)
                    if min_day > max_possible:
                        raise CroniterBadCronError(
                            f"[{expr_format}] is not acceptable. Day(s) {int_days}"
                            f" can never occur in month(s) {int_months}"
                        )

        return expanded, nth_weekday_of_month, expressions, nearest_weekday

    @classmethod
    def _get_low_from_current_date_number(cls, field_index, step, from_timestamp, tzinfo=None):
        # Read the start time back in its own timezone. A naive start_time was
        # converted to a timestamp as if it were UTC, so UTC round-trips it to the
        # same wall clock and nothing changes for that case.
        dt = datetime.datetime.fromtimestamp(from_timestamp, tz=tzinfo or UTC_DT)
        if field_index == MINUTE_FIELD:
            return dt.minute % step
        if field_index == HOUR_FIELD:
            return dt.hour % step
        if field_index == DAY_FIELD:
            return ((dt.day - 1) % step) + 1
        if field_index == MONTH_FIELD:
            return ((dt.month - 1) % step) + 1
        if field_index == DOW_FIELD:
            return (dt.isoweekday() % 7) % step
        if field_index == SECOND_FIELD:
            return dt.second % step
        if field_index == YEAR_FIELD:
            # Like day and month, the year field does not start at 0, so the phase is
            # taken from the field minimum rather than from the value itself.
            year_start = cls.RANGES[YEAR_FIELD][0]
            return ((dt.year - year_start) % step) + year_start

        raise ValueError(f"Can't get current date number for field index {field_index}")



class HashExpander:
    def __init__(self, cronit):
        self.cron = cronit

    def do(self, idx, hash_type="h", hash_id=None, range_end=None, range_begin=None):
        """Return a hashed/random integer given range/hash information"""
        if range_end is None:
            range_end = self.cron.RANGES[idx][1]
        if range_begin is None:
            range_begin = self.cron.RANGES[idx][0]
        if hash_type == "r":
            crc = random.randint(0, 0xFFFFFFFF)
        else:
            crc = binascii.crc32(hash_id) & 0xFFFFFFFF
        return ((crc >> idx) % (range_end - range_begin + 1)) + range_begin

    def match(self, efl, idx, expr, hash_id=None, **kw):
        return hash_expression_re.match(expr)

    def _expand_divisor(self, idx, m, hash_id, range_begin, range_end):
        """Hash a start offset into the first period, then step to range_end.

        The offset is drawn from the first period, [range_begin, range_begin +
        divisor - 1], but never past range_end: a divisor wider than the range has
        only the range itself to draw from. And when the offset lands on range_end
        the step cannot reach a second value, so the result is that single value --
        emitting "{end}-{end}/{divisor}" instead would read as an explicit equal
        range, which croniter expands to the whole field.
        """
        divisor = int(m["divisor"])
        x = self.do(
            idx,
            hash_type=m["hash_type"],
            hash_id=hash_id,
            range_begin=range_begin,
            range_end=min(range_begin + divisor - 1, range_end),
        )
        if x == range_end:
            return str(x)
        return f"{x}-{range_end}/{divisor}"

    def expand(self, efl, idx, expr, hash_id=None, match="", **kw):
        """Expand a hashed/random expression to its normal representation"""
        if match == "":
            match = self.match(efl, idx, expr, hash_id, **kw)
        if not match:
            return expr
        m = match.groupdict()

        if m["hash_type"] == "h" and hash_id is None:
            raise CroniterBadCronError("Hashed definitions must include hash_id")

        if m["range_begin"] and m["range_end"]:
            if int(m["range_begin"]) >= int(m["range_end"]):
                raise CroniterBadCronError("Range end must be greater than range begin")

        if m["range_begin"] and m["range_end"] and m["divisor"]:
            # Example: H(30-59)/10 -> 34-59/10 (i.e. 34,44,54)
            if int(m["divisor"]) == 0:
                raise CroniterBadCronError(f"Bad expression: {expr}")

            return self._expand_divisor(
                idx, m, hash_id, int(m["range_begin"]), int(m["range_end"])
            )
        if m["range_begin"] and m["range_end"]:
            # Example: H(0-29) -> 12
            return str(
                self.do(
                    idx,
                    hash_type=m["hash_type"],
                    hash_id=hash_id,
                    range_end=int(m["range_end"]),
                    range_begin=int(m["range_begin"]),
                )
            )
        if m["divisor"]:
            # Example: H/15 -> 7-59/15 (i.e. 7,22,37,52)
            if int(m["divisor"]) == 0:
                raise CroniterBadCronError(f"Bad expression: {expr}")

            return self._expand_divisor(
                idx, m, hash_id, self.cron.RANGES[idx][0], self.cron.RANGES[idx][1]
            )

        # Example: H -> 32
        return str(self.do(idx, hash_type=m["hash_type"], hash_id=hash_id))


EXPANDERS = {"hash": HashExpander}
