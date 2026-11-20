"""Observe the operand types of identity comparisons in the pinned source."""

import ast
import builtins
import importlib.machinery

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


def install(root, record):
    source_root = str(root) + "/sqlglot/"
    original_loader = importlib.machinery.SourceFileLoader.get_code

    def observe(key, left, right, negated):
        if not comparison_agrees_with_identity(left, right):
            record(key + "/unproven")
        record(key)
        return (left is not right) if negated else (left is right)

    setattr(builtins, PROBE, observe)

    class Observe(ast.NodeTransformer):
        def __init__(self, path):
            self.path = path
            self.changed = False

        def visit_Compare(self, node):
            node = self.generic_visit(node)
            if not identity_site(node):
                return node
            self.changed = True
            key = f"identity/{self.path}:{node.lineno}:{node.col_offset}"
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id=PROBE, ctx=ast.Load()),
                    args=[
                        ast.Constant(key), node.left, node.comparators[0],
                        ast.Constant(isinstance(node.ops[0], ast.IsNot)),
                    ],
                    keywords=[],
                ),
                node,
            )

    def get_code(loader, fullname):
        filename = loader.get_filename(fullname)
        if filename.startswith(source_root):
            source = loader.get_data(filename)
            if b" is " in source:
                transform = Observe(filename[len(str(root)) + 1 :])
                tree = transform.visit(ast.parse(source, filename=filename))
                if transform.changed:
                    ast.fix_missing_locations(tree)
                    return compile(tree, filename, "exec", dont_inherit=True)
        return original_loader(loader, fullname)

    importlib.machinery.SourceFileLoader.get_code = get_code
