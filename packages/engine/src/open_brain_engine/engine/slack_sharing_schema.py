"""Schema17 Slack Work sharing and explicit runtime cutover."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

SLACK_SHARING_SCHEMA = (
    """CREATE TABLE slack_recall_selections (
 selection_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
 request_bytes BLOB NOT NULL, request_sha256 TEXT NOT NULL,
 selection_bytes BLOB NOT NULL, selection_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL
)""",
    """CREATE TRIGGER slack_recall_selections_update_immutable
BEFORE UPDATE ON slack_recall_selections
BEGIN SELECT RAISE(ABORT, 'selection is immutable'); END""",
    """CREATE TRIGGER slack_recall_selections_delete_immutable
BEFORE DELETE ON slack_recall_selections
BEGIN SELECT RAISE(ABORT, 'selection is immutable'); END""",
    "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=12),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=17)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,12,17)",
)
SLACK_SHARING_MIGRATION = _migration(17, "slack-work-sharing", SLACK_SHARING_SCHEMA)


def migrate_slack_sharing(
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
        if state.state in {"current", "supported_old"} and state.version in {17, 18}:
            return
        if state.state != "supported_old" or state.version != 16:
            raise T03Error("operation_pending")
        checkpoint("slack_sharing_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        require_historical_snapshot_settled(connection, profile)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:17],
            schema_version=17,
        )
        upgraded = classify_local_schema(connection)
        if upgraded.state not in {"current", "supported_old"} or upgraded.version != 17:
            raise T03Error("operation_pending")
        checkpoint("slack_sharing_before_commit")
        require_historical_snapshot_settled(connection, profile)
        admission.validate(profile)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise T03Error("operation_pending")
        connection.commit()
        committed = True
        checkpoint("slack_sharing_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError("Slack sharing migration committed") from error
        raise
    finally:
        connection.close()
