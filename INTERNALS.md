# croniter internals (maintainer notes)

The `croniter` class in `src/croniter/croniter.py` is a thin orchestrator over
three internal components, each independently testable:

| Stage | Module | Responsibility |
| --- | --- | --- |
| Field normalization & expansion | `src/croniter/_expand.py` (`FieldExpander`) | Parses the expression string into the normalized `list[list[int \| 'l' \| '*']]` structure: names, ranges, steps, `l`, `W`, `nth#`, and hash/random (`H`/`R`) expressions (`HashExpander`). |
| Date candidate matching | `src/croniter/_match.py` (`DateMatcher`) | Walks the expanded fields (year → month → day → hour → minute → second) to find the nearest matching date in either direction. No parsing knowledge. |
| Timezone stepping | `src/croniter/_tzstep.py` | Attaches tzinfo to naive candidates and resolves DST edge cases (non-existent and ambiguous local times). |

`croniter` keeps its public surface unchanged: same constructor parameters,
exception classes (still defined in `croniter.croniter`), `get_next`/`get_prev`
behavior, and module-level helper aliases (`_add_tzinfo`, `HashExpander`,
`EXPANDERS`, `RANGES`, ...) for backward compatibility. No function in
`croniter.py` exceeds 120 lines.

## Expansion cache

`FieldExpander.expand` caches results keyed by
`(expr_format, hash_id, second_at_beginning, from_timestamp,
from_timestamp_tz, strict, strict_year)` in a bounded module-level dict
(`_expansion_cache`, max 512 entries, oldest evicted). Callers always receive
a deep copy, so cached state cannot be mutated through a returned value.

Random (`r`) hash expressions are **never** cached: `_cache_key` returns
`None` for them, so every expansion draws fresh randomness. Hashed (`H`)
expressions are deterministic for a given `hash_id` and are safe to reuse.

## Stepping hot path benchmark

`benchmarks/bench_step.py` times 10000 forward `get_next()` steps of a
typical every-minute expression (`*/1 * * * *`) from a fixed base time
(2024-01-01 00:00, no wall clock, no sleeps).

The baseline method is re-runnable: `benchmarks/baseline_croniter.py` is the
pre-refactor `src/croniter/croniter.py` (git HEAD before the split), loaded
as a standalone module. `--check` measures baseline and current code
interleaved in the same process (best-of-5 each), so machine load affects
both sides equally; the current code must stay within 15% of the baseline.

Reproduce:

    python3 benchmarks/bench_step.py --check

Latest raw summary (machine-dependent; CPython 3.13.14,
macOS-26.5.2-arm64, arm64, 18 cores):

    baseline (benchmarks/baseline_croniter.py): best=0.0504s (5.04 us/step), raw=[0.0511, 0.051, 0.0535, 0.0504, 0.0659]
    current  (src/croniter):                    best=0.0500s (5.00 us/step), raw=[0.0503, 0.0518, 0.05, 0.0588, 0.0684]
    ratio: 0.992 (limit 1.15)

`benchmarks/baseline_step.json` holds an absolute-time record (with the
recording environment) for reference; regenerate it with
`python3 benchmarks/bench_step.py --save-baseline`. The same comparison runs
in the test suite as
`src/croniter/tests/test_croniter_benchmark.py::SteppingHotPathBenchmarkTest`.

## Behavior guards

`src/croniter/tests/test_croniter_internals.py` pins the behavior of each
stage with fixed base times: 5/6/7-field expressions, seconds and years,
`day_or`, nth weekday, `l` (last), range steps, hash seeds, random
expressions, forward/backward switching, and America/New_York DST (spring
missing hour, fall repeated hour, zoneinfo and pytz).
