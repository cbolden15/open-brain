"""Schema21 owner-local embedding policy and explicit runtime16 cutover."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import SchemaError, _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

_COMPONENT = "length({0})=64 AND {0} NOT GLOB '*[^0-9a-f]*'"

EMBEDDING_POLICY_SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE embedding_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 brain_id TEXT NOT NULL UNIQUE, issuer_epoch INTEGER NOT NULL CHECK(issuer_epoch>=1),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 component_id TEXT CHECK((enabled=0 AND component_id IS NULL)
  OR (enabled=1 AND component_id IS NOT NULL AND {_COMPONENT.format("component_id")})),
 consent_version INTEGER NOT NULL CHECK(consent_version>=0),
 policy_generation INTEGER NOT NULL CHECK(policy_generation>=0),
 changed_at TEXT
)""",
    f"""CREATE TABLE embedding_policy_history (
 version INTEGER PRIMARY KEY CHECK(version>=1),
 action TEXT NOT NULL CHECK(action IN ('enable','disable')),
 component_id TEXT CHECK((action='disable' AND component_id IS NULL)
  OR (action='enable' AND component_id IS NOT NULL AND {_COMPONENT.format("component_id")})),
 changed_at TEXT NOT NULL
)""",
    """CREATE TRIGGER embedding_policy_update_guard BEFORE UPDATE ON embedding_policy
WHEN OLD.singleton IS NOT NEW.singleton OR OLD.brain_id IS NOT NEW.brain_id
 OR OLD.issuer_epoch IS NOT NEW.issuer_epoch
 OR NEW.consent_version <= OLD.consent_version
 OR NEW.policy_generation <= OLD.policy_generation
BEGIN SELECT RAISE(ABORT, 'embedding policy is monotone'); END""",
    """CREATE TRIGGER embedding_policy_delete_immutable BEFORE DELETE ON embedding_policy
BEGIN SELECT RAISE(ABORT, 'embedding policy is retained'); END""",
    """CREATE TRIGGER embedding_policy_history_update_immutable
BEFORE UPDATE ON embedding_policy_history
BEGIN SELECT RAISE(ABORT, 'embedding policy history is immutable'); END""",
    """CREATE TRIGGER embedding_policy_history_delete_immutable
BEFORE DELETE ON embedding_policy_history
BEGIN SELECT RAISE(ABORT, 'embedding policy history is immutable'); END""",
    # An upgraded Brain starts disabled; fresh creation inserts after its identity exists.
    "INSERT INTO embedding_policy SELECT 1,brain_id,issuer_epoch,0,NULL,0,0,NULL "
    "FROM brain_identity",
    "UPDATE engine_generations SET projection_policy_version=projection_policy_version+1",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=16),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=21)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,16,21)",
)
EMBEDDING_POLICY_MIGRATION = _migration(21, "embedding-policy", EMBEDDING_POLICY_SCHEMA)


def initialize_embedding_policy(connection: sqlite3.Connection) -> None:
    """Finish fresh creation with a disabled policy bound to the durable Brain."""
    if not connection.in_transaction:
        raise SchemaError("embedding policy requires a schema transaction")
    identities = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchall()
    if len(identities) != 1:
        raise SchemaError("embedding policy requires a durable Brain identity")
    if connection.execute("SELECT 1 FROM embedding_policy").fetchone() is None:
        connection.execute(
            "INSERT INTO embedding_policy VALUES(1,?,?,0,NULL,0,0,NULL)", tuple(identities[0])
        )


def embedding_policy_matches_identity(connection: sqlite3.Connection) -> bool:
    """Classifier check: exactly one policy row bound to this Brain.

    An issuer-epoch mismatch is not schema damage; ``read_policy`` reports such a
    policy as disabled because consent was given under a different issuer.
    """
    policy = connection.execute("SELECT brain_id FROM embedding_policy").fetchall()
    identity = connection.execute("SELECT brain_id FROM brain_identity").fetchall()
    return len(identity) == 1 and [tuple(row) for row in policy] == [tuple(identity[0])]


def migrate_embedding_policy(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    """Exclusively admit schema21 with local embedding disabled."""
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
        if state.state in {"current", "supported_old"} and state.version in {21, 22}:
            return
        if state.state != "supported_old" or state.version != 20:
            raise T03Error("operation_pending")
        checkpoint("embedding_policy_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        if classify_local_schema(connection) != state:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:21],
            schema_version=21,
        )
        upgraded = classify_local_schema(connection)
        if upgraded.state not in {"current", "supported_old"} or upgraded.version != 21:
            raise T03Error("operation_pending")
        checkpoint("embedding_policy_before_commit")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        admission.validate(profile)
        connection.commit()
        committed = True
        checkpoint("embedding_policy_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError(
                "embedding policy migration committed"
            ) from error
        raise
    finally:
        connection.close()
