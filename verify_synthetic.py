"""Generate independent edits and label them with a full pytest run."""
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path("/opt/sqlglot")
OUT = Path("/verification/synthetic")
OUT.mkdir(exist_ok=True)
(OUT / "commits").mkdir(exist_ok=True)
catalog = Path("/data/tests.json")
shutil.copy(catalog, OUT / "tests.json")
tests = json.loads(catalog.read_text())["tests"]
ids = {test["id"]: test["number"] for test in tests}
mutations = [
    ("dialect-body", "sqlglot/generators/sqlite.py",
     'func = "DATETIME" if', 'func = "TIME" if'),
    ("tokenizer-constant", "sqlglot/tokens.py",
     '"::": TokenType.DCOLON,', '"::": TokenType.COLON,'),
    ("source-comment", "sqlglot/generators/sqlite.py",
     'unit = expression.args.get("unit")\n        # An INTERVAL',
     'unit = expression.args.get("unit")\n        # Preserve a supplied unit.\n        # An INTERVAL'),
    ("test-import", "tests/test_build.py",
     "import unittest\n", "import unittest\nimport blast_deliberately_missing\n"),
]
answers = {}
for name, filename, old, new in mutations:
    source = (BASE / filename).read_text()
    assert source.count(old) == 1, filename
    changed = source.replace(old, new)
    diff = difflib.unified_diff(source.splitlines(keepends=True), changed.splitlines(keepends=True),
                               fromfile=f"a/{filename}", tofile=f"b/{filename}")
    (OUT / "commits" / f"{name}.patch").write_text(f"diff --git a/{filename} b/{filename}\n" + "".join(diff))
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "src"
        shutil.copytree(BASE, root)
        (root / filename).write_text(changed)
        report_file = Path(temp) / "report.json"
        env = dict(os.environ, PYTHONPATH=f"/app:{root}", BLAST_RESULT=str(report_file))
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider",
             "-p", "test_observer", "--continue-on-collection-errors", "tests"],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=150,
        )
        if run.returncode not in (0, 1):
            raise RuntimeError(run.stdout)
        report = json.loads(report_file.read_text())
        error_files = set(report["collection_errors"])
        assert error_files <= {test["file"] for test in tests}, error_files
        required = {test["id"] for test in tests if test["file"] not in error_files}
        assert required <= set(report["finished"]), sorted(required - set(report["finished"]))[:20]
        failures = set(report["failed"])
        failures.update(test["id"] for test in tests if test["file"] in error_files)
        answers[name] = {"failing": sorted(ids[key] for key in failures if key in ids), "kind": "synthetic"}
        print(f"{name}: {len(answers[name]['failing'])} failing, {time.monotonic()-started:.2f} s",
              flush=True)
    (OUT / "answers.json").write_text(json.dumps({"commits": answers}, indent=2))
