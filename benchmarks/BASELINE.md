# Hot-path baseline (`get_next` forward stepping)

## Method (reproducible)

From the repository root:

    python3 benchmarks/bench_step.py            # measure and print a JSON summary
    python3 benchmarks/bench_step.py --check    # compare against baseline_step.json (fails if >15% slower)

Fixed workload, no wall-clock or sleep involved in the measured code:
expression `*/1 * * * *`, base time `datetime(2010, 1, 23, 12, 18)`,
10000 consecutive `get_next()` calls (ret_type=float), 5 repeats, min/median reported.

## Baseline capture (pre-refactor)

- Commit: `d5b35bc` ("Restore seed workflows to initial snapshot")
- Environment: macOS 26.5.2, Apple M5 Max (arm64), CPython 3.13.14
- Command: `python3 benchmarks/bench_step.py --save-baseline`

Raw summary:

```json
{
  "expr": "*/1 * * * *",
  "base_time": "2010-01-23T12:18:00",
  "iterations": 10000,
  "repeats": 5,
  "samples_seconds": [0.055741, 0.054607, 0.056486, 0.05487, 0.052601],
  "min_seconds": 0.052601,
  "median_seconds": 0.05487
}
```

Baseline median: **0.054870 s**, baseline min: **0.052601 s** for 10000
iterations (~5.3-5.5 µs per `get_next`).

The regression gate compares **min-of-repeats** against the baseline min,
because the min is the least load-sensitive estimator of true code speed
(medians are reported for context). The 15% budget therefore allows a min
up to 0.060491 s on this machine. The pytest guard
`src/croniter/tests/test_step_benchmark.py` applies the same gate and
writes its raw numbers to `benchmarks/last_run.json`.

Performance numbers are machine-dependent; re-record the baseline with
`--save-baseline` when moving to a different host, and note the new environment here.
