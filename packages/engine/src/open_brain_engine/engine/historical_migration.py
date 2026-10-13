"""Exclusive schema13 upgrade and explicit empty historical denial state."""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from open_brain_engine.storage.migrations import apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .historical_contracts import HistoricalDestination
from .historical_fence import HistoricalPendingFence
from .historical_registry import HistoricalRegistryStore
from .local_schema import PHASE1_STATE_DATABASE, _MigrationClock, classify_local_schema
from .local_schema_catalog import LOCAL_MIGRATIONS
from .runtime_admission import HeldRuntimeAdmission
from .sharing_contracts import SharingError


def initialize_historical_state(
    connection: sqlite3.Connection, profile: LocalEngineContext
) -> None:
    """Only for genuinely new schema13 tables, inside bootstrap/migration."""
    if not connection.in_transaction:
        raise SharingError("operation_pending")
    identity = connection.execute(
        "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if (
        identity is None
        or connection.execute("SELECT count(*) FROM historical_registry_state").fetchone()[0]
    ):
        raise SharingError("binding_mismatch")
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    registry = HistoricalRegistryStore(profile.root, profile.root_identity).initialize_empty(
        destination
    )
    HistoricalPendingFence(profile.root, profile.root_identity).initialize_empty(registry)
    connection.execute(
        "INSERT INTO historical_registry_state VALUES(1,?,?,0,?)",
        (destination.brain_id, destination.issuer_epoch, registry.registry_sha256),
    )


def migrate_historical(
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
        if state.state == "current" and state.version == 13:
            return
        if state.state != "supported_old" or state.version != 12:
            raise SharingError("operation_pending")
        checkpoint("historical_preflight")
        connection.execute("BEGIN IMMEDIATE")
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:13],
            schema_version=13,
        )
        checkpoint("historical_schema_applied")
        initialize_historical_state(connection, profile)
        checkpoint("historical_files_initialized")
        connection.execute("COMMIT")
        checkpoint("historical_complete")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
