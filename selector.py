"""Select regression tests using reachability from the pinned checkout."""

import ast
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path, PurePosixPath
from types import CodeType

DATA = Path("/data")
APP = Path(__file__).resolve().parent
BASE = Path("/opt/sqlglot")
DIFF = re.compile(rb"diff --git a/([\w./-]+) b/\1\n")
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n")
COLLECTION = "@collection"


def require(condition):
    if not condition:
        raise ValueError("unsupported or inconsistent unified diff")


def apply_patch(patch, base):
    """Apply same-path text edits with exact context and hunk length checks."""
    diff = iter(patch.read_bytes().splitlines(keepends=True))
    line = next(diff, b"")
    edits = {}
    while line:
        match = DIFF.fullmatch(line)
        require(match is not None)
        path = match[1].decode("ascii")
        require(".." not in PurePosixPath(path).parts and not path.startswith("/"))
        require(path not in edits)
        line = next(diff, b"")
        if line.startswith(b"index "):
            line = next(diff, b"")
        require(line == b"--- a/" + match[1] + b"\n")
        require(next(diff, b"") == b"+++ b/" + match[1] + b"\n")
        original = (base / path).read_bytes()
        source = original.splitlines(keepends=True)
        result = []
        removed = set()
        inserted = set()
        cursor = 0
        line = next(diff, b"")
        require(line.startswith(b"@@ "))
        while line.startswith(b"@@ "):
            hunk = HUNK.fullmatch(line)
            require(hunk is not None)
            old_start, old_count, new_start, new_count = (
                int(value or b"1") for value in hunk.groups()
            )
            start = old_start - bool(old_count)
            require(cursor <= start <= len(source))
            result.extend(source[cursor:start])
            require(len(result) == new_start - bool(new_count))
            cursor = start
            while old_count or new_count:
                entry = next(diff, b"")
                mark, contents = entry[:1], entry[1:]
                require(mark in (b" ", b"-", b"+") and contents.endswith(b"\n"))
                if mark != b"+":
                    require(old_count > 0 and cursor < len(source) and source[cursor] == contents)
                    cursor += 1
                    old_count -= 1
                if mark != b"-":
                    require(new_count > 0)
                    result.append(contents)
                    new_count -= 1
                if mark == b"-":
                    removed.add(cursor)
                if mark == b"+":
                    inserted.add(len(result))
            line = next(diff, b"")
        result.extend(source[cursor:])
        edits[path] = (original, b"".join(result), removed, inserted)
    require(edits)
    return edits


class FunctionBodies(ast.NodeTransformer):
    """Compare the enclosing code while locating bodies eligible for selection."""

    def __init__(self, source):
        self.lines = source.splitlines()
        self.functions = []

    def visit_FunctionDef(self, node):
        first = min((d.lineno for d in node.decorator_list), default=node.lineno)
        body = node.body
        has_docstring = ast.get_docstring(node, clean=False) is not None
        start = body[0].lineno
        while start > node.lineno + 1:
            preceding = self.lines[start - 2].strip()
            if preceding and not preceding.startswith(b"#"):
                break
            start -= 1
        self.functions.append((first, start, node.end_lineno))
        node.body = body[:1] if has_docstring else []
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.functions.append((node.lineno, node.body.lineno, node.end_lineno))
        node.body = ast.Constant(None)
        return node


def external_code(code):
    """Keep metadata that can affect a function before its body is entered."""
    result = {}
    for child in code.co_consts:
        if isinstance(child, CodeType):
            result[child.co_firstlineno] = (
                child.co_qualname, child.co_flags, child.co_freevars,
            )
            if not child.co_flags & inspect.CO_NEWLOCALS:
                result.update(external_code(child))
    return result


def changed_functions(path, edit):
    original, modified, removed, inserted = edit
    before = FunctionBodies(original)
    after = FunctionBodies(modified)
    left_code = external_code(compile(original, path, "exec"))
    right_code = external_code(compile(modified, path, "exec"))
    left = ast.dump(before.visit(ast.parse(original, filename=path)))
    right = ast.dump(after.visit(ast.parse(modified, filename=path)))
    if left != right:
        return None

    affected = set()
    for (old_first, old_body, old_end), (new_first, new_body, new_end) in zip(
        before.functions, after.functions, strict=True
    ):
        old_lines = {line for line in removed if old_body <= line <= old_end}
        new_lines = {line for line in inserted if new_body <= line <= new_end}
        if old_lines or new_lines:
            if old_first not in left_code or left_code[old_first] != right_code.get(new_first):
                return None
            affected.add(f"{path}:{old_first}")
            removed = removed - old_lines
            inserted = inserted - new_lines
    return affected if affected and not (removed or inserted) else None


def candidate_tests(edits, mapping, by_file, all_ids):
    selected = set(mapping["failed"])
    for path, edit in edits.items():
        if path.startswith("tests/fixtures/") and path in mapping["fixtures"]:
            for owner in mapping["fixtures"][path]:
                if owner == COLLECTION:
                    return all_ids
                selected.update(by_file.get(owner, (owner,)))
        elif path.startswith("sqlglot/") and path.endswith(".py"):
            keys = changed_functions(path, edit)
            if keys is None:
                return all_ids
            owners = set(mapping.get("source_reads", {}).get(path, ()))
            for key in keys:
                owners.update(mapping["coverage"].get(key, ()))
            if COLLECTION in owners:
                return all_ids
            for owner in owners:
                selected.update(by_file.get(owner, (owner,)))
        else:
            return all_ids
    return selected & all_ids


def schedule(ids, tests):
    items = sorted(
        ((tests[nodeid][1], tests[nodeid][0]) for nodeid in ids),
        key=lambda item: (-item[0], item[1]),
    )
    best_workers = []
    best_cost = 0
    for count in range(1, min(8, len(items)) + 1):
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for duration, number in items:
            index = loads.index(min(loads))
            workers[index].append(number)
            loads[index] += duration
        cost = max(loads) + 2000 * count
        if not best_workers or cost < best_cost:
            best_workers, best_cost = workers, cost
    return best_workers, best_cost


def confirm(edits, candidates, all_ids, by_file, timeout):
    """Return observed failures only when pytest completed the requested run."""
    with tempfile.TemporaryDirectory(prefix="blast-") as scratch:
        scratch = Path(scratch)
        tree = scratch / "source"
        # Keep unchanged files on the read-only layer and break links for edits.
        shutil.copytree(BASE, tree, copy_function=os.symlink)
        for path, (_, content, _, _) in edits.items():
            target = tree / path
            target.unlink()
            target.write_bytes(content)
        result = scratch / "result.json"
        selection = scratch / "selected.json"
        selection.write_text(json.dumps(sorted(candidates)))
        env = dict(
            os.environ, PYTHONPATH=f"{APP}:{tree}", BLAST_RESULT=str(result),
            BLAST_CANDIDATES=str(selection),
        )
        command = [
            sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider",
            "-p", "test_observer", "--continue-on-collection-errors", "tests",
        ]
        with subprocess.Popen(
            command, cwd=tree, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        ) as run:
            try:
                exit_code = run.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(run.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise
        if exit_code not in (0, 1, 5) or not result.is_file():
            return candidates
        data = json.loads(result.read_text())
        failures = set(data["failed"])
        if exit_code == 1 and not (failures or data["collection_errors"]):
            return candidates
        for path in data["collection_errors"]:
            if path not in by_file:
                return all_ids
            failures.update(by_file[path])
        return (failures | (candidates - set(data["finished"]))) & all_ids


def main():
    tests = {}
    by_file = defaultdict(set)
    for test in json.loads((DATA / "tests.json").read_text())["tests"]:
        tests[test["id"]] = (test["number"], round(test["seconds"] * 1000))
        by_file[test["file"]].add(test["id"])
    all_ids = set(tests)
    all_workers, all_cost = schedule(all_ids, tests)
    mapping = json.loads((APP / "reachability.json").read_text())
    healthy = not mapping["collection_errors"] and all_ids <= set(mapping["finished"])
    confirm_candidates = os.environ.get("BLAST_CONFIRM") == "1"
    deadline = time.monotonic() + 145
    for patch in sorted((DATA / "commits").glob("*.patch")):
        workers, cost = all_workers, all_cost
        if healthy:
            try:
                edits = apply_patch(patch, BASE)
                candidates = candidate_tests(edits, mapping, by_file, all_ids)
                workers, cost = schedule(candidates, tests)
            except (OSError, ValueError, SyntaxError, RecursionError):
                candidates = all_ids
                edits = None
            if confirm_candidates and edits and 1 < len(candidates) < len(all_ids):
                estimate = 1.1 + 0.65 * sum(tests[nodeid][1] for nodeid in candidates) / 1000
                timeout = min(12, 2 * estimate + 1, deadline - time.monotonic())
                if cost / 1000 > estimate + 1 and timeout > estimate:
                    try:
                        verified = confirm(edits, candidates, all_ids, by_file, timeout)
                        workers, cost = schedule(verified, tests)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
