"""Repository-root pytest configuration.

Ensures the in-repo ``src`` layout is importable even when the surrounding
Python environment does not have this checkout installed (or has a stale
editable install pointing elsewhere), and prints a terminal summary of the
behavior-guard / validation phases added with the internal component split.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

GUARD_MODULE_MARKERS = (
    "test_behavior_guards",
    "test_components",
    "test_step_benchmark",
)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tr = terminalreporter
    seen = set()
    guard_ids = []
    for reports in tr.stats.values():
        for report in reports:
            if report.when != "call" or report.outcome != "passed":
                continue
            if any(marker in report.nodeid for marker in GUARD_MODULE_MARKERS):
                if report.nodeid not in seen:
                    seen.add(report.nodeid)
                    guard_ids.append(report.nodeid)
    if not guard_ids:
        return
    tr.write_sep("=", "behavior guards & validation phases")
    for nodeid in sorted(guard_ids):
        tr.write_line(f"PASSED {nodeid}")
    perf_path = ROOT / "benchmarks" / "last_run.json"
    if perf_path.exists():
        try:
            perf = json.loads(perf_path.read_text())
        except ValueError:
            return
        tr.write_line(
            "PERF get_next x{iterations}: min {cur_min:.4f}s vs baseline min "
            "{base_min:.4f}s (ratio {ratio:.3f}, limit {limit:.2f}x); "
            "median {median:.4f}s [{env}]".format(
                iterations=perf["iterations"],
                cur_min=perf["min_seconds"],
                base_min=perf["baseline_min_seconds"],
                ratio=perf["ratio"],
                limit=perf["tolerance"],
                median=perf["median_seconds"],
                env=perf["environment"],
            )
        )
