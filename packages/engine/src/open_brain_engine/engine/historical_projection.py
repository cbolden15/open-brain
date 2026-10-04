"""Exact SQL authority projection validation against retained immutable history."""

import sqlite3
from collections.abc import Iterable
from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .contracts import LocalEngineContext
from .historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalClaimRequest,
    HistoricalCopyRelationRequest,
    HistoricalRevocationRequest,
)
from .historical_registry import HistoricalClaimRegistry
from .historical_transition import (
    HistoricalTransition,
    HistoricalTransitionStore,
    validate_historical_chain,
)
from .sharing_contracts import SharingError

type ProjectionRows = dict[str, list[tuple[object, ...]]]


def historical_projection_rows(
    registry: HistoricalClaimRegistry, records: Iterable[HistoricalTransition]
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
    baselines: dict[str, HistoricalBaselineRequest] = {}
    claims: dict[str, HistoricalClaimRequest] = {}
    relations: dict[str, HistoricalCopyRelationRequest] = {}
    linked: set[str] = set()
    revoked: set[str] = set()
    baseline_sources: set[str] = set()
    baseline_namespaces: set[str] = set()
    for record in chain:
        request, receipt = record.request, record.receipt
        operation = request.operation_id
        if type(request) is HistoricalBaselineRequest:
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
        elif type(request) is HistoricalClaimRequest:
            kind = "claim"
            claims[operation] = request
        elif type(request) is HistoricalCopyRelationRequest:
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
        elif type(request) is HistoricalRevocationRequest:
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


def verify_historical_projection(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    registry: HistoricalClaimRegistry,
) -> tuple[HistoricalTransition, ...]:
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
    store = HistoricalTransitionStore(profile.root, profile.root_identity)
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


def _append_historical_projection(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    record: HistoricalTransition,
) -> None:
    """Internal projection step after admitted immutable intent/pending fence.

    The owner recovery task owns exclusive admission, writer lock, evidence/CAS
    validation, registry advancement, commit and completion. This helper grants
    no authority and must not be exposed as a mutation task accepting JSON.
    """
    previous = verify_historical_projection(connection, profile, record.previous)
    store = HistoricalTransitionStore(profile.root, profile.root_identity)
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
    verify_historical_projection(connection, profile, record.proposed)
