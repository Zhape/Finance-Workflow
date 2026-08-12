"""The SQLAlchemy metadata and the SQL migrations must describe one schema.

Queries are written once against `fw.db` metadata and run on SQLite in tests
and Postgres in production. If the two drift, tests pass locally and the
worker breaks against Supabase -- the worst possible failure shape. This
parses the migrations and compares table and column names.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fw.db import metadata  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

_CREATE_TABLE = re.compile(
    r"create table (?:if not exists )?public\.(\w+)\s*\((.*?)\n\);",
    re.IGNORECASE | re.DOTALL,
)


def _strip_noise(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)               # line comments
    return re.sub(r"\$\$.*?\$\$", "", sql, flags=re.DOTALL)  # function bodies


def _columns(body: str) -> set[str]:
    columns: set[str] = set()
    depth = 0
    current = ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            columns.add(current)
            current = ""
        else:
            current += char
    columns.add(current)

    names = set()
    for raw in columns:
        token = raw.strip().split()
        if not token:
            continue
        head = token[0].lower()
        # Table-level constraints, not columns.
        if head in {"primary", "unique", "constraint", "foreign", "check"}:
            continue
        names.add(head)
    return names


def _migration_schema() -> dict[str, set[str]]:
    schema: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = _strip_noise(path.read_text(encoding="utf-8"))
        for name, body in _CREATE_TABLE.findall(sql):
            schema[name] = _columns(body)
    return schema


def test_migrations_are_present():
    assert MIGRATIONS.is_dir(), f"no migrations directory at {MIGRATIONS}"
    assert _migration_schema(), "no create table statements parsed"


def test_every_metadata_table_exists_in_the_migrations():
    missing = set(metadata.tables) - set(_migration_schema())
    assert not missing, f"tables in fw.db but not in migrations: {sorted(missing)}"


@pytest.mark.parametrize("table_name", sorted(metadata.tables))
def test_columns_match(table_name):
    migrated = _migration_schema().get(table_name)
    assert migrated is not None, f"{table_name} is not in the migrations"

    declared = {c.name for c in metadata.tables[table_name].columns}
    assert declared == migrated, (
        f"{table_name}: only in fw.db {sorted(declared - migrated)}, "
        f"only in migrations {sorted(migrated - declared)}"
    )
