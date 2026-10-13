"""Hidden-stage restoration of validated immutable historical authority.

No current consent, active CAS grant or new capture is inferred from the archive.
The caller owns clean stage creation and promotes only after complete fresh audit.
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from open_brain_engine.portable.v1 import PortableSnapshot
from open_brain_engine.portable.v9 import validate_historical_authority
from open_brain_engine.storage.filesystem import RootIdentity

from .contracts import LocalEngineContext
from .historical_dispatch import Transition, VersionedHistoricalTransitionStore
from .historical_fence import HistoricalPendingFence
from .historical_projection import (
    ProjectionRows,
    historical_projection_rows,
    verify_versioned_historical_projection,
)
from .historical_recovery import _historical_transaction
from .historical_registry import HistoricalClaimRegistry, HistoricalRegistryStore
from .materializer import Materialization, materialize_portable_root
from .portable_v5_restore import V5RestoreBundle, audit_restored_v5
from .sharing_contracts import SharingError


def _assert_restored_projection(
    connection: sqlite3.Connection,
    registry: HistoricalClaimRegistry,
    expected: ProjectionRows,
) -> None:
    if not connection.in_transaction:
        raise SharingError("operation_pending")
    identity = connection.execute(
        "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if identity is None or tuple(identity) != (
        registry.destination.brain_id,
        registry.destination.issuer_epoch,
    ):
        raise SharingError("binding_mismatch")
    state = connection.execute("SELECT * FROM historical_registry_state").fetchall()
    if [tuple(row) for row in state] != [
        (
            1,
            registry.destination.brain_id,
            registry.destination.issuer_epoch,
            registry.generation,
            registry.registry_sha256,
        )
    ]:
        raise SharingError("binding_mismatch")
    for table, rows in expected.items():
        actual = [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
        if actual != rows:
            raise SharingError("binding_mismatch")


def _append_restored_projection(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    record: Transition,
    expected: ProjectionRows,
    delta: ProjectionRows,
) -> None:
    # Only the hidden clean-restore stage uses this prevalidated chain projection.
    # Live admission retains the full independently reopened history checks.
    _assert_restored_projection(connection, record.previous, expected)
    store = VersionedHistoricalTransitionStore(profile.root, profile.root_identity)
    if store.read(record.request.operation_id) != record:
        raise SharingError("binding_mismatch")
    for table, rows in delta.items():
        for row in rows:
            placeholders = ",".join("?" for _ in row)
            connection.execute(f"INSERT INTO {table} VALUES({placeholders})", row)
        expected[table].extend(rows)
        expected[table].sort(key=lambda row: str(row[0]))
    changed = connection.execute(
        "UPDATE historical_registry_state SET generation=?,registry_sha256=? "
        "WHERE singleton=1 AND generation=? AND registry_sha256=?",
        (
            record.proposed.generation,
            record.proposed.registry_sha256,
            record.previous.generation,
            record.previous.registry_sha256,
        ),
    )
    if changed.rowcount != 1:
        raise SharingError("binding_mismatch")
    _assert_restored_projection(connection, record.proposed, expected)


def restore_portable_v9_root(
    root: Path,
    *,
    snapshot: PortableSnapshot,
    expected_root_identity: RootIdentity,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Materialization:
    if snapshot.manifest["schema_version"] != 9:
        raise ValueError("v9 restore requires a validated v9 snapshot")
    authority = validate_historical_authority(snapshot.files)
    bundle = V5RestoreBundle._decode_common(snapshot, checkpoint=checkpoint)
    materialization = materialize_portable_root(
        root,
        snapshot=snapshot,
        expected_root_identity=expected_root_identity,
        _v5_restore=bundle,
    )
    profile = materialization.profile
    registry = HistoricalRegistryStore(profile.root, profile.root_identity)
    fence = HistoricalPendingFence(profile.root, profile.root_identity)
    empty = HistoricalClaimRegistry.empty(authority.registry.destination)
    if registry.read(empty.destination) != empty:
        raise ValueError("historical restore requires a new empty authority stage")
    fence.assert_settled(empty)
    with _historical_transaction(profile, lambda: None) as connection:
        verify_versioned_historical_projection(connection, profile, empty)
    # Validate every cross-operation relation once, then index its exact SQL rows.
    # Re-decoding the complete persisted prefix twice per append makes a restore
    # repeatedly normalize all earlier registry memberships.
    rows = historical_projection_rows(authority.registry, authority.records)
    deltas: dict[str, ProjectionRows] = {
        record.request.operation_id: {table: [] for table in rows} for record in authority.records
    }
    expected: ProjectionRows = {table: [] for table in rows}
    for table, values in rows.items():
        for row in values:
            operation = row[-1] if table == "historical_claims" else row[0]
            if not isinstance(operation, str):
                raise SharingError("binding_mismatch")
            deltas[operation][table].append(row)
    for record in authority.records:
        # The normal forward persistence protocol remains pending until its
        # exact SQL facts really commit. An interrupted stage is never promoted.
        fence.prepare(record)
        checkpoint("historical_intent_restored")
        registry.advance(record.previous, record.proposed)
        checkpoint("historical_registry_advanced")
        with _historical_transaction(profile, lambda: None) as connection:
            _append_restored_projection(
                connection, profile, record, expected, deltas[record.request.operation_id]
            )
        checkpoint("historical_sql_committed")
        fence.mark_complete(record, record.proposed)
        checkpoint("historical_completed")
    fence.assert_settled(authority.registry)
    with _historical_transaction(profile, lambda: None) as connection:
        verify_versioned_historical_projection(connection, profile, authority.registry)
    audit_restored_v5(profile, snapshot=snapshot)
    checkpoint("historical_audited")
    return materialization
