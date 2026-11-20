"""Identify SQLGlot receivers with class-level state."""

from functools import cache

RECEIVER_METHODS = {
    ("sqlglot/dialects/dialect.py", "Dialect.__init__"),
    ("sqlglot/dialects/dialect.py", "Dialect.generate"),
    ("sqlglot/dialects/dialect.py", "Dialect.parse"),
    ("sqlglot/dialects/dialect.py", "Dialect.tokenize"),
    ("sqlglot/expressions/core.py", "Expr.__init__"),
    ("sqlglot/generator.py", "Generator.__init__"),
    ("sqlglot/parser.py", "Parser.__init__"),
    ("sqlglot/tokens.py", "Tokenizer.__init__"),
}


@cache
def receivers(kind):
    return tuple(
        f"{cls.__module__}:{cls.__qualname__}"
        for cls in kind.__mro__
        if cls.__module__.startswith("sqlglot.")
    )
