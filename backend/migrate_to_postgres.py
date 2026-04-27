from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .db import configured_database_url, open_database, use_postgres
from .literature import LiteratureService
from .service import BackendService, DEFAULT_DB_PATH
from .storage import blob_enabled, upload_bytes_to_blob


def _table_names(source: sqlite3.Connection) -> list[str]:
    rows = source.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def _columns(source: sqlite3.Connection, table: str) -> list[str]:
    rows = source.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _upload_source_file(row: dict[str, Any]) -> str:
    storage_path = Path(row["storage_path"])
    if not storage_path.exists() or not blob_enabled():
        return row["storage_path"]
    uploaded = upload_bytes_to_blob(
        pathname=f"draftrefine/migrated/source-files/{storage_path.name}",
        body=storage_path.read_bytes(),
        content_type=row.get("content_type") or "application/octet-stream",
    )
    return uploaded["url"]


def _upload_attachment(row: dict[str, Any]) -> str:
    local_path = (row.get("local_path") or "").strip()
    if not local_path or not blob_enabled():
        return local_path
    path = Path(local_path)
    if not path.exists():
        return local_path
    uploaded = upload_bytes_to_blob(
        pathname=f"draftrefine/migrated/literature/{path.name}",
        body=path.read_bytes(),
        content_type="application/pdf",
    )
    return uploaded["url"]


def migrate(source_db_path: Path) -> None:
    database_url = configured_database_url()
    if not database_url or not use_postgres():
        raise RuntimeError("Set DRAFTREFINE_DATABASE_URL or POSTGRES_URL before running migration.")

    os.environ["DRAFTREFINE_SKIP_DEMO_SEED"] = "1"

    BackendService(database_path=source_db_path)
    LiteratureService(database_path=source_db_path)

    source = sqlite3.connect(source_db_path)
    source.row_factory = sqlite3.Row
    try:
        tables = _table_names(source)
        with open_database(source_db_path) as target:
            for table in tables:
                target.execute(f"DELETE FROM {table}")
            for table in tables:
                columns = _columns(source, table)
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    continue
                placeholders = ", ".join("?" for _ in columns)
                insert_sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
                for row in rows:
                    payload = dict(row)
                    if table == "source_files":
                        payload["storage_path"] = _upload_source_file(payload)
                    elif table == "literature_attachments":
                        payload["local_path"] = _upload_attachment(payload)
                    target.execute(insert_sql, tuple(payload[column] for column in columns))
            target.commit()
    finally:
        source.close()


def main() -> None:
    source_path = Path(os.getenv("DRAFTREFINE_SOURCE_SQLITE_PATH", str(DEFAULT_DB_PATH)))
    migrate(source_path)
    print(f"Migrated SQLite data from {source_path} to PostgreSQL target.")


if __name__ == "__main__":
    main()
