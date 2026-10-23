"""Schema18 capture/session lifecycle journal and explicit runtime cutover."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import SchemaError, _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .capture_lifecycle_journal import JOURNAL_CONTRACT, JOURNAL_SCHEMA

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

CAPTURE_LIFECYCLE_SCHEMA = (
    *JOURNAL_SCHEMA,
    "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=13),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=18)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,13,18)",
)
CAPTURE_LIFECYCLE_MIGRATION = _migration(18, "capture-session-lifecycle", CAPTURE_LIFECYCLE_SCHEMA)


def initialize_capture_lifecycle_identity(connection: sqlite3.Connection) -> None:
    """Bind the newly installed journal to the existing durable Brain identity."""
    if not connection.in_transaction:
        raise SchemaError("capture lifecycle identity requires a schema transaction")
    identities = connection.execute("SELECT brain_id FROM brain_identity").fetchall()
    if len(identities) != 1:
        raise SchemaError("capture lifecycle requires a durable Brain identity")
    connection.execute(
        "INSERT INTO lifecycle_journal_identity VALUES(1,?,?)",
        (JOURNAL_CONTRACT, identities[0][0]),
    )


def migrate_capture_lifecycle(
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
        if state.state in {"current", "supported_old"} and state.version in {18, 19, 20, 21, 22}:
            return
        if state.state != "supported_old" or state.version != 17:
            raise T03Error("operation_pending")
        checkpoint("capture_lifecycle_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        if classify_local_schema(connection) != state:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:18],
            schema_version=18,
        )
        initialize_capture_lifecycle_identity(connection)
        upgraded = classify_local_schema(connection)
        if upgraded.state not in {"current", "supported_old"} or upgraded.version != 18:
            raise T03Error("operation_pending")
        checkpoint("capture_lifecycle_before_commit")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        admission.validate(profile)
        connection.commit()
        committed = True
        checkpoint("capture_lifecycle_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError(
                "capture lifecycle migration committed"
            ) from error
        raise
    finally:
        connection.close()
