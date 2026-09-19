#!/usr/bin/env python
"""Performance guard for the get_next() hot path.

Compares the current stepping speed against the recorded pre-refactor
baseline (benchmarks/baseline_step.json). The baseline method is fully
reproducible: `python3 benchmarks/bench_step.py --save-baseline`
(see benchmarks/BASELINE.md for the method, raw numbers and environment).

Performance is machine-dependent: the recorded baseline was captured on
macOS 26.5.2 / Apple M5 Max / CPython 3.13.14. No sleep and no wall-clock
reads are involved in the measured workload itself (fixed base time).
"""

import importlib.util
import json
import statistics
import sys
import unittest
from pathlib import Path

from croniter.tests import base

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCH_PATH = REPO_ROOT / "benchmarks" / "bench_step.py"
BASELINE_PATH = REPO_ROOT / "benchmarks" / "baseline_step.json"
LAST_RUN_PATH = REPO_ROOT / "benchmarks" / "last_run.json"


def _load_bench_module():
    spec = importlib.util.spec_from_file_location("bench_step", BENCH_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_step", module)
    spec.loader.exec_module(module)
    return module


class StepBenchmarkTest(base.TestCase):
    def test_get_next_hot_path_within_15_percent_of_baseline(self):
        bench = _load_bench_module()
        baseline = json.loads(BASELINE_PATH.read_text())
        base_median = baseline["median_seconds"]
        tolerance = baseline.get("tolerance", 1.15)

        samples = bench.measure()
        median = statistics.median(samples)
        current_min = min(samples)
        base_min = baseline.get("min_seconds", base_median)
        ratio = current_min / base_min

        env = bench.environment()
        LAST_RUN_PATH.write_text(
            json.dumps(
                {
                    "iterations": bench.ITERATIONS,
                    "repeats": bench.REPEATS,
                    "samples_seconds": [round(s, 6) for s in samples],
                    "median_seconds": round(median, 6),
                    "min_seconds": round(current_min, 6),
                    "baseline_median_seconds": base_median,
                    "baseline_min_seconds": base_min,
                    "ratio": round(ratio, 4),
                    "tolerance": tolerance,
                    "environment": (
                        f"{env['implementation']} {env['python']}, {env['platform']}"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        # Gate on min-of-repeats: it is the least load-sensitive estimator of
        # true code speed, so the 15% budget is not eaten by machine noise.
        self.assertLessEqual(
            current_min,
            base_min * tolerance,
            f"get_next hot path regression: min {current_min:.4f}s vs baseline min "
            f"{base_min:.4f}s (ratio {ratio:.3f} > {tolerance:.2f}); "
            f"median {median:.4f}s vs baseline median {base_median:.4f}s; "
            f"baseline environment: {baseline['environment']}",
        )


if __name__ == "__main__":
    unittest.main()
