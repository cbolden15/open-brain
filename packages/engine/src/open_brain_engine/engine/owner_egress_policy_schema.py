"""Schema22 owner egress policy: shareable tiers for external-provider reads."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

_SHARES = """share_public INTEGER NOT NULL CHECK(share_public IN (0,1)),
 share_work INTEGER NOT NULL CHECK(share_work IN (0,1)),
 share_personal INTEGER NOT NULL CHECK(share_personal IN (0,1))"""

OWNER_EGRESS_POLICY_SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE owner_egress_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 version INTEGER NOT NULL CHECK(version>=0),
 {_SHARES},
 recorded_at TEXT CHECK((version=0)=(recorded_at IS NULL)),
 operation_id TEXT CHECK((version=0)=(operation_id IS NULL))
)""",
    f"""CREATE TABLE owner_egress_policy_history (
 version INTEGER PRIMARY KEY CHECK(version>=1),
 {_SHARES},
 recorded_at TEXT NOT NULL,
 operation_id TEXT NOT NULL UNIQUE CHECK(length(operation_id) BETWEEN 1 AND 128)
)""",
    """CREATE TRIGGER owner_egress_policy_update_guard BEFORE UPDATE ON owner_egress_policy
WHEN OLD.singleton IS NOT NEW.singleton OR NEW.version <= OLD.version
BEGIN SELECT RAISE(ABORT, 'owner egress policy is monotone'); END""",
    """CREATE TRIGGER owner_egress_policy_delete_immutable BEFORE DELETE ON owner_egress_policy
BEGIN SELECT RAISE(ABORT, 'owner egress policy is retained'); END""",
    """CREATE TRIGGER owner_egress_policy_history_update_immutable
BEFORE UPDATE ON owner_egress_policy_history
BEGIN SELECT RAISE(ABORT, 'owner egress policy history is immutable'); END""",
    """CREATE TRIGGER owner_egress_policy_history_delete_immutable
BEFORE DELETE ON owner_egress_policy_history
BEGIN SELECT RAISE(ABORT, 'owner egress policy history is immutable'); END""",
    # Both upgraded and fresh Brains start with no shareable tier.
    "INSERT INTO owner_egress_policy VALUES(1,0,0,0,0,NULL,NULL)",
    "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=16),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=22)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,16,22)",
)
OWNER_EGRESS_POLICY_MIGRATION = _migration(22, "owner-egress-policy", OWNER_EGRESS_POLICY_SCHEMA)


def owner_egress_policy_matches_history(connection: sqlite3.Connection) -> bool:
    """Classifier check: one policy row equal to its latest retained history row."""
    policy = connection.execute(
        "SELECT version,share_public,share_work,share_personal,recorded_at,operation_id "
        "FROM owner_egress_policy"
    ).fetchall()
    latest = connection.execute(
        "SELECT version,share_public,share_work,share_personal,recorded_at,operation_id "
        "FROM owner_egress_policy_history ORDER BY version DESC LIMIT 1"
    ).fetchall()
    expected = [tuple(row) for row in latest] or [(0, 0, 0, 0, None, None)]
    return [tuple(row) for row in policy] == expected


def migrate_owner_egress_policy(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    """Exclusively admit schema22 with no shareable tier, leaving projections untouched."""
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
        if state.state == "current" and state.version == 22:
            return
        if state.state != "supported_old" or state.version != 21:
            raise T03Error("operation_pending")
        checkpoint("owner_egress_policy_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        if classify_local_schema(connection) != state:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:22],
            schema_version=22,
        )
        upgraded = classify_local_schema(connection)
        if upgraded.state != "current" or upgraded.version != 22:
            raise T03Error("operation_pending")
        checkpoint("owner_egress_policy_before_commit")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        admission.validate(profile)
        connection.commit()
        committed = True
        checkpoint("owner_egress_policy_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError(
                "owner egress policy migration committed"
            ) from error
        raise
    finally:
        connection.close()


__all__ = [
    "OWNER_EGRESS_POLICY_MIGRATION",
    "OWNER_EGRESS_POLICY_SCHEMA",
    "migrate_owner_egress_policy",
    "owner_egress_policy_matches_history",
]
