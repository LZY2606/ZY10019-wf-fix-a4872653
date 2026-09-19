"""Pytest hooks: surface the refactor validation stages in `-q` output."""

_STAGE_MODULES = ("test_croniter_internals", "test_croniter_benchmark")
_collected_stage_items = []


def pytest_collection_modifyitems(items):
    del _collected_stage_items[:]
    for item in items:
        if any(stage in item.nodeid for stage in _STAGE_MODULES):
            _collected_stage_items.append(item.nodeid)


def pytest_terminal_summary(terminalreporter):
    if not _collected_stage_items:
        return
    terminalreporter.write_sep("=", "croniter refactor validation stages")
    for nodeid in _collected_stage_items:
        terminalreporter.write_line(f"  {nodeid}")
