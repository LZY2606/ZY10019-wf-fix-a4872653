#!/usr/bin/env python3
"""Benchmark for the croniter stepping hot path.

Scenario: a typical every-minute expression ("*/1 * * * *") stepped forward
10000 times with get_next().  All inputs are fixed (no wall clock, no sleep).

The baseline is the pre-refactor implementation, frozen in
``benchmarks/baseline_croniter.py`` (a re-runnable baseline method: the
original ``src/croniter/croniter.py`` at the commit before the component
split).  ``--check`` measures baseline and current code interleaved in the
same process, so machine load affects both sides equally; the current code
must not be more than 15% slower (best-of-N runs).

Usage:
    python3 benchmarks/bench_step.py                 # measure and compare
    python3 benchmarks/bench_step.py --check         # exit 1 if >15% slower
    python3 benchmarks/bench_step.py --save-baseline # record absolute-time JSON
"""
import argparse
import datetime
import importlib.util
import json
import os
import platform
import sys
import timeit

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(HERE, "baseline_step.json")
BASELINE_CRONITER_PATH = os.path.join(HERE, "baseline_croniter.py")

# Always benchmark this repository's code, not whatever happens to be
# installed in the environment.  CRONITER_BENCH_SRC overrides the source
# tree, which is how the absolute-time JSON was recorded.
SRC = os.environ.get("CRONITER_BENCH_SRC", os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.abspath(SRC))

ITERATIONS = 10000
REPEAT = 5
THRESHOLD = 1.15  # current must not be slower than baseline * 1.15
EXPR = "*/1 * * * *"
BASE_DT = datetime.datetime(2024, 1, 1, 0, 0)


def _load_baseline_module():
    spec = importlib.util.spec_from_file_location(
        "croniter_step_baseline", BASELINE_CRONITER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure_module(croniter_module, iterations=ITERATIONS):
    """Time `iterations` forward steps of the every-minute expression."""

    def run():
        itr = croniter_module.croniter(EXPR, BASE_DT)
        for _ in range(iterations):
            itr.get_next()

    return timeit.Timer(run).timeit(1)


def measure(iterations=ITERATIONS, repeat=REPEAT):
    """Measure only the current code; return (best_seconds, all_runs)."""
    import croniter

    runs = [measure_module(croniter, iterations) for _ in range(repeat)]
    return min(runs), runs


def compare(repeat=REPEAT):
    """Interleaved measurement of frozen baseline vs current code.

    Returns (baseline_best, current_best, baseline_runs, current_runs).
    Alternating the two sides makes the comparison robust to machine load.
    """
    import croniter as current_module

    baseline_module = _load_baseline_module()
    baseline_runs, current_runs = [], []
    for _ in range(repeat):
        baseline_runs.append(measure_module(baseline_module))
        current_runs.append(measure_module(current_module))
    return min(baseline_runs), min(current_runs), baseline_runs, current_runs


def environment():
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save-baseline", action="store_true",
                        help="record absolute-time baseline JSON (informational)")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if current code is >15%% slower than the baseline")
    parser.add_argument("--repeat", type=int, default=REPEAT)
    args = parser.parse_args(argv)

    env = environment()
    print("croniter stepping hot path benchmark")
    print(f"  expression: {EXPR!r}, {ITERATIONS} x get_next(), fixed base {BASE_DT}")
    print(f"  environment: python {env['python']} ({env['implementation']}), "
          f"{env['platform']}, {env['machine']}")

    if args.save_baseline:
        best, runs = measure(repeat=args.repeat)
        data = {
            "description": "absolute-time record for '*/1 * * * *' x 10000 get_next() "
                           "forward steps (informational; pass/fail uses the interleaved "
                           "comparison against benchmarks/baseline_croniter.py)",
            "iterations": ITERATIONS,
            "repeat": args.repeat,
            "best_seconds": best,
            "runs": runs,
            "environment": env,
        }
        with open(BASELINE_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  best={best:.4f}s, raw runs: {[round(r, 4) for r in runs]}")
        print(f"  absolute-time record saved to {BASELINE_PATH}")
        return 0

    base_best, cur_best, base_runs, cur_runs = compare(repeat=args.repeat)
    ratio = cur_best / base_best
    print(f"  repeat={args.repeat} (interleaved, best-of)")
    print(f"  baseline (benchmarks/baseline_croniter.py): best={base_best:.4f}s "
          f"({base_best / ITERATIONS * 1e6:.2f} us/step), raw={[round(r, 4) for r in base_runs]}")
    print(f"  current  (src/croniter):                    best={cur_best:.4f}s "
          f"({cur_best / ITERATIONS * 1e6:.2f} us/step), raw={[round(r, 4) for r in cur_runs]}")
    print(f"  ratio: {ratio:.3f} (limit {THRESHOLD})")
    if args.check and ratio > THRESHOLD:
        print(f"  FAIL: current code is {ratio:.1%} of baseline, over the 15% threshold")
        return 1
    print("  OK: within 15% of baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
