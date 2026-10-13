"""Exclusive schema14 floor upgrade; immutable source and authority evidence is retained."""

from collections.abc import Callable
from datetime import datetime

from open_brain_engine.storage.migrations import apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .historical_recovery import require_historical_snapshot_settled
from .local_schema import PHASE1_STATE_DATABASE, _MigrationClock, classify_local_schema
from .local_schema_catalog import LOCAL_MIGRATIONS
from .runtime_admission import HeldRuntimeAdmission
from .sharing_contracts import SharingError


def migrate_historical_compatibility(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    admission.validate(profile)
    if admission.live_peer_count:
        raise SharingError("operation_pending")
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    try:
        state = classify_local_schema(connection)
        if state.state == "current" and state.version == 14:
            return
        if state.state != "supported_old" or state.version != 13:
            raise SharingError("operation_pending")
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1 or (
            connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise SharingError("binding_mismatch")
        require_historical_snapshot_settled(connection, profile)
        checkpoint("historical_compatibility_preflight")
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:14],
            schema_version=14,
        )
        checkpoint("historical_compatibility_schema_applied")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SharingError("binding_mismatch")
        require_historical_snapshot_settled(connection, profile)
        admission.validate(profile)
        connection.execute("COMMIT")
        checkpoint("historical_compatibility_complete")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
