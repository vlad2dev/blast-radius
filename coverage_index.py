"""Write baseline coverage as an index and load only queried entries."""

import json
import sqlite3
import sys
from functools import cache
from pathlib import Path


class CoverageIndex:
    def __init__(self, connection, section):
        self.connection = connection
        self.section = section

    @cache
    def get(self, key, default=()):
        row = self.connection.execute(
            "SELECT value FROM lookup WHERE section = ? AND key = ?", (self.section, key)
        ).fetchone()
        return tuple(json.loads(row[0])) if row else default


def load(path):
    mapping = json.loads(path.read_text())
    index = path.with_suffix(".sqlite")
    if index.exists():
        connection = sqlite3.connect(index.as_uri() + "?mode=ro&immutable=1", uri=True)
        for name in ("coverage", "test_references"):
            mapping[name] = CoverageIndex(connection, name)
    return mapping


def convert(path):
    mapping = json.loads(path.read_text())
    with sqlite3.connect(path.with_suffix(".sqlite")) as connection:
        connection.execute(
            "CREATE TABLE lookup(section TEXT, key TEXT, value TEXT NOT NULL, "
            "PRIMARY KEY (section, key)) WITHOUT ROWID"
        )
        for name in ("coverage", "test_references"):
            items = mapping.pop(name)
            connection.executemany(
                "INSERT INTO lookup VALUES (?, ?, ?)",
                ((name, key, json.dumps(values, separators=(",", ":"))) for key, values in items.items()),
            )
    path.write_text(json.dumps(mapping, separators=(",", ":")))


if __name__ == "__main__":
    convert(Path(sys.argv[1]))
