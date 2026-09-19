#!/usr/bin/env python
"""Behavior guards for the internal component split.

Every expectation in this module was pinned against the pre-refactor
implementation (commit d5b35bc). All base times are fixed literals: no
sleep, no reliance on the current system clock.
"""

import unittest
import zoneinfo
from datetime import datetime
from unittest import mock

import pytz

from croniter import croniter
from croniter.tests import base

NY = zoneinfo.ZoneInfo("America/New_York")
NY_PYTZ = pytz.timezone("America/New_York")


def nexts(expr, start, count, **kwargs):
    itr = croniter(expr, start, **kwargs)
    return [itr.get_next(datetime) for _ in range(count)]


class SixAndSevenFieldTest(base.TestCase):
    def test_seconds_field(self):
        self.assertEqual(
            nexts("* * * * * */15", datetime(2024, 5, 6, 12, 0, 7), 4),
            [
                datetime(2024, 5, 6, 12, 0, 15),
                datetime(2024, 5, 6, 12, 0, 30),
                datetime(2024, 5, 6, 12, 0, 45),
                datetime(2024, 5, 6, 12, 1, 0),
            ],
        )

    def test_second_at_beginning(self):
        self.assertEqual(
            nexts("15 * * * *", datetime(2024, 5, 6, 12, 0, 7), 3, second_at_beginning=True),
            [
                datetime(2024, 5, 6, 12, 15),
                datetime(2024, 5, 6, 13, 15),
                datetime(2024, 5, 6, 14, 15),
            ],
        )

    def test_year_field_next(self):
        self.assertEqual(
            nexts("0 0 1 1 * 0 2025-2030/2", datetime(2024, 5, 6, 12, 0, 7), 3),
            [datetime(2025, 1, 1), datetime(2027, 1, 1), datetime(2029, 1, 1)],
        )

    def test_year_field_prev(self):
        itr = croniter("0 0 1 1 * 0 2025-2030/2", datetime(2031, 3, 1))
        self.assertEqual(
            [itr.get_prev(datetime) for _ in range(3)],
            [datetime(2029, 1, 1), datetime(2027, 1, 1), datetime(2025, 1, 1)],
        )


class DaySemanticsTest(base.TestCase):
    def test_day_or_union(self):
        # 13th of the month OR Friday
        self.assertEqual(
            nexts("0 0 13 * 5", datetime(2024, 5, 1), 5),
            [
                datetime(2024, 5, 3),
                datetime(2024, 5, 10),
                datetime(2024, 5, 13),
                datetime(2024, 5, 17),
                datetime(2024, 5, 24),
            ],
        )

    def test_nth_weekday(self):
        self.assertEqual(
            nexts("0 0 * * 2#3", datetime(2024, 5, 1), 3),
            [datetime(2024, 5, 21), datetime(2024, 6, 18), datetime(2024, 7, 16)],
        )

    def test_last_day_of_month(self):
        self.assertEqual(
            nexts("0 0 L * *", datetime(2024, 1, 15), 3),
            [datetime(2024, 1, 31), datetime(2024, 2, 29), datetime(2024, 3, 31)],
        )

    def test_last_dow_of_month(self):
        self.assertEqual(
            nexts("0 0 * * L5", datetime(2024, 5, 1), 3),
            [datetime(2024, 5, 31), datetime(2024, 6, 28), datetime(2024, 7, 26)],
        )

    def test_nearest_weekday(self):
        self.assertEqual(
            nexts("0 0 15W * *", datetime(2024, 5, 1), 3),
            [datetime(2024, 5, 15), datetime(2024, 6, 14), datetime(2024, 7, 15)],
        )

    def test_range_step(self):
        self.assertEqual(
            nexts("5-45/10 * * * *", datetime(2024, 5, 6, 12, 0), 6),
            [
                datetime(2024, 5, 6, 12, 5),
                datetime(2024, 5, 6, 12, 15),
                datetime(2024, 5, 6, 12, 25),
                datetime(2024, 5, 6, 12, 35),
                datetime(2024, 5, 6, 12, 45),
                datetime(2024, 5, 6, 13, 5),
            ],
        )


class HashAndRandomTest(base.TestCase):
    def test_hash_seed_is_deterministic(self):
        expected = [datetime(2024, 5, 6, 12, 13), datetime(2024, 5, 6, 13, 13)]
        self.assertEqual(
            nexts("H(0-29) * * * *", datetime(2024, 5, 6, 12, 0), 2, hash_id="guard-seed"),
            expected,
        )
        # same expression + same seed expands identically across instances
        self.assertEqual(
            nexts("H(0-29) * * * *", datetime(2024, 5, 6, 12, 0), 2, hash_id="guard-seed"),
            expected,
        )

    def test_hash_divisor(self):
        self.assertEqual(
            nexts("H/15 * * * *", datetime(2024, 5, 6, 12, 0), 4, hash_id="guard-seed"),
            [
                datetime(2024, 5, 6, 12, 13),
                datetime(2024, 5, 6, 12, 28),
                datetime(2024, 5, 6, 12, 43),
                datetime(2024, 5, 6, 12, 58),
            ],
        )

    def test_random_not_frozen_by_expansion_cache(self):
        # Each construction must re-roll the random field; a cache hit would
        # freeze the schedule by skipping the draw entirely.
        with mock.patch("random.randint", wraps=__import__("random").randint) as spy:
            croniter("R(0-59) * * * *", datetime(2024, 5, 6, 12, 0))
            croniter("R(0-59) * * * *", datetime(2024, 5, 6, 12, 0))
        self.assertEqual(spy.call_count, 2)

    def test_random_stays_in_range(self):
        for _ in range(10):
            itr = croniter("R(10-20) * * * *", datetime(2024, 5, 6, 12, 0))
            result = itr.get_next(datetime)
            self.assertTrue(10 <= result.minute <= 20)


class DirectionSwitchTest(base.TestCase):
    def test_next_prev_alternation(self):
        itr = croniter("*/20 * * * *", datetime(2024, 5, 6, 12, 7, 0))
        self.assertEqual(
            [
                itr.get_next(datetime),
                itr.get_next(datetime),
                itr.get_prev(datetime),
                itr.get_next(datetime),
            ],
            [
                datetime(2024, 5, 6, 12, 20),
                datetime(2024, 5, 6, 12, 40),
                datetime(2024, 5, 6, 12, 20),
                datetime(2024, 5, 6, 12, 40),
            ],
        )


class AmericaNewYorkDstTest(base.TestCase):
    """Spring gap (2024-03-10 02:00-03:00 missing) and fall overlap
    (2024-11-03 01:00-02:00 repeated) in America/New_York."""

    def test_spring_gap_forward_zoneinfo(self):
        self.assertEqual(
            nexts("30 2 * * *", datetime(2024, 3, 9, 12, 0, tzinfo=NY), 2),
            [
                datetime(2024, 3, 10, 3, 0, tzinfo=NY),
                datetime(2024, 3, 11, 2, 30, tzinfo=NY),
            ],
        )

    def test_spring_gap_backward_zoneinfo(self):
        itr = croniter("30 2 * * *", datetime(2024, 3, 11, 12, 0, tzinfo=NY))
        self.assertEqual(
            [itr.get_prev(datetime) for _ in range(2)],
            [
                datetime(2024, 3, 11, 2, 30, fold=1, tzinfo=NY),
                datetime(2024, 3, 10, 3, 0, tzinfo=NY),
            ],
        )

    def test_fall_overlap_forward_zoneinfo(self):
        results = nexts("30 1 * * *", datetime(2024, 11, 2, 12, 0, tzinfo=NY), 3)
        self.assertEqual(
            results,
            [
                datetime(2024, 11, 3, 1, 30, tzinfo=NY),
                datetime(2024, 11, 3, 1, 30, fold=1, tzinfo=NY),
                datetime(2024, 11, 4, 1, 30, tzinfo=NY),
            ],
        )
        # both occurrences of the repeated wall time are visited
        from datetime import timedelta

        self.assertEqual(results[0].utcoffset(), timedelta(hours=-4))
        self.assertEqual(results[1].utcoffset(), timedelta(hours=-5))

    def test_fall_overlap_backward_zoneinfo(self):
        itr = croniter("30 1 * * *", datetime(2024, 11, 4, 12, 0, tzinfo=NY))
        self.assertEqual(
            [itr.get_prev(datetime) for _ in range(3)],
            [
                datetime(2024, 11, 4, 1, 30, fold=1, tzinfo=NY),
                datetime(2024, 11, 3, 1, 30, fold=1, tzinfo=NY),
                datetime(2024, 11, 3, 1, 30, tzinfo=NY),
            ],
        )

    def test_spring_gap_forward_pytz(self):
        results = nexts("30 2 * * *", NY_PYTZ.localize(datetime(2024, 3, 9, 12, 0)), 2)
        self.assertEqual(
            [r.replace(tzinfo=None) for r in results],
            [datetime(2024, 3, 10, 3, 0), datetime(2024, 3, 11, 2, 30)],
        )
        self.assertEqual(str(results[0].utcoffset()), "-1 day, 20:00:00")

    def test_fall_overlap_forward_pytz(self):
        results = nexts("30 1 * * *", NY_PYTZ.localize(datetime(2024, 11, 2, 12, 0)), 3)
        self.assertEqual(
            [r.replace(tzinfo=None) for r in results],
            [datetime(2024, 11, 3, 1, 30)] * 2 + [datetime(2024, 11, 4, 1, 30)],
        )
        self.assertEqual(str(results[0].tzinfo), "America/New_York")
        self.assertNotEqual(results[0].utcoffset(), results[1].utcoffset())


if __name__ == "__main__":
    unittest.main()
