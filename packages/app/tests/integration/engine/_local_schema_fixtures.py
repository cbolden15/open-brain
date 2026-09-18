"""Historical SQLite rematerialization for integration fixtures with real Portable files."""

import sqlite3
from pathlib import Path

import pytest
from open_brain_engine.engine import local_schema
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS

FIXTURES = Path(__file__).parents[5] / "tests/fixtures/local-schema"


def use_schema_six_runtime(monkeypatch: pytest.MonkeyPatch, namespace: dict[str, object]) -> None:
    """Execute historical migration/restore contracts at their fixed target version.

    Existing SQL fixtures and their checksums remain unchanged. Current schema-seven
    activation is exercised independently by source migration and source task tests.
    """
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
    if "PHASE1_STATE_SCHEMA_VERSION" in namespace:
        monkeypatch.setitem(namespace, "PHASE1_STATE_SCHEMA_VERSION", 6)
    if "LOCAL_MIGRATIONS" in namespace:
        monkeypatch.setitem(namespace, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])


def schema_six_script(script: str) -> str:
    """Select the same historical target in an actual child-process failure schedule."""
    return (
        "from open_brain_engine.engine import local_schema as _historical_schema\n"
        "_historical_schema.PHASE1_STATE_SCHEMA_VERSION = 6\n"
        "_historical_schema.LOCAL_MIGRATIONS = _historical_schema.LOCAL_MIGRATIONS[:6]\n"
        + script
    )


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
        "managed_recovery_decisions",
        "managed_write_authority",
        "review_page_heads",
        "review_sources",
        "review_contexts",
        "managed_suggestions",
        "managed_inference_requests",
        "managed_inference_budgets",
        "managed_exclusions",
        "managed_consents",
        "managed_conflicts",
        "managed_links",
        "managed_note_observations",
        "managed_operations",
        "managed_notes",
        "managed_note_revisions",
        "managed_workspaces",
        "runtime_compatibility",
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
