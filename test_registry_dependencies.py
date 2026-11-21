"""Independent dependency fixtures; no SQLGlot runtime or label lookup."""
import ast
import unittest
from functools import cache
from pathlib import Path

SOURCE = ast.parse(Path(__file__).with_name("selector.py").read_text())
FUNCTION = next(n for n in SOURCE.body if isinstance(n, ast.FunctionDef) and n.name == "registry_dependents")

class RegistryDependencies(unittest.TestCase):
    def scan(self, code):
        files = {"sqlglot/dialects/copy.py": code.encode()}
        namespace = dict(ast=ast, cache=cache,
            project_source_files=lambda: tuple(files.items()),
            module_syntax=lambda path: ast.parse(files[path]))
        exec(compile(ast.Module(body=[FUNCTION], type_ignores=[]), "selector.py", "exec"), namespace)
        return namespace["registry_dependents"]("sqlglot/dialects/hive.py", "Hive", "KEYWORDS")

    def test_import_forms_and_aliases(self):
        forms = [
            ("from sqlglot.dialects.hive import Hive", "Hive"),
            ("from .hive import Hive as H", "H"),
            ("import sqlglot.dialects.hive as h", "h.Hive"),
            ("import sqlglot.dialects.hive", "sqlglot.dialects.hive.Hive"),
            ("from . import hive as h", "h.Hive"),
            ("from sqlglot.dialects import hive", "hive.Hive"),
            ("from .hive import Hive\nAlias = Hive", "Alias"),
        ]
        for statement, name in forms:
            with self.subTest(statement=statement):
                self.assertEqual(self.scan(statement + "\nclass Copy:\n    KEYWORDS = {**" + name + ".Tokenizer.KEYWORDS}\n"),
                                 (("sqlglot.dialects.copy", "Copy"),))

    def test_wildcard_is_uncertain(self):
        self.assertIsNone(self.scan("from .hive import *\nclass Copy:\n    KEYWORDS = Hive.Tokenizer.KEYWORDS\n"))

    def test_module_level_read_is_uncertain(self):
        self.assertIsNone(self.scan("from .hive import Hive\nKEYWORDS = Hive.Tokenizer.KEYWORDS\n"))

    def test_unrelated_import(self):
        self.assertEqual(self.scan("from .other import Other\nclass Copy:\n    KEYWORDS = Other.KEYWORDS\n"), ())

if __name__ == "__main__":
    unittest.main()
