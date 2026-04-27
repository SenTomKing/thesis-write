from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency in local sqlite mode
    psycopg = None
    dict_row = None


POSTGRES_ENV_KEYS = (
    "DRAFTREFINE_DATABASE_URL",
    "POSTGRES_URL_NON_POOLING",
    "POSTGRES_URL",
    "DATABASE_URL",
)

_SQLITE_MASTER_WITH_NAME_RE = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*'([^']+)'",
    re.I,
)
_SQLITE_MASTER_ALL_RE = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'",
    re.I,
)
_PRAGMA_TABLE_INFO_RE = re.compile(r"PRAGMA\s+table_info\(([^)]+)\)", re.I)


def configured_database_url() -> str | None:
    for key in POSTGRES_ENV_KEYS:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return None


def use_postgres() -> bool:
    database_url = configured_database_url()
    if not database_url:
        return False
    return database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://"))


class CompatRow:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = dict(mapping)
        self._keys = list(self._mapping.keys())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._mapping[self._keys[key]]
        return self._mapping[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)

    def keys(self) -> list[str]:
        return list(self._keys)

    def items(self) -> list[tuple[str, Any]]:
        return [(key, self._mapping[key]) for key in self._keys]

    def values(self) -> list[Any]:
        return [self._mapping[key] for key in self._keys]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.values())

    def __len__(self) -> int:
        return len(self._keys)


class CompatCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self) -> CompatRow | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, CompatRow):
            return row
        return CompatRow(row if isinstance(row, dict) else dict(row))

    def fetchall(self) -> list[CompatRow]:
        rows = self._cursor.fetchall()
        normalized: list[CompatRow] = []
        for row in rows:
            if isinstance(row, CompatRow):
                normalized.append(row)
            elif isinstance(row, dict):
                normalized.append(CompatRow(row))
            else:
                normalized.append(CompatRow(dict(row)))
        return normalized

    def __iter__(self) -> Iterator[CompatRow]:
        return iter(self.fetchall())


class _StaticResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = [CompatRow(row) for row in rows]

    def fetchone(self) -> CompatRow | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[CompatRow]:
        return list(self._rows)


class PostgresCompatConnection:
    def __init__(self, database_url: str) -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required when using a PostgreSQL database URL.")
        normalized_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._connection = psycopg.connect(normalized_url, row_factory=dict_row)

    def __enter__(self) -> "PostgresCompatConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            self.rollback()
        self.close()

    def execute(self, sql: str, params: Any = ()) -> CompatCursor | _StaticResult:
        metadata_result = self._maybe_handle_sqlite_metadata(sql)
        if metadata_result is not None:
            return metadata_result
        translated_sql = self._translate_sql(sql)
        cursor = self._connection.execute(translated_sql, params)
        return CompatCursor(cursor)

    def executescript(self, script: str) -> None:
        for statement in _split_sql_script(script):
            self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def _translate_sql(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def _maybe_handle_sqlite_metadata(self, sql: str) -> _StaticResult | None:
        compact = " ".join(sql.strip().split())
        pragma_match = _PRAGMA_TABLE_INFO_RE.fullmatch(compact)
        if pragma_match:
            table_name = pragma_match.group(1).strip().strip("'\"")
            cursor = self._connection.execute(
                """
                SELECT column_name AS name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            return _StaticResult(cursor.fetchall())

        sqlite_with_name_match = _SQLITE_MASTER_WITH_NAME_RE.fullmatch(compact)
        if sqlite_with_name_match:
            table_name = sqlite_with_name_match.group(1)
            cursor = self._connection.execute(
                """
                SELECT table_name AS name
                FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' AND table_name = %s
                """,
                (table_name,),
            )
            return _StaticResult(cursor.fetchall())

        sqlite_all_match = _SQLITE_MASTER_ALL_RE.fullmatch(compact)
        if sqlite_all_match:
            cursor = self._connection.execute(
                """
                SELECT table_name AS name
                FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            return _StaticResult(cursor.fetchall())
        return None


def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    previous = ""
    for char in script:
        if char == "'" and not in_double and previous != "\\":
            in_single = not in_single
        elif char == '"' and not in_single and previous != "\\":
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        previous = char
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


@contextmanager
def open_database(database_path: Path) -> Iterator[Any]:
    database_url = configured_database_url()
    if use_postgres() and database_url:
        connection = PostgresCompatConnection(database_url)
        try:
            yield connection
        finally:
            connection.close()
        return

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()
