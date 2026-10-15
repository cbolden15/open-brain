"""Additive schema-15 facet DDL and explicitly admitted empty-ledger cutover."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .facet_vault_schema import FACET_VAULT_SCHEMA

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

FACET_SCHEMA_VERSION = 15
FACET_RUNTIME_SESSION_VERSION = 10
FACET_TABLE_SCHEMA = (
    """CREATE TABLE facet_dimensions (
 dimension_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
 archived INTEGER NOT NULL CHECK(archived IN (0,1)),
 version INTEGER NOT NULL CHECK(version>=1 AND version<=9007199254740991),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)""",
    """CREATE TABLE facet_values (
 value_id TEXT PRIMARY KEY, dimension_id TEXT NOT NULL REFERENCES facet_dimensions(dimension_id),
 name TEXT NOT NULL, name_key TEXT NOT NULL,
 archived INTEGER NOT NULL CHECK(archived IN (0,1)),
 version INTEGER NOT NULL CHECK(version>=1 AND version<=9007199254740991),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(dimension_id,name_key)
)""",
    """CREATE TABLE facet_subject_versions (
 subject_kind TEXT NOT NULL CHECK(subject_kind IN ('source','canonical')),
 subject_id TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>=0 AND version<=9007199254740991),
 PRIMARY KEY(subject_kind,subject_id)
)""",
    """CREATE TABLE facet_assignments (
 subject_kind TEXT NOT NULL CHECK(subject_kind IN ('source','canonical')),
 subject_id TEXT NOT NULL, value_id TEXT NOT NULL REFERENCES facet_values(value_id),
 present INTEGER NOT NULL CHECK(present IN (0,1)),
 membership_version INTEGER NOT NULL CHECK(membership_version>=1
 AND membership_version<=9007199254740991),
 last_event_sequence INTEGER NOT NULL CHECK(last_event_sequence>=1
 AND last_event_sequence<=9007199254740991),
 PRIMARY KEY(subject_kind,subject_id,value_id),
 FOREIGN KEY(subject_kind,subject_id) REFERENCES facet_subject_versions(subject_kind,subject_id)
)""",
    """CREATE TABLE facet_queries (
 query_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
 selector_json TEXT NOT NULL, archived INTEGER NOT NULL CHECK(archived IN (0,1)),
 version INTEGER NOT NULL CHECK(version>=1 AND version<=9007199254740991),
 selection_version INTEGER NOT NULL CHECK(selection_version>=1 AND selection_version<=version),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)""",
    """CREATE TABLE facet_page_queries (
 page_id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES facet_queries(query_id),
 version INTEGER NOT NULL CHECK(version>=1 AND version<=9007199254740991)
)""",
    """CREATE TABLE facet_events (
 event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE
 CHECK(sequence>=1 AND sequence<=9007199254740991), operation_id TEXT NOT NULL UNIQUE,
 actor_id TEXT NOT NULL, recorded_at TEXT NOT NULL, kind TEXT NOT NULL,
 event_json TEXT NOT NULL
)""",
    """CREATE TABLE facet_operations (
 operation_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, request_sha256 TEXT NOT NULL
 CHECK(length(request_sha256)=64), request_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
 event_id TEXT REFERENCES facet_events(event_id)
)""",
)
FACET_SCHEMA = (
    *FACET_TABLE_SCHEMA,
    *FACET_VAULT_SCHEMA,
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=10),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=15)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,10,15)",
)
FACET_MIGRATION = _migration(15, "owner-defined-facets", FACET_SCHEMA)


def facet_schema_available(connection: sqlite3.Connection) -> bool:
    return bool(
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
            "('facet_dimensions','facet_values','facet_subject_versions','facet_assignments',"
            "'facet_queries','facet_page_queries','facet_events','facet_operations')"
        ).fetchone()[0]
        == 8
    )


def migrate_facets(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    """Run only through the coordinator's exclusive schema/session admission.

    Registration of migration 15 and recognition of its exact shape belong to
    the coordinator. A failed pre-commit stage leaves schema 14 unchanged; a
    post-commit fault explicitly reports committed-upgrade recovery.
    """
    from .historical_recovery import require_historical_snapshot_settled
    from .local_schema import (
        PHASE1_STATE_DATABASE,
        LocalSchemaUpgradeCommittedError,
        _MigrationClock,
        classify_local_schema,
    )
    from .local_schema_catalog import LOCAL_MIGRATIONS
    from .t03_contracts import T03Error

    admission.validate(profile)
    if admission.live_peer_count:
        raise T03Error("operation_pending")
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    committed = False
    try:
        state = classify_local_schema(connection)
        if state.state in {"current", "supported_old"} and state.version in {15, 16, 17}:
            if not facet_schema_available(connection):
                raise T03Error("operation_pending")
            return
        if state.state != "supported_old" or state.version != 14:
            raise T03Error("operation_pending")
        checkpoint("facet_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        state = classify_local_schema(connection)
        if state.state != "supported_old" or state.version != 14:
            raise T03Error("operation_pending")
        if len(LOCAL_MIGRATIONS) < FACET_SCHEMA_VERSION:
            raise T03Error("operation_pending")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1 or (
            connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:FACET_SCHEMA_VERSION],
            schema_version=FACET_SCHEMA_VERSION,
        )
        for table in (
            "facet_dimensions",
            "facet_values",
            "facet_subject_versions",
            "facet_assignments",
            "facet_queries",
            "facet_page_queries",
            "facet_events",
            "facet_operations",
        ):
            if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                raise T03Error("operation_pending")
        if classify_local_schema(connection).version != FACET_SCHEMA_VERSION:
            raise T03Error("operation_pending")
        checkpoint("facet_schema_applied")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        admission.validate(profile)
        connection.execute("COMMIT")
        committed = True
        checkpoint("facet_complete")
    except BaseException as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if committed:
            raise LocalSchemaUpgradeCommittedError("facet schema upgrade committed") from error
        raise
    finally:
        connection.close()
