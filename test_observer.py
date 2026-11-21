"""Collect test reachability or report failures with pytest's native hooks."""

import ast
import json
import os
import sys
import unittest
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.reduction import ForkingPickler
from pathlib import Path

import pytest

from receiver_probes import RECEIVER_METHODS, receivers

ROOT = Path.cwd()
SOURCE_START = len(str(ROOT)) + 1
SOURCE_PREFIX = str(ROOT / "sqlglot") + "/"
FIXTURE_PREFIX = str(ROOT / "tests" / "fixtures") + "/"
OUTPUT = Path(os.environ["BLAST_RESULT"])
CHILD_REPORTS = OUTPUT.with_suffix(".children")
BUILD = os.environ.get("BLAST_COLLECT") == "1"
COLLECTION = "@collection"

active_test = COLLECTION
coverage = defaultdict(set)
receiver_codes = {}
fixtures = defaultdict(set)
identity_sites = set()
collected = set()
plain_unittest = set()
source_reads = defaultdict(set)
finished = set()
failures = set()
collection_errors = set()


def on_enter(code, offset):
    if code.co_filename.startswith(SOURCE_PREFIX):
        path = code.co_filename[SOURCE_START :]
        probe = receiver_codes.get(code)
        if probe is None:
            probe = (path, code.co_qualname) in RECEIVER_METHODS
            receiver_codes[code] = probe
            monitor.set_local_events(tool, code, monitor.events.LINE)
        coverage[f"{path}:{code.co_firstlineno}"].add(active_test)
        if probe:
            observed = receivers(type(sys._getframe(1).f_locals["self"]))
            for receiver in observed:
                coverage[f"receiver/{receiver}"].add(active_test)
            return None
    return sys.monitoring.DISABLE


def on_line(code, lineno):
    path = code.co_filename[SOURCE_START :]
    coverage[f"{path}#{lineno}"].add(active_test)
    return monitor.DISABLE


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
                owner = filename[SOURCE_START :]
                break
            frame = frame.f_back
    reads = fixtures if under_fixtures else source_reads
    reads[path[SOURCE_START :]].add(owner)


if BUILD:
    from comparison_probe import install
    from sitecustomize import run_pool_task

    def record_lookup(key):
        coverage[key].add(active_test)

    install(ROOT, identity_sites.add, record_lookup)

    submit = ProcessPoolExecutor.submit

    def submit_observed(pool, func, /, *args, **kwargs):
        # Send opaque arguments so unpickling SQL expressions runs under the
        # submitting test in the worker as well.
        payload = bytes(ForkingPickler.dumps((func, args, kwargs)))
        return submit(pool, run_pool_task, active_test, payload)

    ProcessPoolExecutor.submit = submit_observed
    CHILD_REPORTS.mkdir()
    os.environ.update(BLAST_SOURCE=str(ROOT), BLAST_CHILD_REPORTS=str(CHILD_REPORTS))
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID
    monitor.use_tool_id(tool, "blast-radius reachability")
    monitor.register_callback(tool, monitor.events.PY_START, on_enter)
    monitor.register_callback(tool, monitor.events.PY_RESUME, on_enter)
    monitor.register_callback(tool, monitor.events.LINE, on_line)
    monitor.set_events(tool, monitor.events.PY_START | monitor.events.PY_RESUME)
    sys.addaudithook(on_audit)


def pytest_collection_finish(session):
    collected.update(item.nodeid for item in session.items)
    if BUILD:
        for item in session.items:
            cls = getattr(item, "cls", None)
            if not isinstance(cls, type) or not issubclass(cls, unittest.TestCase):
                continue
            if item.nodeid.count("::") != 2 or "[" in item.nodeid or list(item.iter_markers()):
                continue
            # Keep pytest-only fixtures and autouse hooks in the full selector.
            fixtures = {"request", "_unittest_setUpClass_fixture_" + cls.__qualname__}
            if set(item.fixturenames) - fixtures:
                continue
            plain_unittest.add(item.nodeid)


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


def index_test_references():
    """Index lexical references once while the pinned catalog is available."""
    files = defaultdict(set)
    for nodeid in collected:
        files[nodeid.split("::", 1)[0]].add(nodeid)
    references = defaultdict(set)
    for filename, ids in files.items():
        methods = defaultdict(set)
        for nodeid in ids:
            methods[nodeid.rsplit("::", 1)[-1].split("[", 1)[0]].add(nodeid)
        pending = [(ast.parse((ROOT / filename).read_bytes()), None)]
        while pending:
            node, owner = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                owner = node.name
            symbol = None
            if isinstance(node, ast.Name):
                symbol = node.id
            elif isinstance(node, ast.Attribute):
                symbol = node.attr
            elif isinstance(node, ast.alias) and node.asname is not None:
                symbol = node.name
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                symbol = node.value
            if symbol:
                references[symbol].update(ids if owner is None else methods[owner])
            pending.extend((child, owner) for child in ast.iter_child_nodes(node))
    return {name: sorted(ids) for name, ids in references.items()}


def pytest_sessionfinish(session, exitstatus):
    data = {
        "collected": sorted(collected),
        "finished": sorted(finished),
        "failed": sorted(failures),
        "collection_errors": sorted(collection_errors),
    }
    if BUILD:
        for report in CHILD_REPORTS.glob("*.json"):
            child_sites, child_observations = json.loads(report.read_text())
            identity_sites.update(child_sites)
            for owner, entered, opened in child_observations:
                for key in entered:
                    coverage[key].add(owner)
                for path in opened:
                    fixtures[path].add(owner)
            report.unlink()
        CHILD_REPORTS.rmdir()
        owners = sorted(set().union(*coverage.values()))
        numbers = {owner: number for number, owner in enumerate(owners)}
        data["owners"] = owners
        data["test_references"] = index_test_references()
        data["plain_unittest"] = sorted(plain_unittest - failures)
        data["dictionary_lookups"] = [
            kind for kind in ("numeric-escape", "numeric-origin", "keyword", "parser-rule")
            if any(key.startswith(kind + "/") for key in coverage)
        ]
        # This table describes observed operand semantics at source locations.
        # It contains no test identities or outcomes from patched executions.
        data["proven_comparisons"] = {
            key: True for key in sorted(identity_sites)
            if not key.endswith("/unproven") and key + "/unproven" not in identity_sites
        }
        data["coverage"] = {
            key: sorted(numbers[owner] for owner in values) for key, values in coverage.items()
        }
        data["source_reads"] = {key: sorted(values) for key, values in source_reads.items()}
        data["fixtures"] = {key: sorted(values) for key, values in fixtures.items()}
    OUTPUT.write_text(json.dumps(data, separators=(",", ":")))
