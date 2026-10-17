"""Concrete successor DDL rehearsal, not a production cutover."""

import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_lifecycle_journal import JOURNAL_SCHEMA
from open_brain_engine.engine.consumer_schema import (
    CONSUMER_INTAKE_MIGRATION,
    CONSUMER_INTAKE_SCHEMA,
)


def _schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    for statement in JOURNAL_SCHEMA:
        connection.execute(statement)
    connection.execute("INSERT INTO lifecycle_journal_identity VALUES(1,?,?)",
                       ("ob.capture-lifecycle-journal.v1", "synthetic-brain"))
    connection.execute("CREATE TABLE engine_generations (projection_policy_version INTEGER)")
    connection.execute("INSERT INTO engine_generations VALUES(1)")
    connection.execute("CREATE TABLE runtime_compatibility (singleton INTEGER, "
                       "minimum_runtime_session_version INTEGER,state_schema_version INTEGER)")
    connection.execute("INSERT INTO runtime_compatibility VALUES(1,14,19)")


def test_successor_ddl_preserves_journal_tables_and_fences_runtime() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        _schema(connection)
        before = connection.execute("SELECT name,sql FROM sqlite_master "
                                    "WHERE type='table' AND name GLOB 'lifecycle_journal_*' "
                                    "ORDER BY name").fetchall()
        for statement in CONSUMER_INTAKE_SCHEMA:
            connection.execute(statement)
        assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [(1,15,20)]
        assert connection.execute("SELECT contract FROM lifecycle_journal_identity").fetchone() == (
            "ob.capture-lifecycle-journal.v2",)
        assert connection.execute("SELECT name,sql FROM sqlite_master "
                                  "WHERE type='table' AND name GLOB 'lifecycle_journal_*' "
                                  "ORDER BY name").fetchall() == before
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE lifecycle_journal_identity SET contract='forged'")
        connection.execute("INSERT INTO consumer_intent_objects VALUES(?,?,?)",
                           (sha256(b'{}').hexdigest(), "operation", b'{}'))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM consumer_intent_objects")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO consumer_approvals VALUES(?,?,?,?)",
                               ("missing-operation", sha256(b'{}').hexdigest(), b'{}', "a"*64))


def test_precommit_failure_rolls_back_identity_and_all_new_tables() -> None:
    with closing(sqlite3.connect(":memory:")) as connection:
        _schema(connection)
        connection.commit()
        before = tuple(connection.iterdump())
        connection.execute("BEGIN IMMEDIATE")
        for statement in CONSUMER_INTAKE_SCHEMA[:-1]:
            connection.execute(statement)
        connection.rollback()
        assert tuple(connection.iterdump()) == before


def test_rehearsal_restores_exact_pre_migration_clone() -> None:
    with (
        closing(sqlite3.connect(":memory:")) as original,
        closing(sqlite3.connect(":memory:")) as clone,
    ):
        _schema(original)
        original.commit()
        original.backup(clone)
        fingerprint = sha256(canonical(list(clone.iterdump()))).hexdigest()
        original.execute("BEGIN IMMEDIATE")
        for statement in CONSUMER_INTAKE_SCHEMA:
            original.execute(statement)
        original.rollback()
        clone.backup(original)
        assert sha256(canonical(list(original.iterdump()))).hexdigest() == fingerprint
        assert CONSUMER_INTAKE_MIGRATION.version == 20


def test_concrete_schema_matches_reviewed_golden_bytes() -> None:
    migration = CONSUMER_INTAKE_MIGRATION
    expected = canonical({
        "contract": "ob.consumer-schema.v1", "version": migration.version,
        "name": migration.name, "checksum": migration.checksum,
        "statements": list(migration.statements),
    })
    assert (Path(__file__).parent / "fixtures/consumer-schema20.json").read_bytes() == expected
