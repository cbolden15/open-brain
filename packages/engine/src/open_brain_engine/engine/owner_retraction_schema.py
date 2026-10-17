"""Schema19 owner retraction evidence and explicit runtime cutover."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

OWNER_RETRACTION_SCHEMA = (
    """CREATE TABLE retraction_previews (
 preview_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
 brain_id TEXT NOT NULL, issuer_epoch INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 impact_bytes BLOB NOT NULL, impact_sha256 TEXT NOT NULL
)""",
    """CREATE TABLE retraction_approvals (
 operation_id TEXT PRIMARY KEY REFERENCES lifecycle_journal_requests(operation_id),
 request_bytes BLOB NOT NULL, request_sha256 TEXT NOT NULL UNIQUE,
 receipt_bytes BLOB NOT NULL, receipt_sha256 TEXT NOT NULL UNIQUE
)""",
    """CREATE TRIGGER retraction_approvals_update_immutable
BEFORE UPDATE ON retraction_approvals
BEGIN SELECT RAISE(ABORT, 'retraction approval evidence is immutable'); END""",
    """CREATE TRIGGER retraction_approvals_delete_immutable
BEFORE DELETE ON retraction_approvals
BEGIN SELECT RAISE(ABORT, 'retraction approval evidence is immutable'); END""",
    "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=14),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=19)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,14,19)",
)
OWNER_RETRACTION_MIGRATION = _migration(19, "owner-retraction", OWNER_RETRACTION_SCHEMA)


def migrate_owner_retraction(
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
        _validate_upgrade_data,
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
        if state.state in {"current", "supported_old"} and state.version in {19, 20}:
            return
        if state.state != "supported_old" or state.version != 18:
            raise T03Error("operation_pending")
        checkpoint("owner_retraction_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        if classify_local_schema(connection) != state:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:19],
            schema_version=19,
        )
        upgraded = classify_local_schema(connection)
        if upgraded.state not in {"current", "supported_old"} or upgraded.version != 19:
            raise T03Error("operation_pending")
        checkpoint("owner_retraction_before_commit")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        admission.validate(profile)
        connection.commit()
        committed = True
        checkpoint("owner_retraction_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError(
                "owner retraction migration committed"
            ) from error
        raise
    finally:
        connection.close()
