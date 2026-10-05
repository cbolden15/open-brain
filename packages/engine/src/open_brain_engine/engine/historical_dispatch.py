"""Explicit runtime version dispatch; frozen archive parsers never use this decoder."""

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from open_brain_engine.storage.filesystem import RootIdentity

from . import historical_admission as v1
from . import historical_admission_v2 as v2
from .contracts import LocalEngineContext
from .historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalClaimRequest,
    HistoricalCopyRelationRequest,
    HistoricalReceipt,
    HistoricalRevocationRequest,
    RetainedCaptureEvidence,
)
from .historical_contracts_v2 import (
    HistoricalBaselineRequestV2,
    HistoricalClaimRequestV2,
    HistoricalCopyRelationRequestV2,
    HistoricalReceiptV2,
    HistoricalRevocationRequestV2,
    RetainedCaptureEvidenceV2,
)
from .historical_registry import HistoricalClaimRegistry
from .historical_transition import HistoricalTransition, HistoricalTransitionStore
from .historical_transition_v2 import HistoricalTransitionV2, HistoricalTransitionV2Store
from .sharing_contracts import SharingError

type BaselineRequest = HistoricalBaselineRequest | HistoricalBaselineRequestV2
type ClaimRequest = HistoricalClaimRequest | HistoricalClaimRequestV2
type RelationRequest = HistoricalCopyRelationRequest | HistoricalCopyRelationRequestV2
type RevocationRequest = HistoricalRevocationRequest | HistoricalRevocationRequestV2
type Receipt = HistoricalReceipt | HistoricalReceiptV2
type RetainedEvidence = RetainedCaptureEvidence | RetainedCaptureEvidenceV2
type Transition = HistoricalTransition | HistoricalTransitionV2
type Request = BaselineRequest | ClaimRequest | RelationRequest | RevocationRequest

BASELINE_TYPES = (HistoricalBaselineRequest, HistoricalBaselineRequestV2)
CLAIM_TYPES = (HistoricalClaimRequest, HistoricalClaimRequestV2)
RELATION_TYPES = (HistoricalCopyRelationRequest, HistoricalCopyRelationRequestV2)
REVOCATION_TYPES = (HistoricalRevocationRequest, HistoricalRevocationRequestV2)
TRANSITION_TYPES = (HistoricalTransition, HistoricalTransitionV2)


def request_kind(request: Request) -> str:
    for kind, types in (
        ("baseline", BASELINE_TYPES),
        ("claim", CLAIM_TYPES),
        ("relation", RELATION_TYPES),
        ("revocation", REVOCATION_TYPES),
    ):
        if type(request) in types:
            return kind
    raise SharingError("invalid_arguments")


def validate_historical_chain(
    registry: HistoricalClaimRegistry, records: Iterable[Transition]
) -> tuple[Transition, ...]:
    previous = HistoricalClaimRegistry.empty(registry.destination)
    result: list[Transition] = []
    seen: set[str] = set()
    for record in records:
        if (
            type(record) not in TRANSITION_TYPES
            or record.previous != previous
            or record.request.operation_id in seen
            or len(result) >= registry.generation
        ):
            raise SharingError("binding_mismatch")
        seen.add(record.request.operation_id)
        result.append(record)
        previous = record.proposed
    if previous != registry:
        raise SharingError("binding_mismatch")
    return tuple(result)


class VersionedHistoricalTransitionStore:
    def __init__(self, root: Path, root_identity: RootIdentity) -> None:
        self._v1 = HistoricalTransitionStore(root, root_identity)
        self._v2 = HistoricalTransitionV2Store(root, root_identity)

    def read_optional(self, operation_id: str) -> Transition | None:
        old, new = self._v1.read_optional(operation_id), self._v2.read_optional(operation_id)
        if old is not None and new is not None:
            raise SharingError("binding_mismatch")
        return old if old is not None else new

    def read(self, operation_id: str) -> Transition:
        result = self.read_optional(operation_id)
        if result is None:
            raise SharingError("binding_mismatch")
        return result

    def persist(self, record: Transition) -> None:
        existing = self.read_optional(record.request.operation_id)
        if existing is not None and existing != record:
            raise SharingError("binding_mismatch")
        if type(record) is HistoricalTransition:
            self._v1.persist(record)
        elif type(record) is HistoricalTransitionV2:
            self._v2.persist(record)
        else:
            raise SharingError("invalid_arguments")


def verify_retained_capture(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    evidence: RetainedEvidence,
    source_id: str,
) -> dict[str, Any]:
    if isinstance(evidence, RetainedCaptureEvidence):
        return v1.verify_retained_capture(connection, profile, evidence, source_id)
    return v2.verify_retained_capture(connection, profile, evidence, source_id)


def verify_historical_baseline_evidence(
    connection: sqlite3.Connection, profile: LocalEngineContext, request: BaselineRequest
) -> None:
    if isinstance(request, HistoricalBaselineRequest):
        v1.verify_historical_baseline_evidence(connection, profile, request)
    else:
        v2.verify_historical_baseline_evidence(connection, profile, request)


def verify_historical_relation_evidence(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    request: RelationRequest,
    baseline: BaselineRequest,
) -> None:
    if isinstance(request, HistoricalCopyRelationRequest):
        if not isinstance(baseline, HistoricalBaselineRequest):
            raise SharingError("binding_mismatch")
        v1.verify_historical_relation_evidence(connection, profile, request, baseline)
    else:
        v2.verify_historical_relation_evidence(connection, profile, request, baseline)


def require_historical_provider_consent(
    snapshot: v1.HistoricalConsentSnapshot, request: RelationRequest
) -> None:
    if isinstance(request, HistoricalCopyRelationRequest):
        v1.require_historical_provider_consent(snapshot, request)
    else:
        v2.require_historical_provider_consent(snapshot, request)


def create_historical_transition(
    *,
    request: Request,
    previous: HistoricalClaimRegistry,
    proposed: HistoricalClaimRegistry,
    receipt: Receipt,
) -> Transition:
    if request.dto_version == 1:
        return HistoricalTransition.create(
            request=cast(Any, request),
            previous=previous,
            proposed=proposed,
            receipt=cast(Any, receipt),
        )
    return HistoricalTransitionV2.create(
        request=cast(Any, request), previous=previous, proposed=proposed, receipt=cast(Any, receipt)
    )


def decode_receipt(value: dict[str, object], version: int) -> Receipt:
    return (
        HistoricalReceipt.from_value(value)
        if version == 1
        else HistoricalReceiptV2.from_value(value)
    )
