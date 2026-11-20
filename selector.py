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
from functools import cache
from pathlib import Path, PurePosixPath
from types import CodeType

from comparison_probe import identity_site

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


def statement_lines(path, original, modified, removed, inserted):
    """Use line coverage when the patch changes one existing statement header."""
    if removed != inserted or not removed:
        return ()
    old_tree = ast.parse(original, filename=path)
    new_tree = ast.parse(modified, filename=path)

    def replace_header(tree):
        choices = []
        for node in ast.walk(tree):
            if not isinstance(node, (
                ast.Assert, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr,
                ast.If, ast.Return, ast.Raise, ast.While, ast.For,
            )):
                continue
            body = [child.lineno for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt)]
            end = min(body) - 1 if body else node.end_lineno
            if node.lineno <= min(removed) <= max(removed) <= end:
                choices.append((node.lineno, end, node))
        if not choices:
            return (), None
        start, end, node = min(choices, key=lambda item: item[1] - item[0])
        # Keep suites intact so changed indentation or an else branch widens.
        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.expr):
                setattr(node, field, ast.Constant(None))
        return tuple(range(start, end + 1)), type(node)

    old_lines, old_type = replace_header(old_tree)
    new_lines, new_type = replace_header(new_tree)
    if old_lines == new_lines and old_type == new_type and ast.dump(old_tree) == ast.dump(new_tree):
        return old_lines
    return ()


def safe_identity_edit(path, source, updated, mapping):
    """Prove the observed pure comparisons agree with identity at every changed site."""
    old = ast.parse(source)
    new = ast.parse(updated)
    new_comparisons = {
        (node.lineno, node.col_offset): node
        for node in ast.walk(new) if isinstance(node, ast.Compare)
    }
    changed = False
    for node in ast.walk(old):
        if not identity_site(node):
            continue
        counterpart = new_comparisons.get((node.lineno, node.col_offset))
        if counterpart is None or not isinstance(
            counterpart.ops[0], ast.Eq if isinstance(node.ops[0], ast.Is) else ast.NotEq,
        ):
            continue
        key = f"identity/{path}:{node.lineno}:{node.col_offset}"
        if key not in mapping["identity_only"]:
            return False
        node.ops[0] = counterpart.ops[0]
        changed = True
    return changed and ast.dump(old) == ast.dump(new)


def observed_on(mapping, keys):
    owners = mapping["owners"]
    return {
        owners[number]
        for key in keys
        for number in mapping["coverage"].get(key, ())
    }


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


def attribute_name(statement):
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign):
        target = statement.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) and statement.value is not None else None


@cache
def module_syntax(path):
    return ast.parse((BASE / path).read_bytes(), filename=path)


def closure_factory(name, tree):
    """Resolve an undecorated factory that only creates and returns a closure."""
    for definition in tree.body:
        if isinstance(definition, ast.ImportFrom) and definition.level == 0:
            imported = next(
                (alias.name for alias in definition.names if (alias.asname or alias.name) == name),
                None,
            )
            if imported and definition.module.startswith("sqlglot."):
                path = definition.module.replace(".", "/") + ".py"
                if (BASE / path).is_file():
                    return creates_closure(imported, module_syntax(path))
        elif isinstance(definition, ast.FunctionDef) and definition.name == name:
            return creates_closure(name, tree)
    return False


def creates_closure(name, tree):
    has_future = any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    if not has_future:
        return False
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name != name:
            continue
        if function.decorator_list or len(function.body) != 2:
            return False
        kept, result = function.body
        return (
            isinstance(kept, ast.FunctionDef) and not kept.decorator_list
            and not kept.args.defaults and not any(kept.args.kw_defaults)
            and isinstance(result, ast.Return) and isinstance(result.value, ast.Name)
            and result.value.id == kept.name
        )
    return False


def same_value_shape(old, new, tree):
    """Allow value-only changes while preserving keys, types, and call binding."""
    if ast.dump(old) == ast.dump(new):
        return True
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        return type(old.value) is type(new.value)
    if isinstance(old, ast.UnaryOp):
        return type(old.op) is type(new.op) and same_value_shape(old.operand, new.operand, tree)
    if isinstance(old, (ast.Tuple, ast.List)):
        return len(old.elts) == len(new.elts) and all(
            same_value_shape(left, right, tree) for left, right in zip(old.elts, new.elts)
        )
    if isinstance(old, ast.Dict):
        return (
            [ast.dump(key) if key else None for key in old.keys]
            == [ast.dump(key) if key else None for key in new.keys]
            and all(same_value_shape(left, right, tree) for left, right in zip(old.values, new.values))
        )
    if isinstance(old, ast.Call):
        return (
            isinstance(old.func, ast.Name) and ast.dump(old.func) == ast.dump(new.func)
            and closure_factory(old.func.id, tree)
            and len(old.args) == len(new.args)
            and [kw.arg for kw in old.keywords] == [kw.arg for kw in new.keywords]
            and all(same_value_shape(a, b, tree) for a, b in zip(old.args, new.args))
            and all(same_value_shape(a.value, b.value, tree)
                    for a, b in zip(old.keywords, new.keywords))
        )
    return False


def class_changes(path, source, updated):
    parts = path.split("/")
    if len(parts) != 3 or parts[1] not in {"dialects", "parsers", "generators", "expressions"}:
        return ()
    old = ast.parse(source)
    new = ast.parse(updated)
    changed = []

    def compare_classes(left, right, enclosing=""):
        for a, b in zip(left.body, right.body):
            if isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and a.name == b.name:
                compare_classes(a, b, f"{enclosing}.{a.name}" if enclosing else a.name)
            elif enclosing and (attribute := attribute_name(a)) and attribute == attribute_name(b):
                if ast.dump(a.value) != ast.dump(b.value) and same_value_shape(a.value, b.value, old):
                    a.value = b.value
                    changed.append((enclosing, attribute))

    compare_classes(old, new)
    if changed and ast.dump(old) == ast.dump(new):
        return changed
    return ()


def references_class(by_file, names):
    """Cover direct test references, including tests that only inspect a class."""
    results = set()
    for filename, ids in by_file.items():
        raw = (BASE / filename).read_bytes()
        if not any(name.encode() in raw for name in names):
            continue
        tree = module_syntax(filename)
        pending = [(tree, None)]
        while pending:
            node, owner = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                owner = node.name
            if (
                isinstance(node, ast.Name) and node.id in names
                or isinstance(node, ast.Attribute) and node.attr in names
                or isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names
            ):
                results.update(
                    nodeid for nodeid in ids
                    if owner is None or nodeid.rsplit("::", 1)[-1].split("[", 1)[0] == owner
                )
            pending.extend((child, owner) for child in ast.iter_child_nodes(node))
    return results


def class_candidates(path, source, updated, mapping, by_file):
    changes = class_changes(path, source, updated)
    if not changes:
        return None
    compile(updated, path, "exec")
    module = path.removesuffix(".py").replace("/", ".")
    candidates = set()
    for name, attribute in changes:
        outer = name.split(".")[0]
        roots = observed_on(mapping, {f"receiver/{module}:{name}", f"receiver/{module}:{outer}"})
        if not roots - {COLLECTION}:
            return None
        candidates.update(roots - {COLLECTION})
        candidates.update(references_class(by_file, {outer, attribute}))
    return candidates


def candidate_tests(edits, mapping, by_file, all_ids):
    selected = set(mapping["failed"])
    class_modules = []
    for path, edit in edits.items():
        if path.startswith("tests/fixtures/") and path in mapping["fixtures"]:
            for owner in mapping["fixtures"][path]:
                if owner == COLLECTION:
                    return all_ids, ()
                selected.update(by_file.get(owner, (owner,)))
        elif path.startswith("sqlglot/") and path.endswith(".py"):
            source, updated, removed, inserted = edit
            owners = set(mapping["source_reads"].get(path, ()))
            if ast.dump(ast.parse(source)) != ast.dump(ast.parse(updated)):
                keys = changed_functions(path, edit)
                if keys is None:
                    class_owners = class_candidates(path, source, updated, mapping, by_file)
                    if class_owners is None:
                        return all_ids, ()
                    owners.update(class_owners)
                    class_modules.append(path.removesuffix(".py").replace("/", "."))
                elif not safe_identity_edit(path, source, updated, mapping):
                    reached = observed_on(mapping, keys)
                    lines = statement_lines(path, source, updated, removed, inserted)
                    line_owners = observed_on(mapping, (f"{path}#{line}" for line in lines))
                    owners.update(line_owners or reached)
            else:
                compile(updated, path, "exec")
            if COLLECTION in owners:
                return all_ids, ()
            for owner in owners:
                selected.update(by_file.get(owner, (owner,)))
        else:
            return all_ids, ()
    return selected & all_ids, class_modules


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



def patched_tree(scratch, edits):
    tree = scratch / "source"
    shutil.copytree(BASE, tree, copy_function=os.symlink)
    for path, (_, content, _, _) in edits.items():
        target = tree / path
        target.unlink()
        target.write_bytes(content)
    return tree


def run_in_tree(command, tree, env, timeout):
    with subprocess.Popen(
        command, cwd=tree, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    ) as run:
        try:
            return run.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(run.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise


def imports_cleanly(edits, modules, timeout):
    """Detect import-time failures introduced by narrower class changes."""
    with tempfile.TemporaryDirectory(prefix="blast-") as scratch:
        tree = patched_tree(Path(scratch), edits)
        command = [
            sys.executable, "-c",
            "import importlib, sys; [importlib.import_module(name) for name in sys.argv[1:]]",
            *modules,
        ]
        return run_in_tree(command, tree, dict(os.environ, PYTHONPATH=str(tree)), timeout) == 0

def confirm(edits, candidates, all_ids, by_file, timeout):
    """Return observed failures only when pytest completed the requested run."""
    with tempfile.TemporaryDirectory(prefix="blast-") as scratch:
        scratch = Path(scratch)
        tree = patched_tree(scratch, edits)
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
        exit_code = run_in_tree(command, tree, env, timeout)
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
                candidates, class_modules = candidate_tests(edits, mapping, by_file, all_ids)
                if class_modules:
                    timeout = min(4, deadline - time.monotonic())
                    if timeout <= 0 or not imports_cleanly(edits, class_modules, timeout):
                        candidates = all_ids
                workers, cost = schedule(candidates, tests)
            except (OSError, ValueError, SyntaxError, RecursionError, subprocess.TimeoutExpired):
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
