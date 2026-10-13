"""Exact SQL authority projection validation against retained immutable history."""

import sqlite3
from collections.abc import Iterable
from hashlib import sha256
from threading import Lock

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .contracts import LocalEngineContext
from .historical_decode_cache import _immutable
from .historical_dispatch import (
    BASELINE_TYPES,
    CLAIM_TYPES,
    RELATION_TYPES,
    REVOCATION_TYPES,
    BaselineRequest,
    ClaimRequest,
    RelationRequest,
    Transition,
    VersionedHistoricalTransitionStore,
    validate_historical_chain,
)
from .historical_registry import HistoricalClaimRegistry
from .historical_transition import HistoricalTransition
from .sharing_contracts import SharingError

type ProjectionRows = dict[str, list[tuple[object, ...]]]
type FrozenProjectionRows = tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]

# One pure derivation, bounded by its complete canonical input representation.
# Strong references prevent object-identity reuse after decoder-cache eviction.
_PROJECTION_MAX_BYTES = 64 * 1024 * 1024
_PROJECTION_MAX_RECORDS = 1024
_projection_lock = Lock()
_projection_cache: tuple[bytes, tuple[Transition, ...], FrozenProjectionRows] | None = None


def historical_projection_rows(
    registry: HistoricalClaimRegistry, records: Iterable[Transition]
) -> ProjectionRows:
    """Reuse pure row derivation; callers still validate all current authority.

    Every hit requires exact current registry bytes and the identical immutable
    decoded records, freshly obtained by the caller. Return private mutable
    containers so consumers cannot poison later comparisons. This never stores
    a successful SQL, filesystem, pending-state or eligibility check.
    """
    global _projection_cache
    if type(registry) is not HistoricalClaimRegistry:
        raise SharingError("invalid_arguments")
    chain = tuple(records)
    registry_bytes = registry.canonical_bytes()
    with _projection_lock:
        retained = _projection_cache
        if (
            retained is not None
            and retained[0] == registry_bytes
            and len(retained[1]) == len(chain)
            and all(old is new for old, new in zip(retained[1], chain, strict=True))
        ):
            return {table: list(rows) for table, rows in retained[2]}
    rows = _derive_historical_projection_rows(registry, chain)
    if (
        len(chain) <= _PROJECTION_MAX_RECORDS
        and _immutable(registry)
        and all(_immutable(record) for record in chain)
    ):
        size = len(registry_bytes)
        for record in chain:
            size += len(record.canonical_bytes())
            if size > _PROJECTION_MAX_BYTES:
                break
        if size <= _PROJECTION_MAX_BYTES:
            frozen = tuple((table, tuple(values)) for table, values in rows.items())
            if _immutable(frozen):
                with _projection_lock:
                    _projection_cache = (registry_bytes, chain, frozen)
    return rows


def _derive_historical_projection_rows(
    registry: HistoricalClaimRegistry, records: Iterable[Transition]
) -> ProjectionRows:
    """Derive all facts, including optional links/revocations, from a full chain.

    No current eligibility or accepted-content integrity is inferred here. An
    owner admission task must validate retained bodies, CAS and authority first.
    """
    chain = validate_historical_chain(registry, records)
    rows: ProjectionRows = {
        table: []
        for table in (
            "historical_operations",
            "historical_claims",
            "historical_baselines",
            "historical_relations",
            "historical_revocations",
        )
    }
    baselines: dict[str, BaselineRequest] = {}
    claims: dict[str, ClaimRequest] = {}
    relations: dict[str, RelationRequest] = {}
    linked: set[str] = set()
    revoked: set[str] = set()
    baseline_sources: set[str] = set()
    baseline_namespaces: set[str] = set()
    for record in chain:
        request, receipt = record.request, record.receipt
        operation = request.operation_id
        if isinstance(request, BASELINE_TYPES):
            kind = "baseline"
            observed = request.observed_delivery
            namespace = sha256(observed.submission.namespace_bytes()).hexdigest()
            if request.source_cas.source_id in baseline_sources or namespace in baseline_namespaces:
                raise SharingError("binding_mismatch")
            baseline_sources.add(request.source_cas.source_id)
            baseline_namespaces.add(namespace)
            baselines[operation] = request
            rows["historical_baselines"].append(
                (
                    operation,
                    request.source_cas.source_id,
                    request.retained_original.capture_id,
                    namespace,
                    observed.submission.revision_key,
                    observed.envelope_sha256,
                    observed.custody_bytes(),
                )
            )
        elif isinstance(request, CLAIM_TYPES):
            kind = "claim"
            claims[operation] = request
        elif isinstance(request, RELATION_TYPES):
            kind = "relation"
            baseline = baselines.get(request.baseline_operation_id)
            claim = claims.get(request.copy_claim_operation_id)
            if (
                baseline is None
                or claim is None
                or request.expected_relation_version != 0
                or claim.claim_role != "historical_copy"
                or claim.retained_capture != request.retained_copy
                or baseline.source_cas.source_id != request.source_cas.source_id
                or baseline.retained_original.capture_id != receipt.original_capture_id
                or claim.source_cas.source_id != request.source_cas.source_id
                or claim.capture_source_cas.source_id != request.copy_source_cas.source_id
                or request.retained_copy.capture_id in linked
            ):
                raise SharingError("binding_mismatch")
            linked.add(request.retained_copy.capture_id)
            relations[operation] = request
            rows["historical_relations"].append(
                (
                    operation,
                    request.baseline_operation_id,
                    request.copy_claim_operation_id,
                    receipt.original_capture_id,
                    request.retained_copy.capture_id,
                    request.source_cas.source_id,
                    request.copy_source_cas.source_id,
                    request.approval_evidence_sha256,
                    canonical(list(request.provider_ids)).decode(),
                    1,
                )
            )
        elif isinstance(request, REVOCATION_TYPES):
            kind = "revocation"
            relation = relations.get(request.relation_operation_id)
            if (
                relation is None
                or request.relation_operation_id in revoked
                or request.expected_relation_version != 1
                or request.source_cas.source_id != relation.source_cas.source_id
                or receipt.original_capture_id != relation.source_cas.expected_head
                or receipt.copy_capture_id != relation.retained_copy.capture_id
            ):
                raise SharingError("binding_mismatch")
            revoked.add(request.relation_operation_id)
            rows["historical_revocations"].append((operation, request.relation_operation_id, 2))
        else:
            raise SharingError("binding_mismatch")
        rows["historical_operations"].append(
            (
                operation,
                kind,
                request.request_sha256,
                request.canonical_bytes(),
                canonical(receipt.value()),
                record.transition_sha256,
                record.proposed.generation,
            )
        )
        added = set(record.proposed.memberships) - set(record.previous.memberships)
        for member in added:
            rows["historical_claims"].append(
                (
                    member.capture_id,
                    member.source_id,
                    member.capture_source_id,
                    member.claim_role,
                    operation,
                )
            )
    for table in rows:
        rows[table].sort(key=lambda row: str(row[0]))
    return rows


def verify_versioned_historical_projection(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    registry: HistoricalClaimRegistry,
) -> tuple[Transition, ...]:
    """Require a snapshot, exact identity, complete chain and every SQL fact.

    The caller must also assert the independent pending fence is settled before
    external output. In particular, absence of an optional revocation row can
    never restore eligibility: a missing row differs from this retained history.
    """
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
    store = VersionedHistoricalTransitionStore(profile.root, profile.root_identity)
    records = tuple(
        store.read(row[0])
        for row in connection.execute(
            "SELECT operation_id FROM historical_operations ORDER BY registry_generation"
        )
    )
    expected = historical_projection_rows(registry, records)
    for table, rows in expected.items():
        actual = [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
        if actual != rows:
            raise SharingError("binding_mismatch")
    return records


def verify_historical_projection(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    registry: HistoricalClaimRegistry,
) -> tuple[HistoricalTransition, ...]:
    """Keep the frozen Portable8 consumer closed over V1 transitions."""
    records = verify_versioned_historical_projection(connection, profile, registry)
    if any(type(record) is not HistoricalTransition for record in records):
        raise SharingError("binding_mismatch")
    return tuple(record for record in records if isinstance(record, HistoricalTransition))


def _append_historical_projection(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    record: Transition,
) -> None:
    """Internal projection step after admitted immutable intent/pending fence.

    The owner recovery task owns exclusive admission, writer lock, evidence/CAS
    validation, registry advancement, commit and completion. This helper grants
    no authority and must not be exposed as a mutation task accepting JSON.
    """
    previous = verify_versioned_historical_projection(connection, profile, record.previous)
    store = VersionedHistoricalTransitionStore(profile.root, profile.root_identity)
    if store.read(record.request.operation_id) != record:
        raise SharingError("binding_mismatch")
    old = historical_projection_rows(record.previous, previous)
    proposed = historical_projection_rows(record.proposed, (*previous, record))
    for table, rows in proposed.items():
        before = set(old[table])
        for row in rows:
            if row not in before:
                placeholders = ",".join("?" for _ in row)
                connection.execute(f"INSERT INTO {table} VALUES({placeholders})", row)
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
    verify_versioned_historical_projection(connection, profile, record.proposed)
