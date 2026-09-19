#!/usr/bin/env python
"""Direct tests for the internal components created by the split:

* ``croniter._fields``  -- field normalization & expansion (plus its cache)
* ``croniter._match``   -- date candidate matching
* ``croniter._tzstep``  -- timezone stepping / DST resolution

All base times are fixed literals; no sleep, no system clock.
"""

import unittest
import zoneinfo
from datetime import datetime, timedelta
from unittest import mock

import pytz

from croniter import (
    CroniterBadCronError,
    CroniterBadDateError,
    CroniterNotAlphaError,
    croniter,
)
from croniter import _fields, _match, _tzstep
from croniter.tests import base

NY = zoneinfo.ZoneInfo("America/New_York")
NY_PYTZ = pytz.timezone("America/New_York")


class FieldExpansionComponentTest(base.TestCase):
    def expand(self, expr, **kwargs):
        return _fields.expand_expression(croniter, expr, **kwargs)

    def test_unix_expression(self):
        expanded, nth, expressions, nearest = self.expand("0-5,10 * * * mon-fri")
        self.assertEqual(expanded, [[0, 1, 2, 3, 4, 5, 10], ["*"], ["*"], ["*"], [1, 2, 3, 4, 5]])
        self.assertEqual(nth, {})
        self.assertEqual(nearest, set())

    def test_second_and_year_fields(self):
        expanded, _, _, _ = self.expand("0 0 * * * */15 2025-2030/2")
        self.assertEqual(expanded[5], [0, 15, 30, 45])
        self.assertEqual(expanded[6], [2025, 2027, 2029])

    def test_nth_weekday_mapping(self):
        expanded, nth, _, _ = self.expand("* * * * 2#3")
        self.assertEqual(expanded[4], [2])
        self.assertEqual(nth, {2: {3}})

    def test_last_and_nearest_weekday(self):
        expanded, _, _, _ = self.expand("0 * l * *")
        self.assertEqual(expanded[2], ["l"])
        _, _, _, nearest = self.expand("0 0 15W * *")
        self.assertEqual(nearest, {15})

    def test_hash_seed_expansion_is_stable(self):
        first = self.expand("H(0-29) * * * *", hash_id=b"guard-seed")
        second = self.expand("H(0-29) * * * *", hash_id=b"guard-seed")
        self.assertEqual(first, second)
        self.assertEqual(first[0][0], [13])

    def test_invalid_expression_raises(self):
        with self.assertRaises(CroniterBadCronError):
            self.expand("0 0 * *")
        with self.assertRaises(CroniterNotAlphaError):
            self.expand("0 0 * foo *")
        with self.assertRaises(CroniterBadCronError):
            self.expand("70 * * * *")


class ExpansionCacheTest(base.TestCase):
    def setUp(self):
        _fields._expansion_cache.clear()

    def tearDown(self):
        _fields._expansion_cache.clear()

    def test_same_expression_and_seed_are_reused(self):
        _fields.expand_expression_cached(croniter, "H(0-29) * * * *", hash_id=b"seed")
        _fields.expand_expression_cached(croniter, "H(0-29) * * * *", hash_id=b"seed")
        self.assertEqual(len(_fields._expansion_cache), 1)

    def test_cache_hands_out_defensive_copies(self):
        first, _, _, _ = _fields.expand_expression_cached(croniter, "0 0 * * *")
        first[0].append(999)
        second, _, _, _ = _fields.expand_expression_cached(croniter, "0 0 * * *")
        self.assertNotIn(999, second[0])

    def test_random_expression_bypasses_cache(self):
        with mock.patch("random.randint", wraps=__import__("random").randint) as spy:
            _fields.expand_expression_cached(croniter, "R(0-59) * * * *")
            _fields.expand_expression_cached(croniter, "R(0-59) * * * *")
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(len(_fields._expansion_cache), 0)

    def test_random_alias_free_expression_is_cached(self):
        _fields.expand_expression_cached(croniter, "@daily", hash_id=b"seed")
        _fields.expand_expression_cached(croniter, "@daily", hash_id=b"seed")
        self.assertEqual(len(_fields._expansion_cache), 1)


class DateMatcherComponentTest(base.TestCase):
    def matcher(self, expr, **kwargs):
        expanded, nth, expressions, nearest = _fields.expand_expression(croniter, expr, **kwargs)
        return _match.DateMatcher(expanded, nth, nearest, expressions=expressions)

    def test_naive_forward(self):
        m = self.matcher("*/20 * * * *")
        self.assertEqual(m.calc_next(datetime(2024, 5, 6, 12, 7), False), datetime(2024, 5, 6, 12, 20))

    def test_naive_backward(self):
        m = self.matcher("*/20 * * * *")
        self.assertEqual(m.calc_next(datetime(2024, 5, 6, 12, 7), True), datetime(2024, 5, 6, 12, 0))

    def test_day_or_union(self):
        m = self.matcher("0 0 13 * 5")
        self.assertEqual(m.calc_next(datetime(2024, 5, 1), False), datetime(2024, 5, 3))

    def test_unsatisfiable_raises(self):
        m = self.matcher("0 0 31 2 *")
        with self.assertRaises(CroniterBadDateError):
            m.calc_next(datetime(2024, 5, 1), False)

    def test_nearest_diff_helpers(self):
        self.assertEqual(_match.get_next_nearest_diff(7, [0, 15, 30, 45], 60), 8)
        self.assertEqual(_match.get_prev_nearest_diff(7, [0, 15, 30, 45], 60), -7)
        self.assertEqual(_match.get_nth_weekday_of_month(2024, 5, 2), (7, 14, 21, 28))
        self.assertEqual(_match.get_nearest_weekday(2024, 6, 15), 14)  # Sat -> Fri


class TzStepComponentTest(base.TestCase):
    def test_add_tzinfo_nonexistent_pytz(self):
        prev = NY_PYTZ.localize(datetime(2024, 3, 9, 12, 0))
        aware, exists = _tzstep._add_tzinfo(datetime(2024, 3, 10, 2, 30), prev, False)
        self.assertFalse(exists)
        self.assertEqual(aware.replace(tzinfo=None), datetime(2024, 3, 10, 3, 0))

    def test_add_tzinfo_ambiguous_pytz(self):
        prev = NY_PYTZ.localize(datetime(2024, 11, 2, 12, 0))
        aware, exists = _tzstep._add_tzinfo(datetime(2024, 11, 3, 1, 30), prev, False)
        self.assertTrue(exists)
        self.assertEqual(aware.utcoffset(), timedelta(hours=-4))

    def test_add_tzinfo_ambiguous_zoneinfo_fold(self):
        prev = datetime(2024, 11, 2, 12, 0, tzinfo=NY)
        aware, exists = _tzstep._add_tzinfo(datetime(2024, 11, 3, 1, 30), prev, False)
        self.assertTrue(exists)
        self.assertEqual(aware.fold, 0)
        later = datetime(2024, 11, 4, 12, 0, tzinfo=NY)
        aware_prev, exists_prev = _tzstep._add_tzinfo(datetime(2024, 11, 3, 1, 30), later, True)
        self.assertTrue(exists_prev)
        self.assertEqual(aware_prev.fold, 1)

    def test_resolve_aware_time_without_dst_change(self):
        now = datetime(2024, 5, 6, 12, 0, tzinfo=NY)
        candidate = datetime(2024, 5, 6, 12, 20)
        recalc = mock.Mock(side_effect=AssertionError("must not recalculate"))
        result = _tzstep.resolve_aware_time(now, candidate, False, False, recalc)
        self.assertEqual(result, candidate.replace(tzinfo=NY))
        recalc.assert_not_called()

    def test_successor_and_delta_helpers(self):
        now = datetime(2024, 11, 3, 1, 30, tzinfo=NY)
        later = datetime(2024, 11, 3, 1, 30, fold=1, tzinfo=NY)
        self.assertTrue(_tzstep._is_successor(later, now, False))
        self.assertFalse(_tzstep._is_successor(later, now, True))
        self.assertEqual(_tzstep._timezone_delta(now, later), timedelta(hours=-1))


if __name__ == "__main__":
    unittest.main()
