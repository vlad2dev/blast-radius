"""Observe comparisons and tokenizer dictionary reads in the pinned source."""

import ast
import builtins
import importlib.machinery
import operator

PROBE = "_blast_observe_identity"


def identity_site(node):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    right = node.comparators[0]
    return (
        isinstance(node.ops[0], (ast.Is, ast.IsNot))
        and isinstance(node.left, ast.Attribute)
        and (isinstance(right, ast.Attribute) or (isinstance(right, ast.Constant) and right.value is None))
    )


ORDER_OPERATORS = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}


def order_site(node):
    return (
        isinstance(node, ast.Compare) and len(node.ops) == 1
        and type(node.ops[0]) in ORDER_OPERATORS
        and isinstance(node.left, (ast.Attribute, ast.Name))
        and isinstance(node.comparators[0], (ast.Constant, ast.Tuple))
    )


def proven_order(left, right):
    if type(left) is not type(right):
        return False
    if type(left) in (int, float, str):
        return left != right
    if type(left) is tuple:
        return (
            all(type(item) is int for item in left)
            and all(type(item) is int for item in right)
            and left != right
        )
    return False


def default_comparison(value):
    kind = type(value)
    return kind is type(None) or (
        kind.__eq__ is object.__eq__ and kind.__ne__ is object.__ne__
    )


def comparison_agrees_with_identity(left, right):
    if default_comparison(left) and default_comparison(right):
        return True
    left_type, right_type = type(left), type(right)
    # Use the pure built-in comparison of the actual string payload. This
    # also covers string enums without invoking arbitrary equality methods.
    if (
        issubclass(left_type, str) and issubclass(right_type, str)
        and left_type.__eq__ is str.__eq__ and right_type.__eq__ is str.__eq__
        and left_type.__ne__ is str.__ne__ and right_type.__ne__ is str.__ne__
    ):
        return str.__eq__(left, right) is (left is right)
    return False


def install(root, record, record_lookup):
    source_root = str(root) + "/sqlglot/"
    original_loader = importlib.machinery.SourceFileLoader.get_code

    order = {kind.__name__: compare for kind, compare in ORDER_OPERATORS.items()}

    def observe(key, left, right, operation):
        record(key)
        if operation in order:
            if not proven_order(left, right):
                record(key + "/unproven")
            return order[operation](left, right)
        if not comparison_agrees_with_identity(left, right):
            record(key + "/unproven")
        return (left is not right) if operation == "IsNot" else (left is right)

    numeric_registries = {}

    def register_numeric(origin, table):
        if type(table) is dict:
            numeric_registries[id(table)] = (table, origin)
        return table

    def record_key(kind, table, key):
        if not isinstance(key, str):
            return
        if kind == "numeric-escape":
            registry = numeric_registries.get(id(table))
            origin = registry[1] if registry and registry[0] is table else "unknown"
            record_lookup(f"numeric-origin/{origin}/{key}")
        record_lookup(f"{kind}/{key}")

    def lookup(kind, table, key, *default):
        record_key(kind, table, key)
        return table.get(key, *default)

    def lookup_item(kind, table, key):
        record_key(kind, table, key)
        return table[key]

    setattr(builtins, PROBE, observe)
    builtins._blast_observe_lookup = lookup
    builtins._blast_observe_item = lookup_item
    builtins._blast_register_numeric = register_numeric

    class Observe(ast.NodeTransformer):
        def __init__(self, path):
            self.path = path
            self.classes = []
            self.changed = False

        def visit_ClassDef(self, node):
            self.classes.append(node.name)
            try:
                return self.generic_visit(node)
            finally:
                self.classes.pop()

        def tag_numeric(self, target, value):
            if (
                self.classes and isinstance(target, ast.Name) and target.id == "NUMERIC_ESCAPES"
                and isinstance(value, ast.Dict)
            ):
                origin = self.path + ":" + ".".join(self.classes)
                self.changed = True
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="_blast_register_numeric", ctx=ast.Load()),
                        args=[ast.Constant(origin), value],
                        keywords=[],
                    ),
                    value,
                )
            return value

        def visit_Assign(self, node):
            node = self.generic_visit(node)
            if len(node.targets) == 1:
                node.value = self.tag_numeric(node.targets[0], node.value)
            return node

        def visit_AnnAssign(self, node):
            node = self.generic_visit(node)
            node.value = self.tag_numeric(node.target, node.value)
            return node

        def dictionary_kind(self, node):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if self.path == "sqlglot/tokenizer_core.py":
                return {"numeric_escapes": "numeric-escape", "keywords": "keyword"}.get(name)
            if self.path == "sqlglot/parser.py" and isinstance(node, ast.Name) and name == "parsers":
                return "parser-rule"
            return None

        def visit_Call(self, node):
            node = self.generic_visit(node)
            if (
                isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and (kind := self.dictionary_kind(node.func.value))
                and len(node.args) in (1, 2) and not node.keywords
                and not any(isinstance(arg, ast.Starred) for arg in node.args)
            ):
                self.changed = True
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="_blast_observe_lookup", ctx=ast.Load()),
                        args=[ast.Constant(kind), node.func.value, *node.args],
                        keywords=[],
                    ),
                    node,
                )
            return node

        def visit_Subscript(self, node):
            node = self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and (kind := self.dictionary_kind(node.value)):
                self.changed = True
                return ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="_blast_observe_item", ctx=ast.Load()),
                        args=[ast.Constant(kind), node.value, node.slice],
                        keywords=[],
                    ),
                    node,
                )
            return node

        def visit_Compare(self, node):
            node = self.generic_visit(node)
            if not (identity_site(node) or order_site(node)):
                return node
            self.changed = True
            key = f"comparison/{self.path}:{node.lineno}:{node.col_offset}"
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id=PROBE, ctx=ast.Load()),
                    args=[
                        ast.Constant(key), node.left, node.comparators[0],
                        ast.Constant(type(node.ops[0]).__name__),
                    ],
                    keywords=[],
                ),
                node,
            )

    def get_code(loader, fullname):
        filename = loader.get_filename(fullname)
        if filename.startswith(source_root):
            source = loader.get_data(filename)
            if any(token in source for token in (b" is ", b"<", b">", b"NUMERIC_ESCAPES")):
                transform = Observe(filename[len(str(root)) + 1 :])
                tree = transform.visit(ast.parse(source, filename=filename))
                if transform.changed:
                    ast.fix_missing_locations(tree)
                    return compile(tree, filename, "exec", dont_inherit=True)
        return original_loader(loader, fullname)

    importlib.machinery.SourceFileLoader.get_code = get_code
