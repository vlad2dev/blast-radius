"""Collect test reachability or report failures with pytest's native hooks."""

import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

ROOT = Path.cwd()
SOURCE_PREFIX = str(ROOT / "sqlglot") + "/"
FIXTURE_PREFIX = str(ROOT / "tests" / "fixtures") + "/"
OUTPUT = Path(os.environ["BLAST_RESULT"])
CHILD_REPORTS = OUTPUT.with_suffix(".children")
BUILD = os.environ.get("BLAST_COLLECT") == "1"
COLLECTION = "@collection"

active_test = COLLECTION
coverage = defaultdict(set)
fixtures = defaultdict(set)
collected = set()
source_reads = defaultdict(set)
finished = set()
failures = set()
collection_errors = set()


def on_enter(code, offset):
    if code.co_filename.startswith(SOURCE_PREFIX):
        path = code.co_filename[len(str(ROOT)) + 1 :]
        coverage[f"{path}:{code.co_firstlineno}"].add(active_test)
    return sys.monitoring.DISABLE


def on_audit(event, args):
    if event != "open" or not isinstance(args[0], (str, bytes)):
        return
    path = os.path.abspath(os.fsdecode(args[0]))
    under_fixtures = path.startswith(FIXTURE_PREFIX)
    reading_source = path.startswith(SOURCE_PREFIX) and path.endswith(".py")
    if not under_fixtures and not reading_source:
        return
    frame = sys._getframe(1)
    if reading_source and frame.f_code.co_filename.startswith("<frozen importlib"):
        return
    owner = active_test
    if owner == COLLECTION:
        while frame is not None:
            filename = frame.f_code.co_filename
            if filename.startswith(str(ROOT / "tests") + "/test"):
                owner = filename[len(str(ROOT)) + 1 :]
                break
            frame = frame.f_back
    reads = fixtures if under_fixtures else source_reads
    reads[path[len(str(ROOT)) + 1 :]].add(owner)


if BUILD:
    from sitecustomize import run_pool_task

    submit = ProcessPoolExecutor.submit

    def submit_observed(pool, func, /, *args, **kwargs):
        return submit(pool, run_pool_task, active_test, func, args, kwargs)

    ProcessPoolExecutor.submit = submit_observed
    CHILD_REPORTS.mkdir()
    os.environ.update(BLAST_SOURCE=str(ROOT), BLAST_CHILD_REPORTS=str(CHILD_REPORTS))
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID
    monitor.use_tool_id(tool, "blast-radius reachability")
    monitor.register_callback(tool, monitor.events.PY_START, on_enter)
    monitor.register_callback(tool, monitor.events.PY_RESUME, on_enter)
    monitor.set_events(tool, monitor.events.PY_START | monitor.events.PY_RESUME)
    sys.addaudithook(on_audit)


def pytest_collection_finish(session):
    collected.update(item.nodeid for item in session.items)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items, config):
    if BUILD or not os.environ.get("BLAST_CANDIDATES"):
        return
    wanted = set(json.loads(Path(os.environ["BLAST_CANDIDATES"]).read_text()))
    deselected = [item for item in items if item.nodeid not in wanted]
    items[:] = [item for item in items if item.nodeid in wanted]
    config.hook.pytest_deselected(items=deselected)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    global active_test
    if BUILD:
        active_test = item.nodeid
        os.environ["BLAST_ACTIVE_TEST"] = item.nodeid
        monitor.restart_events()
    try:
        return (yield)
    finally:
        active_test = COLLECTION
        if BUILD:
            os.environ.pop("BLAST_ACTIVE_TEST", None)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    global active_test
    if not BUILD:
        return (yield)
    active_test = item.nodeid.split("::", 1)[0]
    monitor.restart_events()
    try:
        return (yield)
    finally:
        active_test = item.nodeid
        monitor.restart_events()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    return (yield from pytest_runtest_setup(item))


def pytest_runtest_logreport(report):
    if report.failed:  # Includes pytest 9's failed SubtestReport.
        failures.add(report.nodeid)
    if report.when == "teardown" and not getattr(report, "context", None):
        finished.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        collection_errors.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    data = {
        "collected": sorted(collected),
        "finished": sorted(finished),
        "failed": sorted(failures),
        "collection_errors": sorted(collection_errors),
    }
    if BUILD:
        for report in CHILD_REPORTS.glob("*.json"):
            for owner, entered, opened in json.loads(report.read_text()):
                for key in entered:
                    coverage[key].add(owner)
                for path in opened:
                    fixtures[path].add(owner)
            report.unlink()
        CHILD_REPORTS.rmdir()
        data["coverage"] = {key: sorted(values) for key, values in coverage.items()}
        data["source_reads"] = {key: sorted(values) for key, values in source_reads.items()}
        data["fixtures"] = {key: sorted(values) for key, values in fixtures.items()}
    OUTPUT.write_text(json.dumps(data, separators=(",", ":")))
