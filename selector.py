"""Select regression tests using reachability from the pinned checkout."""

import ast
import inspect
import json
import math
import multiprocessing
import os
import re
import resource
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

from comparison_probe import identity_site, order_site
from coverage_index import load as load_coverage
from quick_unittest import main as quick_unittest
from global_probe import sites as global_sites, module_name as global_module

DATA = Path("/data")
APP = Path(__file__).resolve().parent
BASE = Path("/opt/sqlglot")
DIFF = re.compile(rb"diff --git a/([\w./-]+) b/\1\n")
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n")
COLLECTION = "@collection"


def require(condition):
    if not condition:
        raise ValueError("unsupported or inconsistent unified diff")


def same_ast(left, right):
    """Compare syntax, ignoring source locations and retaining literal types."""
    kind = type(left)
    if kind is not type(right):
        return False
    if isinstance(left, ast.AST):
        for field in left._fields:
            if not same_ast(getattr(left, field, None), getattr(right, field, None)):
                return False
        return True
    if kind is list:
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if not same_ast(a, b):
                return False
        return True
    return left == right


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
    if old_lines == new_lines and old_type == new_type and same_ast(old_tree, new_tree):
        return old_lines
    return ()


COMPARISON_TOKEN = re.compile(rb"\bis\b|[<>]")


def may_change_comparison(edit):
    source, updated, removed, inserted = edit
    return any(
        COMPARISON_TOKEN.search(line)
        for text, numbers in ((source, removed), (updated, inserted))
        for index, line in enumerate(text.splitlines(), 1)
        if index in numbers
    )


def safe_comparison_edit(path, source, updated, mapping):
    """Prove the changed comparisons agree on all observed built-in operands."""
    old = ast.parse(source)
    new = ast.parse(updated)
    new_comparisons = {
        (node.lineno, node.col_offset): node
        for node in ast.walk(new) if isinstance(node, ast.Compare)
    }
    opposite = {
        ast.Is: ast.Eq, ast.IsNot: ast.NotEq,
        ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    }
    changed = False
    for node in ast.walk(old):
        if not (identity_site(node) or order_site(node)):
            continue
        counterpart = new_comparisons.get((node.lineno, node.col_offset))
        if counterpart is None or not isinstance(counterpart.ops[0], opposite[type(node.ops[0])]):
            continue
        key = f"comparison/{path}:{node.lineno}:{node.col_offset}"
        if key not in mapping["proven_comparisons"]:
            return False
        node.ops[0] = counterpart.ops[0]
        changed = True
    return changed and same_ast(old, new)


def observed_on(mapping, keys):
    owners = mapping["owners"]
    return {
        owners[number]
        for key in keys
        for number in mapping["coverage"].get(key, ())
    }


@cache
def original_function_layout(path):
    source = (BASE / path).read_bytes()
    visitor = FunctionBodies(source)
    tree = visitor.visit(ast.parse(source, filename=path))
    return visitor.functions, tree, external_code(compile(source, path, "exec"))


def changed_functions(path, edit, updated_tree):
    _, modified, removed, inserted = edit
    old_functions, left, left_code = original_function_layout(path)
    after = FunctionBodies(modified)
    right = after.visit(updated_tree)
    if not same_ast(left, right):
        return None
    right_code = external_code(compile(modified, path, "exec"))

    affected = set()
    for (old_first, old_body, old_end), (new_first, new_body, new_end) in zip(
        old_functions, after.functions, strict=True
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


def resolves_enum(name, tree, depth=0):
    if depth >= 8:
        return False
    for node in reversed(tree.body):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return any(
                isinstance(base, ast.Name) and resolves_enum(base.id, tree, depth + 1)
                for base in node.bases
            )
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                if node.module == "enum":
                    return alias.name in {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
                if node.module.startswith("sqlglot."):
                    path = node.module.replace(".", "/") + ".py"
                    if (BASE / path).is_file():
                        return resolves_enum(alias.name, module_syntax(path), depth + 1)
                return False
    return False


def same_value_shape(old, new, tree):
    """Allow value-only changes while preserving keys, types, and call binding."""
    if same_ast(old, new):
        return True
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        return type(old.value) is type(new.value)
    if isinstance(old, ast.Attribute):
        return (
            isinstance(old.value, ast.Name) and isinstance(new.value, ast.Name)
            and old.value.id == new.value.id and resolves_enum(old.value.id, tree)
        )
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
            isinstance(old.func, ast.Name) and same_ast(old.func, new.func)
            and closure_factory(old.func.id, tree)
            and len(old.args) == len(new.args)
            and [kw.arg for kw in old.keywords] == [kw.arg for kw in new.keywords]
            and all(same_value_shape(a, b, tree) for a, b in zip(old.args, new.args))
            and all(same_value_shape(a.value, b.value, tree)
                    for a, b in zip(old.keywords, new.keywords))
        )
    return False


def passive_registry_value(value, tree):
    if literal_value(value):
        return True
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        return resolves_enum(value.value.id, tree)
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return (
            closure_factory(value.func.id, tree)
            and all(literal_value(arg) for arg in value.args)
            and all(keyword.arg and literal_value(keyword.value) for keyword in value.keywords)
        )
    return False


def named_entries(dictionary):
    entries = {}
    for key, value in zip(dictionary.keys, dictionary.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if key.value in entries:
                return None
            entries[key.value] = value
    return entries


def pure_registry_delta(path, attribute, old, new, tree):
    """Allow key edits to baseline per-dialect registries with passive values."""
    dialect_table = path.startswith("sqlglot/dialects/") and attribute in {"KEYWORDS", "NUMERIC_ESCAPES"}
    parser_table = path.startswith("sqlglot/parsers/") and attribute == "SHOW_PARSERS"
    if not (dialect_table or parser_table) or not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return False
    left, right = named_entries(old), named_entries(new)
    if left is None or right is None or left.keys() == right.keys():
        return False

    changed_keys = left.keys() ^ right.keys()
    if not all(passive_registry_value(left[key] if key in left else right[key], tree) for key in changed_keys):
        return False

    def retained(dictionary):
        pairs = [
            (key, value) for key, value in zip(dictionary.keys, dictionary.values)
            if not isinstance(key, ast.Constant) or key.value not in changed_keys
        ]
        return ast.Dict(keys=[key for key, _ in pairs], values=[value for _, value in pairs])

    # Require all other entries and dictionary expansions to be unchanged.
    return same_ast(retained(old), retained(new))


def class_changes(path, source, updated):
    parts = path.split("/")
    if len(parts) not in {2, 3}:
        return ()
    if len(parts) == 3 and parts[1] not in {"dialects", "parsers", "generators", "expressions"}:
        return ()
    old = ast.parse(source)
    new = ast.parse(updated)
    changed = []

    def compare_classes(left, right, enclosing=""):
        for a, b in zip(left.body, right.body):
            if isinstance(a, ast.ClassDef) and isinstance(b, ast.ClassDef) and a.name == b.name:
                compare_classes(a, b, f"{enclosing}.{a.name}" if enclosing else a.name)
            elif enclosing and (attribute := attribute_name(a)) and attribute == attribute_name(b):
                if not same_ast(a.value, b.value) and (
                    same_value_shape(a.value, b.value, old)
                    or (literal_value(a.value) and literal_value(b.value)
                        and type(ast.literal_eval(a.value)) is type(ast.literal_eval(b.value)))
                    or pure_registry_delta(path, attribute, a.value, b.value, old)
                ):
                    changed.append((enclosing, attribute, a.value, b.value))
                    a.value = b.value

    compare_classes(old, new)
    if changed and same_ast(old, new):
        return changed
    return ()


def references_class(mapping, names):
    """Cover direct test references, including tests that only inspect a class."""
    index = mapping["test_references"]
    return {nodeid for name in names for nodeid in index.get(name, ())}


@cache
def project_source_files():
    return tuple(
        (path.relative_to(BASE).as_posix(), path.read_bytes())
        for path in sorted((BASE / "sqlglot").rglob("*.py"))
    )



@cache
def default_call_sites(name, parameter, position):
    """Follow direct uses of a default; unknown function escapes retain entry coverage."""
    keys = set()
    encoded = name.encode()
    for path, source in project_source_files():
        if encoded not in source:
            continue
        tree = module_syntax(path)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in parents:
            if (
                isinstance(node, ast.alias) and node.name == name and node.asname not in (None, name)
                or isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == name
            ):
                return None
            if not (
                isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
                or isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, ast.Load)
            ):
                continue
            call = parents[node]
            if not isinstance(call, ast.Call) or call.func is not node:
                return None
            if any(keyword.arg == parameter for keyword in call.keywords):
                continue
            has_star = any(isinstance(arg, ast.Starred) for arg in call.args)
            if position is not None and not has_star and len(call.args) > position:
                continue
            keys.update(f"{path}#{line}" for line in range(call.lineno, call.end_lineno + 1))
    return tuple(sorted(keys))


@cache
def binding_consumers(attribute, bare_names=False):
    """Map configuration reads to lines, or calls for captured defaults."""
    symbols = {attribute}
    files = project_source_files()
    if bare_names:
        while True:
            renamed = set()
            encoded = tuple(name.encode() for name in symbols)
            for path, source in files:
                if not any(name in source for name in encoded):
                    continue
                for node in ast.walk(module_syntax(path)):
                    if isinstance(node, ast.ImportFrom) and all(alias.name != "*" for alias in node.names):
                        renamed.update(
                            alias.asname for alias in node.names
                            if alias.name in symbols and alias.asname
                        )
            if renamed <= symbols:
                break
            symbols.update(renamed)
            if len(symbols) > 12:
                return (), ()

    encoded = tuple(name.encode() for name in symbols)
    keys = set()
    names = set(symbols)
    for path, source in files:
        if not any(name in source for name in encoded):
            continue
        tree = module_syntax(path)
        default_owner = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = [*node.args.posonlyargs, *node.args.args]
                offset = len(positional) - len(node.args.defaults)
                defaults = [
                    (expression, positional[index].arg, index)
                    for index, expression in enumerate(node.args.defaults, offset)
                    if index >= len(node.args.posonlyargs)
                ]
                defaults.extend(
                    (expression, None, None)
                    for index, expression in enumerate(node.args.defaults, offset)
                    if index < len(node.args.posonlyargs)
                )
                defaults.extend(
                    (expression, argument.arg, None)
                    for argument, expression in zip(node.args.kwonlyargs, node.args.kw_defaults)
                    if expression is not None
                )
                for expression, parameter, position in defaults:
                    default_owner.update(
                        (child, (node, expression, parameter, position))
                        for child in ast.walk(expression)
                    )

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in symbols:
                return (), ()
            if isinstance(node, ast.Name) and node.id in symbols and isinstance(node.ctx, ast.Load):
                if not bare_names:
                    return (), ()
            elif not (
                isinstance(node, ast.Attribute) and node.attr in symbols and isinstance(node.ctx, ast.Load)
            ):
                continue
            default = default_owner.get(node)
            if default is None:
                keys.update(f"{path}#{line}" for line in range(node.lineno, node.end_lineno + 1))
                continue
            owner, expression, parameter, position = default
            if owner.decorator_list:
                # Decorators can inspect or execute the function at definition
                # time. Retain the broader fallback for these bindings.
                return (), ()
            call_sites = (
                default_call_sites(owner.name, parameter, position)
                if parameter and node is expression and owner in tree.body else None
            )
            if call_sites is None:
                keys.add(f"{path}:{owner.lineno}")
            else:
                keys.update(call_sites)
            names.add(owner.name)
    return tuple(sorted(keys)), tuple(sorted(names))


def scalar_literal(node):
    return (
        isinstance(node, ast.Constant)
        or isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant)
    )


@cache
def ordinary_keyword_characters():
    """Conservatively collect punctuation that feeds the pinned keyword tries."""
    punctuation = set()
    for path, source in project_source_files():
        if b"SINGLE_TOKENS" not in source:
            continue
        for node in ast.walk(module_syntax(path)):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and attribute_name(node) == "SINGLE_TOKENS":
                if isinstance(node.value, ast.Dict):
                    for key, value in zip(node.value.keys, node.value.values):
                        if key is None:
                            if not isinstance(value, ast.Attribute) or value.attr != "SINGLE_TOKENS":
                                return None
                        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                            punctuation.update(key.value)
                        else:
                            return None
                elif isinstance(node.value, ast.Call):
                    call = node.value
                    if (call.args or call.keywords or not isinstance(call.func, ast.Attribute)
                        or call.func.attr != "copy" or not isinstance(call.func.value, ast.Attribute)
                        or call.func.value.attr != "SINGLE_TOKENS"):
                        return None
                elif not isinstance(node.value, ast.Attribute) or node.value.attr != "SINGLE_TOKENS":
                    return None
    return frozenset(punctuation)


def value_consumers(path, attribute, old, new):
    if scalar_literal(old) and scalar_literal(new):
        return binding_consumers(attribute)[0]
    kind = None
    if path.startswith("sqlglot/dialects/"):
        kind = {"NUMERIC_ESCAPES": "numeric-escape", "KEYWORDS": "keyword"}.get(attribute)
    if path.startswith("sqlglot/parsers/") and attribute == "SHOW_PARSERS":
        kind = "parser-rule"
    if kind == "keyword" and isinstance(old, ast.Dict) and isinstance(new, ast.Dict):
        left, right = named_entries(old), named_entries(new)
        if left is not None and right is not None and left.keys() != right.keys():
            removed = left.keys() ^ right.keys()
            removed.update(key for key in left.keys() & right.keys() if not same_ast(left[key], right[key]))
            # In the pinned tokenizer, ordinary words do not change the
            # punctuation/multiword trie or the special hint and join keys.
            # Added keys also require negative membership observations.
            punctuation = ordinary_keyword_characters()
            if punctuation is not None and all(
                re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and not (set(key) & punctuation)
                for key in removed
            ):
                return tuple(f"keyword/{key}" for key in sorted(removed))
    if (
        kind and isinstance(old, ast.Dict) and isinstance(new, ast.Dict)
        and same_ast(old.keys, new.keys)
    ):
        # Equal key sets preserve truth, membership, and generated tries.
        # Other reads in the pinned implementation go through the instrumented
        # tokenizer or parser. Unchanged dictionary expansions may be ignored.
        changed = [
            key for key, before, after in zip(old.keys, old.values, new.values)
            if not same_ast(before, after)
        ]
        if all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in changed):
            return tuple(f"{kind}/{key.value}" for key in changed)

    return ()


@cache
def registry_dependents(path, outer, attribute):
    """Find downstream copies even across distinct receiver hierarchies."""
    qualified = path.removesuffix(".py").replace("/", ".")
    pending = {(qualified, outer)}
    seen = set()
    while pending:
        origin, exported = pending.pop()
        seen.add((origin, exported))
        for candidate, contents in project_source_files():
            module = candidate.removesuffix(".py").replace("/", ".")
            if module == origin or attribute.encode() not in contents:
                continue
            tree = module_syntax(candidate)
            aliases = set()
            for statement in ast.walk(tree):
                if isinstance(statement, ast.Import):
                    for name in statement.names:
                        if name.name == origin:
                            aliases.add(name.asname or name.name.split(".")[0])
                elif isinstance(statement, ast.ImportFrom):
                    imported = statement.module or ""
                    if statement.level:
                        package = module.split(".")[:-1]
                        if statement.level > len(package):
                            return None
                        imported = ".".join(package[:len(package) - statement.level + 1] + ([imported] if imported else []))
                    for name in statement.names:
                        if name.name == "*" and imported in {origin, origin.rsplit(".", 1)[0]}:
                            return None
                        if (
                            imported in {origin, origin.rsplit(".", 1)[0]} and name.name == exported
                            or imported + "." + name.name == origin
                        ):
                            aliases.add(name.asname or name.name)
            # Local rebinding can carry the imported registry into another
            # class. Follow simple aliases; ambiguous assignments stay broad.
            progress = True
            while progress:
                progress = False
                for statement in ast.walk(tree):
                    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                        continue
                    value = statement.value
                    while isinstance(value, ast.Attribute):
                        value = value.value
                    if isinstance(value, ast.Name) and value.id in aliases:
                        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                        for target in targets:
                            if isinstance(target, ast.Name) and target.id not in aliases:
                                aliases.add(target.id)
                                progress = True
            if not aliases:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr != attribute:
                    continue
                base = node.value
                while isinstance(base, ast.Attribute):
                    base = base.value
                if isinstance(base, ast.Name) and base.id in aliases:
                    classes = {(module, child.name) for child in tree.body if isinstance(child, ast.ClassDef)}
                    if not classes:
                        return None
                    pending.update(classes - seen)
                    break
    return tuple(sorted(seen - {(qualified, outer)}))


def class_candidates(path, source, updated, mapping, by_file):
    changes = class_changes(path, source, updated)
    if not changes:
        return None
    compile(updated, path, "exec")
    module = path.removesuffix(".py").replace("/", ".")
    candidates = set()
    for name, attribute, old, new in changes:
        outer = name.split(".")[0]
        roots = observed_on(mapping, {f"receiver/{module}:{name}", f"receiver/{module}:{outer}"})
        if not roots - {COLLECTION}:
            return None
        dependents = ()
        if attribute:
            dependents = registry_dependents(path, outer, attribute)
            if dependents is None:
                return None
            for dependent, cls in dependents:
                propagated = observed_on(mapping, (f"receiver/{dependent}:{cls}",))
                if not propagated - {COLLECTION}:
                    return None
                roots.update(propagated)
                candidates.update(references_class(mapping, {cls, attribute}))
        keys = value_consumers(path, attribute, old, new)
        if (attribute == "KEYWORDS" and isinstance(old, ast.Dict) and isinstance(new, ast.Dict)
            and "keyword-membership" not in mapping["dictionary_lookups"]):
            left, right = named_entries(old), named_entries(new)
            if left is not None and right is not None and right.keys() - left.keys():
                keys = ()
        if not dependents and keys and keys[0].startswith("numeric-escape/") and "numeric-origin" in mapping["dictionary_lookups"]:
            escapes = [key.split("/", 1)[1] for key in keys]
            keys = tuple(
                f"numeric-origin/{origin}/{escape}"
                for origin in (f"{path}:{name}", "unknown")
                for escape in escapes
            )
        consumers = observed_on(mapping, keys)
        watched_dictionary = keys and keys[0].split("/", 1)[0] in mapping["dictionary_lookups"]
        direct_reads = (bool(consumers) or watched_dictionary) and COLLECTION not in consumers
        if path.count("/") == 1 and not direct_reads:
            return None
        if direct_reads:
            # Expand shared setup observations before intersecting test IDs.
            readers = set().union(*(set(by_file.get(owner, (owner,))) for owner in consumers))
            roots = {
                test for owner in roots - {COLLECTION}
                for test in by_file.get(owner, (owner,))
                if test in readers
            }
        candidates.update(roots - {COLLECTION})
        candidates.update(references_class(mapping, {outer, attribute}))
    return candidates


def literal_value(node):
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return False
    return True


def global_binding_candidates(path, source, updated, mapping):
    if not mapping.get("scalar_globals"):
        return None
    before, after = ast.parse(source), ast.parse(updated)
    changed = []
    for old, new in zip(before.body, after.body):
        name = attribute_name(old)
        if (name and name.isupper() and name == attribute_name(new)
            and literal_value(old.value) and literal_value(new.value)
            and not same_ast(old.value, new.value) and same_value_shape(old.value, new.value, before)):
            old.value = new.value
            changed.append(name)
    if not changed or not same_ast(before, after):
        return None
    compile(updated, path, "exec")
    keys = set()
    for name in changed:
        sites = global_sites(APP / "global_sites.json", global_module(path), name)
        if sites is None:
            return None
        keys.update(sites)
    owners = observed_on(mapping, keys)
    if COLLECTION in owners:
        return None
    owners.update(references_class(mapping, set(changed) | {
        "getmembers", "getmembers_static", "signature", "getattr", "vars", "dir",
        "__dict__", "__globals__", "eval", "exec", "globals", "locals",
    }))
    return owners


def module_constant_candidates(path, source, updated, mapping, by_file):
    """Trace literal globals through reads, import aliases, and captured defaults."""
    precise = global_binding_candidates(path, source, updated, mapping)
    if precise is not None:
        return precise
    before = ast.parse(source)
    after = ast.parse(updated)
    changed = []
    for old, new in zip(before.body, after.body):
        name = attribute_name(old)
        if (
            name and name.isupper() and name == attribute_name(new)
            and literal_value(old.value) and literal_value(new.value)
            and ast.dump(old.value) != ast.dump(new.value)
            and same_value_shape(old.value, new.value, before)
        ):
            old.value = new.value
            changed.append(name)
    if not changed or not same_ast(before, after):
        return None
    compile(updated, path, "exec")
    candidates = set()
    for name in changed:
        keys, names = binding_consumers(name, bare_names=True)
        if any(
            any(symbol.encode() in candidate.read_bytes() for symbol in names)
            for candidate in (BASE / "tests").rglob("*.py")
            if candidate.relative_to(BASE).as_posix() not in by_file
        ):
            return None
        consumers = observed_on(mapping, keys)
        if not consumers or COLLECTION in consumers:
            return None
        candidates.update(consumers)
        candidates.update(references_class(mapping, names))
    return candidates


def default_argument_candidates(path, source, updated, mapping):
    """Use actual entry values; explicit non-default arguments stay independent."""
    original, patched = ast.parse(source), ast.parse(updated)
    before = [n for n in ast.walk(original) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    after = [n for n in ast.walk(patched) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(before) != len(after):
        return None
    parents = {child: parent for parent in ast.walk(original) for child in ast.iter_child_nodes(parent)}
    keys = set()
    names = set()
    for old, new in zip(before, after):
        if (old.name != new.name or len(old.args.defaults) != len(new.args.defaults)
            or len(old.args.kw_defaults) != len(new.args.kw_defaults)):
            return None
        parameters = [*old.args.posonlyargs, *old.args.args]
        pairs = [(old.args.defaults, new.args.defaults, parameters[len(parameters)-len(old.args.defaults):]),
                 (old.args.kw_defaults, new.args.kw_defaults, old.args.kwonlyargs)]
        for old_defaults, new_defaults, params in pairs:
            for index, (left, right, parameter) in enumerate(zip(old_defaults, new_defaults, params)):
                if same_ast(left, right):
                    continue
                if left is None or right is None:
                    return None
                try:
                    old_value, new_value = ast.literal_eval(left), ast.literal_eval(right)
                except (ValueError, TypeError, SyntaxError):
                    return None
                scalar = (type(None), bool, int, float, str)
                if type(old_value) not in scalar or type(new_value) not in scalar:
                    return None
                if isinstance(old_value, float) and not math.isfinite(old_value):
                    return None
                if any(not isinstance(d, ast.Name) or d.id not in {"classmethod", "staticmethod"} for d in old.decorator_list):
                    return None
                ancestor = parents.get(old)
                while ancestor is not None:
                    if isinstance(ancestor, ast.ClassDef) and (ancestor.decorator_list or ancestor.keywords):
                        return None
                    ancestor = parents.get(ancestor)
                first = min([old.lineno, *[d.lineno for d in old.decorator_list]])
                keys.add(f"default/{path}:{first}/{parameter.arg}")
                names.add(old.name)
                old_defaults[index] = right
    if not keys or not same_ast(original, patched):
        return None
    compile(updated, path, "exec")
    owners = observed_on(mapping, keys)
    if COLLECTION in owners:
        return None
    # The pinned source uses inspect.signature only to read parameter names;
    # keep test-side metadata and reflection checks irrespective of calls.
    names.update({"signature", "__defaults__", "__kwdefaults__", "getattr", "vars", "__dict__", "dir"})
    owners.update(references_class(mapping, names))
    return owners


def signature_candidates(path, source, updated, mapping):
    """Scope literal defaults changed in otherwise identical module functions."""
    if mapping.get("argument_defaults"):
        precise = default_argument_candidates(path, source, updated, mapping)
        if precise is not None:
            return precise
    original, patched = ast.parse(source), ast.parse(updated)
    affected = []
    for old, new in zip(original.body, patched.body):
        if not (
            isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)) and type(old) is type(new)
            and old.name == new.name and not old.decorator_list
            and len(old.args.defaults) == len(new.args.defaults)
            and len(old.args.kw_defaults) == len(new.args.kw_defaults)
        ):
            continue
        for old_defaults, new_defaults in (
            (old.args.defaults, new.args.defaults), (old.args.kw_defaults, new.args.kw_defaults)
        ):
            for index, (before, after) in enumerate(zip(old_defaults, new_defaults)):
                if (
                    before is not None and after is not None
                    and not same_ast(before, after)
                    and literal_value(before) and literal_value(after)
                    and same_value_shape(before, after, original)
                ):
                    old_defaults[index] = after
                    affected.append((old.name, old.lineno))
    if not affected or not same_ast(original, patched):
        return None
    compile(updated, path, "exec")
    owners = observed_on(mapping, (f"{path}:{line}" for _, line in affected))
    if not owners or COLLECTION in owners:
        return None
    owners.update(references_class(mapping, {name for name, _ in affected}))
    return owners


@cache
def isolated_test_module(path):
    """Require a declarative test module with no pinned inbound references."""
    target = BASE / path
    if not target.name.startswith("test_") or target.name == "conftest.py":
        return False
    stem = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(target.stem.encode()) + rb"(?![A-Za-z0-9_])")
    for folder in (BASE / "tests", BASE / "sqlglot"):
        for other in folder.rglob("*.py"):
            if other != target and stem.search(other.read_bytes()):
                return False
    tree = module_syntax(path)
    os_imported = any(isinstance(n, ast.Import) and any(a.name in {"os", "os.path"} and (a.asname or a.name.split(".")[0]) == "os" for a in n.names) for n in tree.body)

    unittest_imported = any(isinstance(n, ast.Import) and any(a.name == "unittest" and (a.asname or a.name) == "unittest" for a in n.names) for n in tree.body)
    mock_imported = any(isinstance(n, ast.ImportFrom) and n.module == "unittest" and any(a.name == "mock" and (a.asname or a.name) == "mock" for a in n.names) for n in tree.body)

    def passive_function(node):
        for decorator in node.decorator_list:
            name = decorator.func if isinstance(decorator, ast.Call) else decorator
            qualified = ast.unparse(name)
            if qualified.startswith("unittest.") and not unittest_imported:
                return False
            if qualified.startswith("mock.") and not mock_imported:
                return False
            if isinstance(decorator, ast.Call) and any(isinstance(child, ast.Call) for expression in [*decorator.args, *[k.value for k in decorator.keywords]] for child in ast.walk(expression)):
                return False
            if qualified not in {
                "classmethod", "staticmethod", "unittest.skip", "unittest.skipIf", "unittest.skipUnless",
                "mock.patch", "mock.patch.object", "mock.patch.dict",
                "unittest.mock.patch", "unittest.mock.patch.object", "unittest.mock.patch.dict",
            }:
                return False
        annotations = [a.annotation for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs) if a.annotation is not None]
        if node.returns is not None:
            annotations.append(node.returns)
        if any(isinstance(child, ast.Call) for annotation in annotations for child in ast.walk(annotation)):
            return False
        return all(literal_value(x) for x in (*node.args.defaults, *filter(None, node.args.kw_defaults)))

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.FunctionDef):
            if not passive_function(node):
                return False
            continue
        if not isinstance(node, ast.ClassDef) or node.decorator_list or node.keywords:
            return False
        if len(node.bases) != 1 or ast.unparse(node.bases[0]) != "unittest.TestCase":
            return False
        paths = {"__file__"}
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and passive_function(child):
                continue
            if isinstance(child, ast.Assign) and all(isinstance(t, ast.Name) for t in child.targets):
                if literal_value(child.value):
                    paths.update(t.id for t in child.targets if isinstance(ast.literal_eval(child.value), str))
                    continue
                value = child.value
                if (os_imported and isinstance(value, ast.Call) and not value.keywords
                    and ast.unparse(value.func) in {"os.path.dirname", "os.path.join"}
                    and all(isinstance(a, ast.Constant) and isinstance(a.value, str) or isinstance(a, ast.Name) and a.id in paths for a in value.args)):
                    paths.update(t.id for t in child.targets)
                    continue
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                continue
            return False
    return True


def test_module_candidates(path, edit, by_file):
    if path not in by_file or not isolated_test_module(path):
        return None
    source, updated, _, _ = edit
    old, new = ast.parse(source), ast.parse(updated)
    # Existing imports and declarations must remain in order. Added imports can
    # fail collection of this isolated module; they cannot modify package globals
    # when they name modules outside the pinned source. Unknown imports stay broad.
    extras = [node for node in new.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    old_imports = [node for node in old.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    if len(extras) > len(old_imports):
        pending = list(old_imports)
        for node in extras:
            if pending and same_ast(node, pending[0]):
                pending.pop(0)
                continue
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            # A failed, standard import cannot execute the rest of the module.
            # Prove absence without importing candidate-controlled code.
            import importlib.util
            if isinstance(node, ast.ImportFrom) and node.level:
                return None
            if len(names) != 1 or "." in names[0] or not names[0].isidentifier():
                return None
            if importlib.util.find_spec(names[0]) is not None:
                return None
        if pending:
            return None
        new.body = [n for n in new.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        old.body = [n for n in old.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        return set(by_file[path]) if same_ast(old, new) else None

    class Assertions(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            self.generic_visit(node)
            # An unconditional pure assertion adds no behavior outside the test.
            node.body = [stmt for stmt in node.body if not (
                isinstance(stmt, ast.Assert) and literal_value(stmt.test)
                and (stmt.msg is None or literal_value(stmt.msg))
            )]
            return node
        def visit_Call(self, node):
            if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self" and node.func.attr.startswith("assert")):
                node.args = [ast.Constant(None) if literal_value(arg) else arg for arg in node.args]
                for keyword in node.keywords:
                    if literal_value(keyword.value):
                        keyword.value = ast.Constant(None)
            return self.generic_visit(node)
    return set(by_file[path]) if same_ast(Assertions().visit(old), Assertions().visit(new)) else None


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
            updated_tree = ast.parse(updated, filename=path)
            if not same_ast(module_syntax(path), updated_tree):
                keys = changed_functions(path, edit, updated_tree)
                if keys is None:
                    class_owners = class_candidates(path, source, updated, mapping, by_file)
                    if class_owners is None:
                        class_owners = module_constant_candidates(path, source, updated, mapping, by_file)
                    if class_owners is None:
                        class_owners = signature_candidates(path, source, updated, mapping)
                    if class_owners is None:
                        return all_ids, ()
                    owners.update(class_owners)
                    class_modules.append(path.removesuffix(".py").replace("/", "."))
                elif not (may_change_comparison(edit) and safe_comparison_edit(path, source, updated, mapping)):
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
        elif path.startswith("tests/") and path.endswith(".py"):
            owners = test_module_candidates(path, edit, by_file)
            if owners is None:
                return all_ids, ()
            selected.update(owners)
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
    fixtures = BASE / "tests" / "fixtures"
    unchanged_fixtures = fixtures.is_dir() and not any(
        path.startswith("tests/fixtures/") for path in edits
    )

    def ignore(directory, names):
        return {"fixtures"} if unchanged_fixtures and Path(directory) == fixtures.parent else set()

    shutil.copytree(BASE, tree, copy_function=os.symlink, ignore=ignore)
    if unchanged_fixtures:
        (tree / "tests" / "fixtures").symlink_to(fixtures, target_is_directory=True)
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
            "-p", "test_observer", "--continue-on-collection-errors",
            *sorted({test.split("::", 1)[0] for test in candidates}),
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


@cache
def class_fixture_cost(path, class_name):
    """Identify test groups with class-level fixture setup."""
    return any(
        isinstance(node, ast.ClassDef) and node.name == class_name
        and any(
            isinstance(member, ast.FunctionDef)
            and member.name in {"setUpClass", "tearDownClass"}
            for member in node.body
        )
        for node in ast.walk(module_syntax(path))
    )


def quick_child(tree, request, response):
    os.setsid()
    os.chdir(tree)
    os.environ["PYTHONPATH"] = str(tree)
    sys.path.insert(0, str(tree))
    with open(os.devnull, "r+b", buffering=0) as sink:
        for fd in (0, 1, 2):
            os.dup2(sink.fileno(), fd)
        sys.exit(quick_unittest(request, response))


def run_quick(tree, request, response, timeout):
    # Share the already-loaded standard library. No SQLGlot modules are
    # imported before the child switches to the patched checkout.
    process = multiprocessing.get_context("fork").Process(
        target=quick_child, args=(tree, request, response)
    )
    process.start()
    try:
        process.join(timeout)
        if process.exitcode is None:
            try:
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.join()
        return process.exitcode
    finally:
        process.close()


def confirm_unittest(edits, candidates, timeout):
    """Keep the static set unless every approved case ran to completion."""
    with tempfile.TemporaryDirectory(prefix="blast-") as scratch:
        scratch = Path(scratch)
        tree = patched_tree(scratch, edits)
        requested = scratch / "selected.json"
        response = scratch / "failed.json"
        requested.write_text(json.dumps(sorted(candidates)))
        if run_quick(tree, requested, response, timeout) != 0 or not response.is_file():
            return candidates
        failed = set(json.loads(response.read_text()))
        return failed if failed <= candidates else candidates


def cpu_seconds():
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


def main():
    tests = {}
    by_file = defaultdict(set)
    for test in json.loads((DATA / "tests.json").read_text())["tests"]:
        tests[test["id"]] = (test["number"], round(test["seconds"] * 1000))
        by_file[test["file"]].add(test["id"])
    all_ids = set(tests)
    all_workers, all_cost = schedule(all_ids, tests)
    mapping = load_coverage(APP / "reachability.json")
    healthy = not mapping["collection_errors"] and all_ids <= set(mapping["finished"])
    confirm_candidates = os.environ.get("BLAST_CONFIRM") == "1"
    quick_check = os.environ.get("BLAST_QUICK", "1") == "1"
    plain_unittest = set(mapping.get("plain_unittest", ()))
    deadline = time.monotonic() + 145
    confirmation_credit = 3.0
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
            if (
                quick_check and confirmation_credit > 0.5 and edits
                and 0 < len(candidates) <= 16 and candidates <= plain_unittest
                and all(path.startswith("sqlglot/") and path.endswith(".py") for path in edits)
            ):
                total = sum(tests[nodeid][1] for nodeid in candidates) / 1000
                setup = any(
                    class_fixture_cost(path, cls)
                    for path, cls, _ in (nodeid.split("::") for nodeid in candidates)
                )
                budget = min(3.0 if setup else 1.4, 0.73 * cost / 1000)
                timeout = min(budget, deadline - time.monotonic())
                if total <= 2.3 and timeout > 0.4 and (not setup or budget >= 2.6):
                    started = cpu_seconds()
                    previous_cost = cost
                    try:
                        verified = confirm_unittest(edits, candidates, timeout)
                        workers, cost = schedule(verified, tests)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                    confirmation_credit += (previous_cost - cost) / 1000 - (cpu_seconds() - started)
            if (
                confirm_candidates and confirmation_credit > 0.5
                and edits and 0 < len(candidates) < len(all_ids)
                and all(path.startswith("sqlglot/") and path.endswith(".py") for path in edits)
            ):
                total = sum(tests[nodeid][1] for nodeid in candidates) / 1000
                files = {nodeid.split("::", 1)[0] for nodeid in candidates}
                timeout = min(2.5, deadline - time.monotonic())
                if len(files) <= 3 and total <= 0.8 and timeout > 0.5:
                    started = cpu_seconds()
                    previous_cost = cost
                    try:
                        verified = confirm(edits, candidates, all_ids, by_file, timeout)
                        workers, cost = schedule(verified, tests)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                    confirmation_credit += (previous_cost - cost) / 1000 - (cpu_seconds() - started)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
