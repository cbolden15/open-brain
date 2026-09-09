"""Historical SQLite rematerialization for integration fixtures with real Portable files."""

import sqlite3
from pathlib import Path

FIXTURES = Path(__file__).parents[5] / "tests/fixtures/local-schema"


def rematerialize_w2(connection: sqlite3.Connection, *, version: int = 1) -> None:
    tables = (
        "captures",
        "spaces",
        "space_operations",
        "route_operations",
        "proposal_sets",
        "proposals",
        "decisions",
        "search_documents",
    )
    rows = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        connection.execute(f'DROP TRIGGER "{name}"')
    for table in (
        "search_documents_fts",
        "search_fts_identity",
        "markdown_import_files",
        "markdown_import_revisions",
        "markdown_import_roots",
        "schema_migrations",
        *tables,
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.executescript((FIXTURES / "w2.sql").read_text())
    for table, values in rows.items():
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        connection.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
    connection.execute(f"PRAGMA user_version = {version}")
