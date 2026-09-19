# Internal architecture notes (maintainers only)

This file documents the internal component split of `croniter`. It is not
user-facing documentation; the public API (`croniter`, `croniter_range`,
exception types, `get_next`/`get_prev` semantics, repr, pickling and error
messages) is unchanged.

## Pipeline stages

`croniter` now delegates to three independently testable internal
components, one per pipeline stage:

| Stage | Module | Entry points |
| --- | --- | --- |
| Field normalization & expansion | `src/croniter/_fields.py` | `expand_expression`, `expand_expression_cached`, `HashExpander` |
| Date candidate matching | `src/croniter/_match.py` | `DateMatcher.calc_next`, `DateMatcher.calc`, `get_next_nearest_diff`, `get_prev_nearest_diff` |
| Timezone stepping (DST) | `src/croniter/_tzstep.py` | `_add_tzinfo`, `resolve_aware_time`, `_is_successor`, `_timezone_delta` |

Supporting modules:

- `src/croniter/_errors.py` — the public exception hierarchy. `__module__`
  is pinned to `croniter.croniter` so class reprs and pickle paths are
  identical to the pre-split layout.
- `src/croniter/_calendar.py` — shared date primitives (`UTC_DT`, `EPOCH`,
  `DAYS`, `_is_leap`, `_last_day_of_month`, `datetime_to_timestamp`).

`src/croniter/croniter.py` keeps the public surface: the `croniter` class
(orchestration, timestamp conversion, iterator protocol), `croniter_range`,
and re-exports of every previously importable module-level name (constants,
regexes, `HashExpander`, `EXPANDERS`, helpers). Methods such as
`croniter._expand`, `croniter._calc`, `croniter._get_next_nearest_diff`
remain as thin delegates so subclass overrides keep working; no function in
this module exceeds 120 lines.

## Data flow

1. `croniter.__init__` calls `croniter._expand` →
   `_fields.expand_expression_cached` → `(expanded, nth_weekday_of_month,
   expressions, nearest_weekday)`.
2. `get_next`/`get_prev` → `_calc_next` builds a `_match.DateMatcher` from
   the current instance state and calls `calc_next` (day_or union logic
   lives here).
3. `DateMatcher.calc` searches naive local times; when the iterator is
   timezone-aware it delegates DST gap/overlap resolution to
   `_tzstep.resolve_aware_time`, passing `calc` as the `recalc` callback
   used to skip nonexistent local times and to probe the alternative
   UTC offset after a transition.

## Expansion cache rules

`_fields.expand_expression_cached` caches expansion results keyed by
`(class, expression, hash_id, second_at_beginning, from_timestamp,
from_timestamp_tz, strict, strict_year)`:

- Same expression + same seed (`hash_id`) → safe to reuse; callers always
  receive defensive copies, so mutating a returned structure cannot
  corrupt the cache.
- `R` (random) fields **bypass the cache entirely** — every expansion
  re-rolls `random.randint`. Caching them would freeze schedules that are
  meant to vary per instance.
- The cache is bounded (`_EXPANSION_CACHE_MAXSIZE = 512`, FIFO eviction)
  and falls back to uncached expansion if a key component is unhashable.

## Behavior guards

`src/croniter/tests/test_behavior_guards.py` pins pre-refactor behavior
(fixed base times, no sleep, no system clock): 5/6/7-field expressions,
seconds & years, `day_or`, nth weekday, `L`/`L5`, `W`, range steps, hash
seeds, random re-rolling, next/prev switching, and America/New_York DST
(spring gap + fall overlap, zoneinfo and pytz).
`src/croniter/tests/test_components.py` exercises each component directly.

## Performance guard

`benchmarks/bench_step.py` times 10000 forward `get_next()` calls on
`*/1 * * * *` from a fixed base time (5 repeats). The pre-refactor
baseline lives in `benchmarks/baseline_step.json`; method, raw numbers
and environment are documented in `benchmarks/BASELINE.md`.

- Re-measure: `python3 benchmarks/bench_step.py`
- Check against baseline (fails if >15% slower):
  `python3 benchmarks/bench_step.py --check`
- Re-record baseline (only on the reference machine, and note the new
  environment in `benchmarks/BASELINE.md`):
  `python3 benchmarks/bench_step.py --save-baseline`

The pytest guard `src/croniter/tests/test_step_benchmark.py` enforces the
same 15% budget and writes `benchmarks/last_run.json`; the root
`conftest.py` prints the guard/validation names and the perf line in the
terminal summary of `python3 -m pytest -q`. The gate compares
min-of-repeats (least load-sensitive); the recorded baseline environment
is macOS 26.5.2 / Apple M5 Max / CPython 3.13.14.
