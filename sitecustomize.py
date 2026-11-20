"""Collect reachability from Python subprocesses started by baseline tests."""

import atexit
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

owner = os.environ.get("BLAST_ACTIVE_TEST")
if owner:
    root = os.environ["BLAST_SOURCE"]
    prefix = root + "/sqlglot/"
    fixture_prefix = root + "/tests/fixtures/"
    target = Path(os.environ["BLAST_CHILD_REPORTS"]) / f"{os.getpid()}.json"
    entered = defaultdict(set)
    opened = defaultdict(set)
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID

    def on_enter(code, offset):
        if code.co_filename.startswith(prefix):
            entered[owner].add(f"{code.co_filename[len(root) + 1:]}:{code.co_firstlineno}")
        return monitor.DISABLE

    def on_open(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path.startswith(fixture_prefix):
                opened[owner].add(path[len(root) + 1:])

    @atexit.register
    def save():
        data = [
            (key, sorted(entered.get(key, ())), sorted(opened.get(key, ())))
            for key in entered.keys() | opened.keys()
        ]
        target.write_text(json.dumps(data))

    monitor.use_tool_id(tool, "blast-radius child reachability")
    monitor.register_callback(tool, monitor.events.PY_START, on_enter)
    monitor.register_callback(tool, monitor.events.PY_RESUME, on_enter)
    monitor.set_events(tool, monitor.events.PY_START | monitor.events.PY_RESUME)
    sys.addaudithook(on_open)


def run_pool_task(test, func, args, kwargs):
    """Attribute every job to its submitter even when pytest reuses a pool."""
    global owner
    start = owner
    if owner != "@collection":
        entered["@collection"].update(entered.pop(owner, ()))
        opened["@collection"].update(opened.pop(owner, ()))
    owner = test
    os.environ["BLAST_ACTIVE_TEST"] = test
    monitor.restart_events()
    try:
        return func(*args, **kwargs)
    finally:
        owner = "@collection"
        os.environ["BLAST_ACTIVE_TEST"] = start
        monitor.restart_events()
