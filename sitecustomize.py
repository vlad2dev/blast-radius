"""Collect reachability from Python subprocesses started by baseline tests."""

import atexit
import json
import os
import sys
from collections import defaultdict
from multiprocessing.reduction import ForkingPickler
from pathlib import Path

owner = os.environ.get("BLAST_ACTIVE_TEST")
if owner:
    from comparison_probe import install
    from default_probe import DefaultReads
    from receiver_probes import RECEIVER_METHODS, receivers

    root = os.environ["BLAST_SOURCE"]
    prefix = root + "/sqlglot/"
    fixture_prefix = root + "/tests/fixtures/"
    target = Path(os.environ["BLAST_CHILD_REPORTS"]) / f"{os.getpid()}.json"
    entered = defaultdict(set)
    opened = defaultdict(set)
    ran_job = False
    receiver_codes = {}
    identity_sites = set()
    monitor = sys.monitoring
    tool = monitor.COVERAGE_ID

    defaults = DefaultReads(Path(__file__).with_name("default_sites.json"), lambda key: entered[owner].add(key))

    def on_enter(code, offset, entry=True):
        if code.co_filename.startswith(prefix):
            path = code.co_filename[len(root) + 1:]
            probe = receiver_codes.get(code)
            if probe is None:
                probe = (path, code.co_qualname) in RECEIVER_METHODS
                receiver_codes[code] = probe
                monitor.set_local_events(tool, code, monitor.events.LINE)
            keys = entered[owner]
            keys.add(f"{path}:{code.co_firstlineno}")
            complete = defaults.observe(f"{path}:{code.co_firstlineno}", code, sys._getframe(1), owner) if entry else True
            if probe:
                for receiver in receivers(type(sys._getframe(1).f_locals["self"])):
                    keys.add(f"receiver/{receiver}")
                return None
            if not complete:
                return None
        return monitor.DISABLE

    def on_resume(code, offset):
        return on_enter(code, offset, entry=False)

    def on_line(code, lineno):
        entered[owner].add(f"{code.co_filename[len(root) + 1:]}#{lineno}")
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
        target.write_text(json.dumps((sorted(identity_sites), data)))

    def record_lookup(key):
        entered[owner].add(key)

    install(Path(root), identity_sites.add, record_lookup)
    monitor.use_tool_id(tool, "blast-radius child reachability")
    monitor.register_callback(tool, monitor.events.PY_START, on_enter)
    monitor.register_callback(tool, monitor.events.PY_RESUME, on_resume)
    monitor.register_callback(tool, monitor.events.LINE, on_line)
    monitor.set_events(tool, monitor.events.PY_START | monitor.events.PY_RESUME)
    sys.addaudithook(on_open)


def run_pool_task(test, payload):
    """Attribute every job to its submitter even when pytest reuses a pool."""
    global owner, ran_job
    if not ran_job:
        entered["@collection"].update(entered.pop(owner, ()))
        opened["@collection"].update(opened.pop(owner, ()))
        defaults.seen.clear()
    ran_job = True
    owner = test
    os.environ["BLAST_ACTIVE_TEST"] = test
    monitor.restart_events()
    func, args, kwargs = ForkingPickler.loads(payload)
    # The worker serializes the return value before starting the next job.
    return func(*args, **kwargs)
