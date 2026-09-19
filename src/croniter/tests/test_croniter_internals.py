#!/usr/bin/env python
"""Behavior guards for the internal component split.

The croniter main class was split into three internal components:

- croniter._expand.FieldExpander  (field normalization and expansion)
- croniter._match.DateMatcher     (date candidate matching)
- croniter._tzstep                (timezone stepping / DST correction)

These tests pin the externally observable behavior of each stage.  All
base times are fixed; no test sleeps or reads the current system clock.
"""
import unittest
import zoneinfo
from datetime import datetime
from unittest import mock

from croniter import CroniterBadCronError, croniter
from croniter import _expand as expand_mod
from croniter._expand import FieldExpander, _expansion_cache
from croniter._match import DateMatcher
from croniter.tests import base

NY = zoneinfo.ZoneInfo("America/New_York")


class FieldExpansionGuardTest(base.TestCase):
    """Field normalization and expansion (FieldExpander)."""

    def test_five_field_expansion(self):
        expanded, nth, expressions, nearest = FieldExpander.expand("5 4 * * mon-fri")
        self.assertEqual(expanded[0], [5])
        self.assertEqual(expanded[1], [4])
        self.assertEqual(expanded[4], [1, 2, 3, 4, 5])
        self.assertEqual(nth, {})
        self.assertEqual(nearest, set())

    def test_six_field_seconds_expansion(self):
        expanded, _, _, _ = FieldExpander.expand("0 0 * * * */15")
        self.assertEqual(len(expanded), 6)
        self.assertEqual(expanded[5], [0, 15, 30, 45])

    def test_seven_field_years_expansion(self):
        expanded, _, _, _ = FieldExpander.expand("0 12 1 7 * 0 2025-2030/2")
        self.assertEqual(len(expanded), 7)
        self.assertEqual(expanded[6], [2025, 2027, 2029])

    def test_second_at_beginning_expansion(self):
        expanded, _, _, _ = FieldExpander.expand("*/20 0 12 * * *", second_at_beginning=True)
        self.assertEqual(expanded[5], [0, 20, 40])
        self.assertEqual(expanded[0], [0])
        self.assertEqual(expanded[1], [12])

    def test_range_step_expansion(self):
        expanded, _, _, _ = FieldExpander.expand("5-35/10 * * * *")
        self.assertEqual(expanded[0], [5, 15, 25, 35])

    def test_nth_weekday_expansion(self):
        expanded, nth, _, _ = FieldExpander.expand("0 9 * * 2#3")
        self.assertEqual(expanded[4], [2])
        self.assertEqual(nth, {2: {3}})

    def test_last_day_expansion(self):
        expanded, _, _, _ = FieldExpander.expand("0 0 l * *")
        self.assertEqual(expanded[2], ["l"])

    def test_hash_seed_expansion_is_reused_safely(self):
        expr = "H/15 H(0-5) * * * H"
        marker = b"test_hash_seed_expansion_is_reused_safely"
        e1 = FieldExpander.expand(expr, hash_id=marker)
        size_after_first = len(_expansion_cache)
        e2 = FieldExpander.expand(expr, hash_id=marker)
        # Same expression and seed: identical result, served through the cache
        # (the second expansion does not grow the cache).
        self.assertEqual(e1, e2)
        self.assertEqual(len(_expansion_cache), size_after_first)
        # Mutating a returned structure must not corrupt the cached entry.
        e1[0][0] = "mutated"
        e3 = FieldExpander.expand(expr, hash_id=marker)
        self.assertEqual(e3, e2)
        self.assertNotEqual(e3[0][0], "mutated")

    def test_random_expression_is_never_cached(self):
        expr = "r r(0-5) * * *"
        self.assertIsNone(FieldExpander._cache_key(expr, None, False, None, None, False, None))
        before = len(_expansion_cache)
        with mock.patch.object(expand_mod.random, "randint", side_effect=[7, 8, 9, 10]) as rnd:
            first = FieldExpander.expand(expr)
            second = FieldExpander.expand(expr)
        # Each expansion draws fresh randomness instead of reusing a cached value.
        self.assertEqual(rnd.call_count, 4)
        self.assertEqual(len(_expansion_cache), before)
        self.assertNotEqual(first[0], second[0])

    def test_invalid_expression_raises_public_error(self):
        with self.assertRaises(CroniterBadCronError):
            FieldExpander.expand("not a cron expression at all")


class DateMatcherGuardTest(base.TestCase):
    """Date candidate matching (DateMatcher) via fixed base times."""

    @staticmethod
    def _matcher(expr, **kw):
        expanded, nth, _, nearest = FieldExpander.expand(expr, **kw)
        matcher = DateMatcher(nearest_weekday=nearest, max_years_between_matches=50)
        return matcher, expanded, nth

    def test_direct_component_next_and_prev(self):
        matcher, expanded, nth = self._matcher("*/20 3 * * *")
        start = datetime(2024, 1, 15, 3, 7)
        self.assertEqual(matcher.calc(start, expanded, dict(nth), False), datetime(2024, 1, 15, 3, 20))
        self.assertEqual(matcher.calc(start, expanded, dict(nth), True), datetime(2024, 1, 15, 3, 0))

    def test_nth_weekday_schedule(self):
        itr = croniter("0 9 * * 2#3", datetime(2024, 1, 1), ret_type=datetime)
        self.assertEqual(itr.get_next(datetime), datetime(2024, 1, 16, 9, 0))  # 3rd Tuesday
        self.assertEqual(itr.get_next(datetime), datetime(2024, 2, 20, 9, 0))

    def test_last_day_schedule(self):
        itr = croniter("0 0 l * *", datetime(2024, 1, 15), ret_type=datetime)
        self.assertEqual(itr.get_next(datetime), datetime(2024, 1, 31, 0, 0))
        self.assertEqual(itr.get_next(datetime), datetime(2024, 2, 29, 0, 0))  # leap year

    def test_day_or_union_and_intersection(self):
        # day_or=True (default): 13th of month OR any Friday.
        itr = croniter("0 0 13 * fri", datetime(2024, 1, 1), ret_type=datetime)
        self.assertEqual(itr.get_next(datetime), datetime(2024, 1, 5, 0, 0))  # Friday
        self.assertEqual(itr.get_next(datetime), datetime(2024, 1, 12, 0, 0))  # Friday
        self.assertEqual(itr.get_next(datetime), datetime(2024, 1, 13, 0, 0))  # 13th
        # day_or=False: intersection, i.e. Friday the 13th.
        itr = croniter("0 0 13 * fri", datetime(2024, 1, 1), ret_type=datetime, day_or=False)
        self.assertEqual(itr.get_next(datetime), datetime(2024, 9, 13, 0, 0))

    def test_seconds_schedule(self):
        itr = croniter("0 0 * * * */10", datetime(2024, 1, 1, 0, 0, 5), ret_type=datetime)
        self.assertEqual(itr.get_next(datetime), datetime(2024, 1, 1, 0, 0, 10))
        self.assertEqual(itr.get_next(datetime), datetime(2024, 1, 1, 0, 0, 20))

    def test_years_schedule(self):
        itr = croniter("0 12 1 7 * 0 2025", datetime(2024, 1, 1), ret_type=datetime)
        self.assertEqual(itr.get_next(datetime), datetime(2025, 7, 1, 12, 0, 0))

    def test_forward_backward_switching(self):
        itr = croniter("*/15 * * * *", datetime(2024, 5, 1, 10, 7), ret_type=datetime)
        self.assertEqual(itr.get_next(datetime), datetime(2024, 5, 1, 10, 15))
        self.assertEqual(itr.get_next(datetime), datetime(2024, 5, 1, 10, 30))
        self.assertEqual(itr.get_prev(datetime), datetime(2024, 5, 1, 10, 15))
        self.assertEqual(itr.get_prev(datetime), datetime(2024, 5, 1, 10, 0))
        self.assertEqual(itr.get_next(datetime), datetime(2024, 5, 1, 10, 15))


class TimezoneSteppingGuardTest(base.TestCase):
    """Timezone stepping and DST correction (America/New_York, fixed dates)."""

    def test_spring_forward_missing_hour_next(self):
        # 2024-03-10 02:30 does not exist in America/New_York.
        itr = croniter("30 2 * * *", datetime(2024, 3, 10, 0, 0, tzinfo=NY), ret_type=datetime)
        nxt = itr.get_next(datetime)
        self.assertEqual(nxt.replace(tzinfo=None), datetime(2024, 3, 10, 3, 0))
        self.assertEqual(str(nxt.utcoffset()), "-1 day, 20:00:00")  # EDT
        self.assertEqual(itr.get_next(datetime).replace(tzinfo=None), datetime(2024, 3, 11, 2, 30))

    def test_spring_forward_missing_hour_prev(self):
        itr = croniter("30 2 * * *", datetime(2024, 3, 11, 12, 0, tzinfo=NY), ret_type=datetime)
        prv = itr.get_prev(datetime)
        self.assertEqual(prv.replace(tzinfo=None), datetime(2024, 3, 11, 2, 30))
        self.assertEqual(itr.get_prev(datetime).replace(tzinfo=None), datetime(2024, 3, 10, 3, 0))

    def test_fall_back_repeated_hour_next(self):
        # 2024-11-03 01:30 occurs twice; both occurrences must be visited.
        itr = croniter("30 1 * * *", datetime(2024, 11, 3, 0, 0, tzinfo=NY), ret_type=datetime)
        first = itr.get_next(datetime)
        second = itr.get_next(datetime)
        self.assertEqual(first.replace(tzinfo=None), datetime(2024, 11, 3, 1, 30))
        self.assertEqual(second.replace(tzinfo=None), datetime(2024, 11, 3, 1, 30))
        self.assertEqual((first.utcoffset(), second.utcoffset()),
                         (first.utcoffset().__class__(-1, 72000), first.utcoffset().__class__(-1, 68400)))
        self.assertLess(first.astimezone(zoneinfo.ZoneInfo("UTC")),
                        second.astimezone(zoneinfo.ZoneInfo("UTC")))

    def test_fall_back_repeated_hour_prev(self):
        itr = croniter("30 1 * * *", datetime(2024, 11, 4, 0, 0, tzinfo=NY), ret_type=datetime)
        first = itr.get_prev(datetime)
        second = itr.get_prev(datetime)
        self.assertEqual(first.replace(tzinfo=None), datetime(2024, 11, 3, 1, 30))
        self.assertEqual(second.replace(tzinfo=None), datetime(2024, 11, 3, 1, 30))
        self.assertGreater(first.astimezone(zoneinfo.ZoneInfo("UTC")),
                           second.astimezone(zoneinfo.ZoneInfo("UTC")))

    def test_pytz_timezone_stepping(self):
        import pytz

        nyp = pytz.timezone("America/New_York")
        itr = croniter("30 2 * * *", nyp.localize(datetime(2024, 3, 10, 0, 0)), ret_type=datetime)
        self.assertEqual(itr.get_next(datetime).replace(tzinfo=None), datetime(2024, 3, 10, 3, 0))
        itr = croniter("30 1 * * *", nyp.localize(datetime(2024, 11, 3, 0, 0)), ret_type=datetime)
        first = itr.get_next(datetime)
        second = itr.get_next(datetime)
        self.assertEqual(first.replace(tzinfo=None), datetime(2024, 11, 3, 1, 30))
        self.assertEqual(second.replace(tzinfo=None), datetime(2024, 11, 3, 1, 30))
        self.assertNotEqual(first.utcoffset(), second.utcoffset())


if __name__ == "__main__":
    unittest.main()
