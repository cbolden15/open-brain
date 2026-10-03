"""Hidden-stage restoration of validated immutable historical authority.

No current consent, active CAS grant or new capture is inferred from the archive.
The caller owns clean stage creation and promotes only after complete fresh audit.
"""

from collections.abc import Callable
from pathlib import Path

from open_brain_engine.portable.v1 import PortableSnapshot
from open_brain_engine.portable.v8 import validate_historical_authority
from open_brain_engine.storage.filesystem import RootIdentity

from .historical_fence import HistoricalPendingFence
from .historical_projection import _append_historical_projection, verify_historical_projection
from .historical_recovery import _historical_transaction
from .historical_registry import HistoricalClaimRegistry, HistoricalRegistryStore
from .materializer import Materialization, materialize_portable_root
from .portable_v5_restore import V5RestoreBundle, audit_restored_v5


def restore_portable_v8_root(
    root: Path,
    *,
    snapshot: PortableSnapshot,
    expected_root_identity: RootIdentity,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Materialization:
    if snapshot.manifest["schema_version"] != 8:
        raise ValueError("v8 restore requires a validated v8 snapshot")
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
        verify_historical_projection(connection, profile, empty)
    for record in authority.records:
        # The normal forward persistence protocol remains pending until its
        # exact SQL facts really commit. An interrupted stage is never promoted.
        fence.prepare(record)
        checkpoint("historical_intent_restored")
        registry.advance(record.previous, record.proposed)
        checkpoint("historical_registry_advanced")
        with _historical_transaction(profile, lambda: None) as connection:
            _append_historical_projection(connection, profile, record)
        checkpoint("historical_sql_committed")
        fence.mark_complete(record, record.proposed)
        checkpoint("historical_completed")
    fence.assert_settled(authority.registry)
    audit_restored_v5(profile, snapshot=snapshot)
    checkpoint("historical_audited")
    return materialization
