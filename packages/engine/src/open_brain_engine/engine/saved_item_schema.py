"""Saved-item identity and installation-local browser authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

SAVED_ITEM_SCHEMA = (
    """CREATE TABLE saved_item_sources (
 connector_name TEXT NOT NULL CHECK(connector_name='saved_item'),
 connection_id TEXT NOT NULL CHECK(length(connection_id)=39 AND substr(connection_id,1,7)='folder:'
   AND substr(connection_id,8) NOT GLOB '*[^0-9a-f]*'),
 resource_id TEXT NOT NULL CHECK(length(resource_id)=38 AND substr(resource_id,1,6)='items:'
   AND substr(resource_id,7) NOT GLOB '*[^0-9a-f]*'),
 PRIMARY KEY(connector_name,connection_id,resource_id)
)""",
    """CREATE TABLE browser_installation (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 generation TEXT NOT NULL CHECK(length(generation)=64 AND generation NOT GLOB '*[^0-9a-f]*')
)""",
    "INSERT INTO browser_installation VALUES(1,lower(hex(randomblob(32))))",
    """CREATE TABLE browser_grants (
 token_hash TEXT PRIMARY KEY NOT NULL,
 brain_id TEXT NOT NULL, generation TEXT NOT NULL,
 connector_name TEXT NOT NULL, connection_id TEXT NOT NULL, resource_id TEXT NOT NULL,
 operations_json TEXT NOT NULL, tiers_json TEXT NOT NULL, spaces_json TEXT NOT NULL,
 FOREIGN KEY(connector_name,connection_id,resource_id)
 REFERENCES saved_item_sources(connector_name,connection_id,resource_id)
)""",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=16),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=24)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,16,24)",
)
SAVED_ITEM_MIGRATION = _migration(24, "saved-item-sources", SAVED_ITEM_SCHEMA)


def migrate_saved_item_sources(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
    target: int = 24,
) -> None:
    """Exclusively admit schema24 with an empty saved-item registry, touching no other rows."""
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
        if state.version == target and state.state in {"current", "supported_old"}:
            return
        if state.state != "supported_old" or state.version != target - 1:
            raise T03Error("operation_pending")
        checkpoint("saved_item_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        if classify_local_schema(connection) != state:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:target],
            schema_version=target,
        )
        upgraded = classify_local_schema(connection)
        if upgraded.state not in {"current", "supported_old"} or upgraded.version != target:
            raise T03Error("operation_pending")
        checkpoint("saved_item_before_commit")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        admission.validate(profile)
        connection.commit()
        committed = True
        checkpoint("saved_item_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError("saved-item migration committed") from error
        raise
    finally:
        connection.close()


BROWSER_OPERATION_MIGRATION = _migration(
    25,
    "browser-operations",
    (
        """CREATE TABLE browser_grant_bindings (
 token_hash TEXT PRIMARY KEY REFERENCES browser_grants(token_hash) ON DELETE CASCADE,
 workflow_principal TEXT NOT NULL,
 issuer_epoch INTEGER NOT NULL,
 complete_domain INTEGER NOT NULL CHECK(complete_domain IN (0,1))
)""",
        """CREATE TABLE saved_item_local_witnesses (
 delivery_id TEXT PRIMARY KEY NOT NULL,
 connector_name TEXT NOT NULL,connection_id TEXT NOT NULL,resource_id TEXT NOT NULL,
 item_key TEXT NOT NULL CHECK(length(item_key)=32 AND item_key NOT GLOB '*[^0-9a-f]*'),
 FOREIGN KEY(connector_name,connection_id,resource_id)
 REFERENCES saved_item_sources(connector_name,connection_id,resource_id)
)""",
        """CREATE TABLE browser_operations (
 brain_id TEXT NOT NULL,workflow_principal TEXT NOT NULL,
 connector_name TEXT NOT NULL,connection_id TEXT NOT NULL,resource_id TEXT NOT NULL,
 operation_id TEXT NOT NULL,operation_kind TEXT NOT NULL CHECK(operation_kind='page.save'),
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
 state TEXT NOT NULL CHECK(state IN ('pending','accepted','refused')),delivery_id TEXT NOT NULL,
 item_key TEXT NOT NULL CHECK(length(item_key)=32 AND item_key NOT GLOB '*[^0-9a-f]*'),
 PRIMARY KEY(brain_id,workflow_principal,connector_name,connection_id,resource_id,operation_id),
 FOREIGN KEY(connector_name,connection_id,resource_id)
 REFERENCES saved_item_sources(connector_name,connection_id,resource_id)
)""",
        """CREATE INDEX browser_pending_operations ON browser_operations
 (brain_id,workflow_principal,connector_name,connection_id,resource_id) WHERE state='pending'""",
        """CREATE TABLE saved_item_admissions (
 delivery_id TEXT PRIMARY KEY NOT NULL,
 producer TEXT NOT NULL CHECK(producer IN ('browser','folder')),
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
 AND request_sha256 NOT GLOB '*[^0-9a-f]*')
)""",
        """CREATE TRIGGER browser_operation_ingest_outcome AFTER INSERT ON capture_ingestion_events
 WHEN NEW.event_kind IN ('queued','accepted','duplicate','quarantined','discarded')
 BEGIN
 UPDATE browser_operations SET state=CASE WHEN NEW.event_kind IN ('accepted','duplicate')
 THEN 'accepted' WHEN NEW.event_kind='queued' THEN 'pending' ELSE 'refused' END
 WHERE delivery_id=NEW.delivery_id;
 END""",
        """CREATE INDEX browser_operation_delivery ON browser_operations(delivery_id)""",
        *tuple(stmt.replace("24", "25") for stmt in SAVED_ITEM_SCHEMA[-3:]),
    ),
)
