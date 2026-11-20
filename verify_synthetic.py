"""Generate independent edits and label them with a full pytest run."""
import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--scope", action="store_true", help="verify class and comparison selection")
parser.add_argument("--case", help="label one named mutation in a separate directory")
args = parser.parse_args()

BASE = Path("/opt/sqlglot")
suite = "scope" if args.scope else "synthetic"
OUT = Path("/verification") / (f"case-{args.case}" if args.case else suite)
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
if args.scope:
    mutations = [
        ("dialect-week", "sqlglot/dialects/bigquery.py",
         "WEEK_OFFSET = -1", "WEEK_OFFSET = -4"),
        ("nested-tokenizer", "sqlglot/dialects/postgres.py",
         '"u": (16, 4, 4, 0xFFFF),', '"u": (17, 4, 4, 0xFFFF),'),
        ("nested-delimiter", "sqlglot/dialects/postgres.py",
         "        UNICODE_STRINGS = [(\"U&'\", \"'\"), (\"u&'\", \"'\")]",
         "        UNICODE_STRINGS = [(\"Q&'\", \"'\"), (\"q&'\", \"'\")]"),
        ("dispatch-closure", "sqlglot/parsers/mysql.py",
         '"FULL COLUMNS": _show_parser("COLUMNS", target="FROM", full=True),',
         '"FULL COLUMNS": _show_parser("COLUMNS", target="FROM", full=False),'),
        ("comparison-overload", "sqlglot/planner.py",
         "if self.limit is not math.inf:", "if self.limit != math.inf:"),
    ]
if args.case:
    mutations = [mutation for mutation in mutations if mutation[0] == args.case]
    if not mutations:
        parser.error(f"unknown case in {suite}: {args.case}")
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
