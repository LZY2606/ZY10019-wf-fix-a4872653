#!/usr/bin/env python3
"""Reproducible benchmark for the croniter get_next() hot path.

Method (kept stable so results are comparable across commits):
  * expression: "*/1 * * * *" (typical every-minute schedule)
  * fixed base time: datetime(2010, 1, 23, 12, 18) -- no wall clock, no sleep
  * workload: 10000 consecutive get_next() calls (ret_type=float)
  * 5 repeats, report min/median seconds per repeat
  * the regression gate compares min-of-repeats (least load-sensitive
    estimator); medians are reported for context

Usage (run from the repository root):
  python3 benchmarks/bench_step.py                  # measure and print a summary
  python3 benchmarks/bench_step.py --save-baseline  # also write benchmarks/baseline_step.json
  python3 benchmarks/bench_step.py --check          # exit 1 if median > baseline * 1.15
"""

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
BASELINE_PATH = Path(__file__).resolve().parent / "baseline_step.json"

EXPR = "*/1 * * * *"
BASE_TIME = datetime(2010, 1, 23, 12, 18)
ITERATIONS = 10000
REPEATS = 5
TOLERANCE = 1.15  # fail the check when more than 15% slower than baseline


def measure(iterations=ITERATIONS, repeats=REPEATS):
    from croniter import croniter

    samples = []
    for _ in range(repeats):
        itr = croniter(EXPR, BASE_TIME)
        start = time.perf_counter()
        for _ in range(iterations):
            itr.get_next()
        samples.append(time.perf_counter() - start)
    return samples


def environment():
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    samples = measure()
    median = statistics.median(samples)
    summary = {
        "expr": EXPR,
        "base_time": BASE_TIME.isoformat(),
        "iterations": ITERATIONS,
        "repeats": REPEATS,
        "samples_seconds": [round(s, 6) for s in samples],
        "min_seconds": round(min(samples), 6),
        "median_seconds": round(median, 6),
        "environment": environment(),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

    if args.save_baseline:
        baseline = dict(summary)
        baseline["tolerance"] = TOLERANCE
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        print(f"baseline written to {BASELINE_PATH}")

    if args.check:
        baseline = json.loads(BASELINE_PATH.read_text())
        tolerance = baseline.get("tolerance", TOLERANCE)
        base_min = baseline.get("min_seconds", baseline["median_seconds"])
        base_median = baseline["median_seconds"]
        current_min = min(samples)
        limit = base_min * tolerance
        ratio = current_min / base_min
        print(
            f"check: min {current_min:.4f}s vs baseline min {base_min:.4f}s "
            f"(ratio {ratio:.3f}, limit {tolerance:.2f}x = {limit:.4f}s); "
            f"median {median:.4f}s vs baseline median {base_median:.4f}s"
        )
        if current_min > limit:
            print("PERF REGRESSION: hot path is more than 15% slower than baseline")
            return 1
        print("PERF OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
