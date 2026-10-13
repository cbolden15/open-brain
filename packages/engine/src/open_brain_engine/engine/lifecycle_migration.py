"""Schema-11 lifecycle admission under exclusive runtime coordination."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from open_brain_engine.storage.migrations import apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .local_schema import PHASE1_STATE_DATABASE, _MigrationClock, classify_local_schema
from .local_schema_catalog import LOCAL_MIGRATIONS
from .runtime_admission import HeldRuntimeAdmission
from .t03_contracts import T03Error


def migrate_saved_lifecycle(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    admission.validate(profile)
    if admission.live_peer_count:
        raise T03Error("operation_pending")
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    try:
        state = classify_local_schema(connection)
        if state.state in {"current", "supported_old"} and state.version in {11, 12, 13, 14}:
            return
        if state.state != "supported_old" or state.version != 10:
            raise T03Error("operation_pending")
        checkpoint("saved_lifecycle_preflight")
        connection.execute("BEGIN IMMEDIATE")
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:11],
            schema_version=11,
        )
        checkpoint("saved_lifecycle_schema_applied")
        connection.execute("COMMIT")
        checkpoint("saved_lifecycle_complete")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
