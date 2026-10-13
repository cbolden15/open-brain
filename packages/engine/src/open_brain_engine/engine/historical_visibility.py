"""Fail-closed historical authority before ordinary candidate fallback."""

import sqlite3

from open_brain_engine.core.models import PrivacyDecision, PrivacyTier
from open_brain_engine.storage.filesystem import StorageError

from .contracts import LocalEngineContext
from .historical_admission import verify_historical_source_cas
from .historical_contracts import HistoricalDestination
from .historical_dispatch import (
    BASELINE_TYPES,
    RELATION_TYPES,
    verify_retained_capture,
)
from .historical_fence import HistoricalPendingFence
from .historical_projection import verify_versioned_historical_projection
from .historical_read_snapshot import HistoricalAuthorityBundle
from .historical_registry import HistoricalClaimRegistry, HistoricalRegistryStore
from .sharing_contracts import SharingError


def historical_capture_visibility(
    connection: sqlite3.Connection,
    profile: LocalEngineContext | None,
    capture_id: str,
    *,
    local_history: bool,
    provider_id: str | None,
    brain_id: str | None,
    issuer_epoch: int | None,
) -> bool | None:
    """None means verified ordinary policy, never missing historical evidence.

    Owner retained reads do not call this external/non-owner fence. On schema13,
    missing registry, pending intent or any projection loss denies all candidates
    before ranking; optional missing link state cannot restore ordinary policy.
    """
    if connection.execute("PRAGMA user_version").fetchone()[0] < 13:
        return None
    if profile is None:
        return False
    from .historical_read_snapshot import current_historical_snapshot

    scope = current_historical_snapshot(connection, profile)
    if scope is not None:
        return scope.visibility(
            capture_id,
            local_history=local_history,
            provider_id=provider_id,
            brain_id=brain_id,
            issuer_epoch=issuer_epoch,
        )
    try:
        bundle = load_historical_authority(connection, profile)
        return evaluate_historical_visibility(
            connection,
            profile,
            bundle,
            capture_id,
            local_history=local_history,
            provider_id=provider_id,
            brain_id=brain_id,
            issuer_epoch=issuer_epoch,
        )
    except SharingError, StorageError, KeyError, TypeError, ValueError, sqlite3.Error:
        return False


def read_historical_registry(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
) -> HistoricalClaimRegistry:
    identity = connection.execute(
        "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise SharingError("binding_mismatch")
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(destination)
    HistoricalPendingFence(profile.root, profile.root_identity).assert_settled(registry)
    return registry


def load_historical_authority(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
) -> HistoricalAuthorityBundle:
    registry = read_historical_registry(connection, profile)
    records = verify_versioned_historical_projection(connection, profile, registry)
    return HistoricalAuthorityBundle(registry, records)


def evaluate_historical_visibility(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    bundle: HistoricalAuthorityBundle,
    capture_id: str,
    *,
    local_history: bool,
    provider_id: str | None,
    brain_id: str | None,
    issuer_epoch: int | None,
) -> bool | None:
    registry, records = bundle.registry, bundle.records
    destination = registry.destination
    member = next((item for item in registry.memberships if item.capture_id == capture_id), None)
    if member is None:
        return None
    if member.claim_role != "historical_copy":
        return False
    link = connection.execute(
        "SELECT * FROM historical_relations WHERE copy_capture_id=?", (capture_id,)
    ).fetchone()
    if (
        link is None
        or connection.execute(
            "SELECT 1 FROM historical_revocations WHERE relation_operation_id=?",
            (link["operation_id"],),
        ).fetchone()
    ):
        return False
    requests = {record.request.operation_id: record.request for record in records}
    relation = requests[link["operation_id"]]
    baseline = requests[link["baseline_operation_id"]]
    if (
        not isinstance(relation, RELATION_TYPES)
        or not isinstance(baseline, BASELINE_TYPES)
        or not local_history
        and (
            provider_id not in relation.provider_ids
            or (brain_id, issuer_epoch) != (destination.brain_id, destination.issuer_epoch)
        )
    ):
        return False
    verify_historical_source_cas(connection, relation.source_cas)
    verify_historical_source_cas(connection, relation.copy_source_cas)
    verify_retained_capture(
        connection, profile, baseline.retained_original, baseline.source_cas.source_id
    )
    retained_copy = verify_retained_capture(
        connection, profile, relation.retained_copy, relation.copy_source_cas.source_id
    )
    privacy = PrivacyDecision.from_dict(retained_copy["privacy"])
    return privacy.tier is PrivacyTier.PUBLIC and privacy.authority.external_egress
