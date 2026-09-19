#!/usr/bin/env python
"""Benchmark guard for the stepping hot path.

Compares 10000 forward get_next() steps of a typical every-minute
expression ("*/1 * * * *") against the frozen pre-refactor implementation
in benchmarks/baseline_croniter.py.  Both sides are measured interleaved
in the same process so machine load affects them equally; the current code
must not be more than 15% slower.

Reproduce / re-check standalone:

    python3 benchmarks/bench_step.py --check

The baseline method is re-runnable: benchmarks/baseline_croniter.py is the
original src/croniter/croniter.py from before the component split, and
benchmarks/baseline_step.json holds an absolute-time record (with the
recording environment) for reference.
"""
import importlib.util
import os
import unittest

from croniter.tests import base

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BENCH_PATH = os.path.join(REPO_ROOT, "benchmarks", "bench_step.py")
BASELINE_CRONITER_PATH = os.path.join(REPO_ROOT, "benchmarks", "baseline_croniter.py")


def _load_bench_module():
    spec = importlib.util.spec_from_file_location("croniter_bench_step", BENCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SteppingHotPathBenchmarkTest(base.TestCase):
    def test_stepping_hot_path_within_15_percent_of_baseline(self):
        if not os.path.exists(BASELINE_CRONITER_PATH):
            self.skipTest("benchmarks/baseline_croniter.py is missing")
        bench = _load_bench_module()
        base_best, cur_best, base_runs, cur_runs = bench.compare(repeat=5)
        ratio = cur_best / base_best
        summary = (
            f"baseline best={base_best:.4f}s raw={[round(r, 4) for r in base_runs]}, "
            f"current best={cur_best:.4f}s raw={[round(r, 4) for r in cur_runs]}, "
            f"ratio={ratio:.3f}, limit=1.15. "
            f"Reproduce: python3 benchmarks/bench_step.py --check"
        )
        self.assertLessEqual(ratio, 1.15, f"stepping hot path regression: {summary}")


if __name__ == "__main__":
    unittest.main()
