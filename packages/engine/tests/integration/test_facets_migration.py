"""Additive DDL checks; exclusive runtime/catalog registration is coordinator-owned."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from open_brain_engine.engine.facet_schema_migration import (
    FACET_MIGRATION,
    FACET_SCHEMA,
    FACET_TABLE_SCHEMA,
    facet_schema_available,
)
from open_brain_engine.engine.facet_vault_schema import FACET_VAULT_SCHEMA
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    value = sqlite3.connect(":memory:", isolation_level=None)
    value.execute("PRAGMA foreign_keys=ON")
    value.create_function("local_public_search_text", 2, lambda text, _reference: text)
    for migration in LOCAL_MIGRATIONS[:14]:
        for statement in migration.statements:
            value.execute(statement)
    value.execute("PRAGMA user_version=14")
    try:
        yield value
    finally:
        value.close()


def test_additive_schema_includes_vault_custody_and_exact_compatibility(
    connection: sqlite3.Connection,
) -> None:
    assert not facet_schema_available(connection)
    connection.execute("BEGIN IMMEDIATE")
    for statement in FACET_SCHEMA:
        connection.execute(statement)
    assert facet_schema_available(connection)
    assert connection.execute("SELECT * FROM runtime_compatibility").fetchone() == (1, 10, 15)
    assert connection.execute("SELECT COUNT(*) FROM facet_dimensions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM facet_assignments").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM facet_projection_state").fetchone()[0] == 0
    assert FACET_MIGRATION.version == 15
    assert FACET_MIGRATION.statements == FACET_SCHEMA
    assert FACET_SCHEMA[: len(FACET_TABLE_SCHEMA)] == FACET_TABLE_SCHEMA
    assert (
        FACET_SCHEMA[len(FACET_TABLE_SCHEMA) : len(FACET_TABLE_SCHEMA) + len(FACET_VAULT_SCHEMA)]
        == FACET_VAULT_SCHEMA
    )
    connection.execute("ROLLBACK")
    assert not facet_schema_available(connection)
    assert connection.execute("SELECT * FROM runtime_compatibility").fetchone() == (1, 9, 14)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 14


def test_additive_foreign_keys_and_versions_are_enforced(connection: sqlite3.Connection) -> None:
    for statement in FACET_SCHEMA:
        connection.execute(statement)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO facet_subject_versions VALUES('source','source-test',-1)")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO facet_values VALUES('value-test','missing-dimension',"
            "'Synthetic','synthetic',0,1,'t','t')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO facet_assignments VALUES('source','missing-subject','missing-value',1,1,1)"
        )
