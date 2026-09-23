"""Schema-10 durable-ingestion journal cutover under exclusive runtime admission."""

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

INGESTION_JOURNAL_SCHEMA_VERSION = 10
PREVIOUS_SCHEMA_VERSION = 9


def migrate_ingestion_journal(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    """Install the empty Brain-owned ingress journal atomically.

    There is no payload backfill: schema nine has no acknowledged ingress
    custody. Running this stage twice verifies the completed schema instead of
    reopening an ordinary runtime migration path.
    """
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
        if state.state == "current" and state.version == INGESTION_JOURNAL_SCHEMA_VERSION:
            return
        if state.state != "supported_old" or state.version != PREVIOUS_SCHEMA_VERSION:
            raise T03Error("operation_pending")
        checkpoint("ingestion_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        state = classify_local_schema(connection)
        if state.state != "supported_old" or state.version != PREVIOUS_SCHEMA_VERSION:
            raise T03Error("operation_pending")
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:INGESTION_JOURNAL_SCHEMA_VERSION],
            schema_version=INGESTION_JOURNAL_SCHEMA_VERSION,
        )
        if (
            connection.execute("SELECT 1 FROM capture_ingestion_items LIMIT 1").fetchone()
            is not None
        ):
            raise T03Error("operation_pending")
        checkpoint("ingestion_schema_applied")
        connection.execute("COMMIT")
        checkpoint("ingestion_complete")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


__all__ = ["INGESTION_JOURNAL_SCHEMA_VERSION", "migrate_ingestion_journal"]
