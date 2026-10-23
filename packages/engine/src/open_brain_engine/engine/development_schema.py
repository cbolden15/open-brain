"""Schema16 engine-owned development evidence and explicit runtime cutover."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

DEVELOPMENT_SCHEMA = (
    """CREATE TABLE development_evidence (
 derived_capture_id TEXT PRIMARY KEY REFERENCES captures(capture_id),
 operation_id TEXT NOT NULL UNIQUE, principal_id TEXT NOT NULL,
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 request_json TEXT NOT NULL, receipt_json TEXT NOT NULL
)""",
    """CREATE TABLE development_append_operations (
 delivery_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
 request_json TEXT NOT NULL, draft_json TEXT NOT NULL,
 proposal_id TEXT REFERENCES proposals(proposal_id)
)""",
    """CREATE TRIGGER development_evidence_update_immutable BEFORE UPDATE ON development_evidence
BEGIN SELECT RAISE(ABORT, 'development evidence is immutable'); END""",
    """CREATE TRIGGER development_evidence_delete_immutable BEFORE DELETE ON development_evidence
BEGIN SELECT RAISE(ABORT, 'development evidence is immutable'); END""",
    """CREATE TRIGGER development_append_request_immutable
BEFORE UPDATE ON development_append_operations
WHEN NEW.delivery_id!=OLD.delivery_id OR NEW.request_sha256!=OLD.request_sha256
 OR NEW.request_json!=OLD.request_json OR NEW.draft_json!=OLD.draft_json
 OR (OLD.proposal_id IS NOT NULL AND NEW.proposal_id IS NOT OLD.proposal_id)
BEGIN SELECT RAISE(ABORT, 'append request is immutable'); END""",
    """CREATE TRIGGER development_append_delete_immutable
BEFORE DELETE ON development_append_operations
BEGIN SELECT RAISE(ABORT, 'append request is immutable'); END""",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=11),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=16)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,11,16)",
)
DEVELOPMENT_MIGRATION = _migration(16, "derived-development-evidence", DEVELOPMENT_SCHEMA)


def migrate_developments(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
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
        if state.state in {"current", "supported_old"} and state.version in set(range(16, 24)):
            return
        if state.state != "supported_old" or state.version != 15:
            raise T03Error("operation_pending")
        checkpoint("development_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        require_historical_snapshot_settled(connection, profile)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:16],
            schema_version=16,
        )
        upgraded = classify_local_schema(connection)
        if upgraded.state not in {"current", "supported_old"} or upgraded.version != 16:
            raise T03Error("operation_pending")
        checkpoint("development_before_commit")
        require_historical_snapshot_settled(connection, profile)
        admission.validate(profile)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise T03Error("operation_pending")
        connection.commit()
        committed = True
        checkpoint("development_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError("development migration committed") from error
        raise
    finally:
        connection.close()
