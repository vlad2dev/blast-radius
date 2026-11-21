"""Run baseline-approved, small unittest selections on a patched checkout."""

import importlib
import io
import json
import sys
import unittest
from pathlib import Path


def main():
    requested = json.loads(Path(sys.argv[1]).read_text())
    selected = []
    for nodeid in requested:
        path, class_name, method = nodeid.split("::")
        module = importlib.import_module(path.removesuffix(".py").replace("/", "."))
        fixture = getattr(module, class_name)
        if not issubclass(fixture, unittest.TestCase):
            return 2
        selected.append(fixture(method))

    suite = unittest.TestSuite(selected)
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    if result.testsRun != len(selected) or result.skipped or result.expectedFailures or result.unexpectedSuccesses:
        return 2
    by_identity = dict(zip(map(id, selected), requested))
    failed = set()
    for test, _ in result.failures + result.errors:
        # Subtest failures hold their parent's TestCase in test_case.
        nodeid = by_identity.get(id(getattr(test, "test_case", test)))
        if nodeid is None:
            return 2
        failed.add(nodeid)
    Path(sys.argv[2]).write_text(json.dumps(sorted(failed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
