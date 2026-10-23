"""Concrete schema20 consumer evidence and projection design.

The catalog cutover is coordinated with journal2/runtime15/Portable15. Merely
importing this module does not register or apply a migration to any Brain.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import SchemaError, _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .capture_lifecycle_journal import JOURNAL_SCHEMA

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

CONSUMER_STORAGE_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE consumer_intent_objects (
 sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64),
 kind TEXT NOT NULL CHECK(kind IN
 ('operation','conversation','normalized','segment','profile','identity')),
 object_bytes BLOB NOT NULL CHECK(length(object_bytes)>0 AND length(object_bytes)<=33554432)
)""",
    """CREATE TABLE consumer_previews (
 preview_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
 principal_id TEXT NOT NULL, brain_id TEXT NOT NULL, issuer_epoch INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
 root_sha256 TEXT NOT NULL CHECK(length(root_sha256)=64),
 root_bytes BLOB NOT NULL CHECK(length(root_bytes)>0 AND length(root_bytes)<=16777216)
)""",
    """CREATE TABLE consumer_preview_objects (
 preview_id TEXT NOT NULL REFERENCES consumer_previews(preview_id) ON DELETE CASCADE,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64), kind TEXT NOT NULL,
 object_bytes BLOB NOT NULL CHECK(length(object_bytes)>0 AND length(object_bytes)<=33554432),
 PRIMARY KEY(preview_id,sha256)
)""",
    """CREATE TABLE consumer_approvals (
 operation_id TEXT PRIMARY KEY REFERENCES lifecycle_journal_requests(operation_id),
 root_sha256 TEXT NOT NULL REFERENCES consumer_intent_objects(sha256),
 request_bytes BLOB NOT NULL, request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64)
)""",
    """CREATE TABLE consumer_scopes (
 scope_id TEXT PRIMARY KEY, provider TEXT NOT NULL CHECK(provider IN ('chatgpt','claude')),
 label TEXT NOT NULL, namespace_id TEXT NOT NULL UNIQUE,
 created_operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 UNIQUE(provider,label)
)""",
    """CREATE TABLE consumer_archive_bindings (
 provider TEXT NOT NULL CHECK(provider IN ('chatgpt','claude')),
 archive_sha256 TEXT NOT NULL CHECK(length(archive_sha256)=64),
 scope_id TEXT NOT NULL REFERENCES consumer_scopes(scope_id),
 operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 PRIMARY KEY(provider,archive_sha256)
)""",
    """CREATE TABLE consumer_segment_bindings (
 capture_id TEXT PRIMARY KEY,
 operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 group_key TEXT NOT NULL CHECK(length(group_key)=64),
 revision_sha256 TEXT NOT NULL CHECK(length(revision_sha256)=64),
 segment_index INTEGER NOT NULL CHECK(segment_index>=0 AND segment_index<128),
 source_id TEXT NOT NULL, namespace_bytes BLOB NOT NULL,
 expected_source_bytes BLOB NOT NULL,
 normalized_sha256 TEXT NOT NULL REFERENCES consumer_intent_objects(sha256),
 envelope_sha256 TEXT NOT NULL REFERENCES consumer_intent_objects(sha256),
 UNIQUE(operation_id,group_key,segment_index)
)""",
    """CREATE TABLE consumer_outcomes (
 operation_id TEXT NOT NULL REFERENCES lifecycle_journal_requests(operation_id),
 group_key TEXT NOT NULL CHECK(length(group_key)=64),
 ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<10000),
 outcome TEXT NOT NULL CHECK(outcome IN
 ('committed','unchanged','conflict','retracted','no_text','halted')),
 receipt_bytes BLOB NOT NULL,
 receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
 PRIMARY KEY(operation_id,group_key), UNIQUE(operation_id,ordinal)
)""",
    "CREATE INDEX consumer_segment_bindings_source ON consumer_segment_bindings(source_id)",
    "CREATE INDEX consumer_segment_bindings_group "
    "ON consumer_segment_bindings(group_key,revision_sha256)",
)

CONSUMER_STORAGE_SCHEMA += tuple(
    f"CREATE TRIGGER {table}_{verb.lower()}_immutable BEFORE {verb} ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'consumer evidence is immutable'); END"
    for table in ("consumer_intent_objects", "consumer_approvals", "consumer_segment_bindings")
    for verb in ("UPDATE", "DELETE")
)

# Existing identity bytes explicitly change contract in this successor migration;
# retained requests/events/payloads and every predecessor DDL statement do not.
_IDENTITY_UPDATE_TRIGGER = next(
    statement for statement in JOURNAL_SCHEMA
    if statement.startswith("CREATE TRIGGER lifecycle_journal_identity_update ")
)
CONSUMER_INTAKE_SCHEMA = CONSUMER_STORAGE_SCHEMA + (
    "DROP TRIGGER lifecycle_journal_identity_update",
    "UPDATE lifecycle_journal_identity SET contract='ob.capture-lifecycle-journal.v2' "
    "WHERE singleton=1 AND contract='ob.capture-lifecycle-journal.v1'",
    _IDENTITY_UPDATE_TRIGGER,
    "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=15),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=20)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,15,20)",
)
CONSUMER_INTAKE_MIGRATION = _migration(20, "consumer-conversation-intake", CONSUMER_INTAKE_SCHEMA)


def initialize_consumer_lifecycle_identity(connection: sqlite3.Connection) -> None:
    """Finish fresh creation after the Brain and predecessor journal identities exist."""
    from .consumer_journal import migrate_lifecycle_journal_v2

    if not connection.in_transaction:
        raise SchemaError("consumer lifecycle identity requires a schema transaction")
    identities = connection.execute("SELECT brain_id FROM brain_identity").fetchall()
    if len(identities) != 1:
        raise SchemaError("consumer lifecycle requires a durable Brain identity")
    migrate_lifecycle_journal_v2(connection, brain_id=identities[0][0])


def migrate_consumer_intake(
    profile: LocalEngineContext, *, admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime], checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    """Exclusively admit schema20 without resuming or activating any accepted operation."""
    from .capture_lifecycle_journal import LifecycleJournal
    from .consumer_journal import LifecycleJournalV2
    from .historical_recovery import require_historical_snapshot_settled
    from .local_schema import (
        PHASE1_STATE_DATABASE,
        LocalSchemaUpgradeCommittedError,
        _MigrationClock,
        _validate_upgrade_data,
        classify_local_schema,
    )
    from .local_schema_catalog import LOCAL_MIGRATIONS
    from .t03_contracts import T03Error

    admission.validate(profile)
    if admission.live_peer_count:
        raise T03Error("operation_pending")
    connection = connect_database(
        root=profile.root, database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    committed = False
    try:
        state = classify_local_schema(connection)
        if state.state in {"current", "supported_old"} and state.version in {20, 21, 22}:
            return
        if state.state != "supported_old" or state.version != 19:
            raise T03Error("operation_pending")
        checkpoint("consumer_intake_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        if classify_local_schema(connection) != state:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        brain, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
        # Replaying into checked in-memory state validates evidence but performs
        # no stage-machine, custody, session, or activation writes.
        LifecycleJournal.for_engine(connection, brain_id=brain, issuer_epoch=epoch)._state()
        apply_migrations(connection, clock=_MigrationClock(clock),
                         migrations=LOCAL_MIGRATIONS[:20], schema_version=20)
        LifecycleJournalV2.for_engine(connection, brain_id=brain, issuer_epoch=epoch)._state()
        upgraded = classify_local_schema(connection)
        if upgraded.state not in {"current", "supported_old"} or upgraded.version != 20:
            raise T03Error("operation_pending")
        checkpoint("consumer_intake_before_commit")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        admission.validate(profile)
        connection.commit()
        committed = True
        checkpoint("consumer_intake_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError("consumer intake migration committed") from error
        raise
    finally:
        connection.close()
