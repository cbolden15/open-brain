"""Immutable forward intent for fenced historical-authority mutations.

The typed request determines SQL intent. No caller-supplied SQL or authority is
stored. This record is not a completed receipt until its exact projections are
durably committed and verified by the owner admission/recovery task.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import RootIdentity, atomic_write_new, read_confined

from .historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalClaimRequest,
    HistoricalCopyRelationRequest,
    HistoricalReceipt,
    HistoricalRevocationRequest,
    _unique_object,
)
from .historical_decode_cache import decode_historical_bytes
from .historical_registry import HistoricalClaimMembership, HistoricalClaimRegistry
from .sharing_contracts import _OPERATION, SharingError, _digest, _text, _version

type HistoricalRequest = (
    HistoricalBaselineRequest
    | HistoricalClaimRequest
    | HistoricalCopyRelationRequest
    | HistoricalRevocationRequest
)
_KINDS: dict[str, Any] = {
    "baseline": HistoricalBaselineRequest,
    "claim": HistoricalClaimRequest,
    "relation": HistoricalCopyRelationRequest,
    "revocation": HistoricalRevocationRequest,
}
_MAX_BYTES = 9 * 1024 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalTransition:
    request: HistoricalRequest
    previous: HistoricalClaimRegistry
    proposed: HistoricalClaimRegistry
    receipt: HistoricalReceipt
    transition_sha256: str
    dto_version: int = 1

    @classmethod
    def create(
        cls,
        *,
        request: HistoricalRequest,
        previous: HistoricalClaimRegistry,
        proposed: HistoricalClaimRegistry,
        receipt: HistoricalReceipt,
    ) -> HistoricalTransition:
        body = _body(request, previous, proposed, receipt)
        return cls(
            request=request,
            previous=previous,
            proposed=proposed,
            receipt=receipt,
            transition_sha256=_digest_body(body),
        )

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 1:
            raise SharingError("invalid_arguments")
        _digest(self.transition_sha256)
        request, previous, proposed, receipt = (
            self.request,
            self.previous,
            self.proposed,
            self.receipt,
        )
        if (
            type(request) not in _KINDS.values()
            or type(previous) is not HistoricalClaimRegistry
            or type(proposed) is not HistoricalClaimRegistry
            or type(receipt) is not HistoricalReceipt
        ):
            raise SharingError("invalid_arguments")
        if (
            previous.destination != request.destination
            or proposed.destination != request.destination
            or receipt.destination != request.destination
            or previous.generation != request.expected_claim_generation
            or proposed.generation != previous.generation + 1
            or receipt.claim_generation != proposed.generation
            or receipt.operation_id != request.operation_id
            or receipt.request_sha256 != request.request_sha256
            or receipt.source_id != request.source_cas.source_id
        ):
            raise SharingError("invalid_arguments")
        expected_members = set(previous.memberships)
        if type(request) is HistoricalBaselineRequest:
            expected_members.add(
                HistoricalClaimMembership(
                    capture_id=request.retained_original.capture_id,
                    source_id=request.source_cas.source_id,
                    capture_source_id=request.source_cas.source_id,
                    claim_role="baseline_original",
                )
            )
            outcome, copy_id, relation_version = "baseline_adopted", None, None
        elif type(request) is HistoricalClaimRequest:
            expected_members.add(
                HistoricalClaimMembership(
                    capture_id=request.retained_capture.capture_id,
                    source_id=request.source_cas.source_id,
                    capture_source_id=request.capture_source_cas.source_id,
                    claim_role=request.claim_role,
                )
            )
            outcome = "denied_claim_recorded"
            copy_id = (
                request.retained_capture.capture_id
                if request.claim_role == "historical_copy"
                else None
            )
            relation_version = None
        elif type(request) is HistoricalCopyRelationRequest:
            outcome, copy_id = "historical_copy_linked", request.retained_copy.capture_id
            relation_version = request.expected_relation_version + 1
        elif type(request) is HistoricalRevocationRequest:
            outcome, copy_id = "historical_copy_revoked", receipt.copy_capture_id
            relation_version = request.expected_relation_version + 1
        else:
            raise SharingError("invalid_arguments")
        if (
            tuple(sorted(expected_members, key=lambda item: item.capture_id))
            != proposed.memberships
            or receipt.outcome != outcome
            or receipt.copy_capture_id != copy_id
            or receipt.relation_version != relation_version
        ):
            raise SharingError("invalid_arguments")
        if type(request) is not HistoricalRevocationRequest:
            original_id = (
                request.retained_capture.capture_id
                if type(request) is HistoricalClaimRequest
                and request.claim_role == "baseline_original"
                else request.source_cas.expected_head
            )
            if receipt.original_capture_id != original_id:
                raise SharingError("invalid_arguments")
        if type(request) in (HistoricalCopyRelationRequest, HistoricalRevocationRequest):
            original = HistoricalClaimMembership(
                capture_id=receipt.original_capture_id,
                source_id=request.source_cas.source_id,
                capture_source_id=request.source_cas.source_id,
                claim_role="baseline_original",
            )
            copy_source_id = (
                request.copy_source_cas.source_id
                if type(request) is HistoricalCopyRelationRequest
                else None
            )
            if original not in previous.memberships or not any(
                member.capture_id == copy_id
                and member.source_id == request.source_cas.source_id
                and member.claim_role == "historical_copy"
                and (copy_source_id is None or member.capture_source_id == copy_source_id)
                for member in previous.memberships
            ):
                raise SharingError("invalid_arguments")
        if _digest_body(_body(request, previous, proposed, receipt)) != self.transition_sha256:
            raise SharingError("invalid_arguments")
        if len(self.canonical_bytes()) > _MAX_BYTES:
            raise SharingError("invalid_arguments")

    def canonical_bytes(self) -> bytes:
        return portable_canonical_json_bytes(
            dict(
                _body(self.request, self.previous, self.proposed, self.receipt),
                transition_sha256=self.transition_sha256,
            )
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> HistoricalTransition:
        if type(raw) is not bytes or not raw or len(raw) > _MAX_BYTES:
            raise SharingError("invalid_arguments")
        try:
            value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_object)
            if (
                type(value) is not dict
                or set(value)
                != {
                    "dto_version",
                    "kind",
                    "request",
                    "previous",
                    "proposed",
                    "receipt",
                    "transition_sha256",
                }
                or type(value["kind"]) is not str
                or value["kind"] not in _KINDS
            ):
                raise SharingError("invalid_arguments")
            result = cls(
                dto_version=value["dto_version"],
                request=_KINDS[value["kind"]].from_value(value["request"]),
                previous=HistoricalClaimRegistry.from_bytes(
                    portable_canonical_json_bytes(value["previous"])
                ),
                proposed=HistoricalClaimRegistry.from_bytes(
                    portable_canonical_json_bytes(value["proposed"])
                ),
                receipt=HistoricalReceipt.from_value(value["receipt"]),
                transition_sha256=value["transition_sha256"],
            )
            if result.canonical_bytes() != raw:
                raise SharingError("invalid_arguments")
            return result
        except TypeError, ValueError, UnicodeError:
            raise SharingError("invalid_arguments") from None


def _body(
    request: HistoricalRequest,
    previous: HistoricalClaimRegistry,
    proposed: HistoricalClaimRegistry,
    receipt: HistoricalReceipt,
) -> dict[str, object]:
    kinds = [kind for kind, cls in _KINDS.items() if type(request) is cls]
    if (
        not kinds
        or type(previous) is not HistoricalClaimRegistry
        or type(proposed) is not HistoricalClaimRegistry
        or type(receipt) is not HistoricalReceipt
    ):
        raise SharingError("invalid_arguments")
    return {
        "dto_version": 1,
        "kind": kinds[0],
        "request": request.value(),
        "previous": json.loads(previous.canonical_bytes()),
        "proposed": json.loads(proposed.canonical_bytes()),
        "receipt": receipt.value(),
    }


def _digest_body(body: dict[str, object]) -> str:
    return sha256(
        b"open-brain-historical-transition.v1\0" + portable_canonical_json_bytes(body)
    ).hexdigest()


def validate_historical_chain(
    registry: HistoricalClaimRegistry,
    records: Iterable[HistoricalTransition],
) -> tuple[HistoricalTransition, ...]:
    """Require every generation, including membership-neutral revocations.

    Registry equality alone cannot detect a missing revocation because linking
    and revoking keep the same memberships. Callers must also compare each exact
    record with its SQL projections and enforce the persistent pending fence.
    This validates history, not current eligibility or terminal durability.
    """
    if type(registry) is not HistoricalClaimRegistry:
        raise SharingError("invalid_arguments")
    previous = HistoricalClaimRegistry.empty(registry.destination)
    result: list[HistoricalTransition] = []
    seen: set[str] = set()
    for record in records:
        if (
            type(record) is not HistoricalTransition
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


def _path(operation_id: str) -> str:
    _text(operation_id, pattern=_OPERATION)
    key = sha256(b"open-brain-historical-operation.v1\0" + operation_id.encode()).hexdigest()
    return ".open-brain/historical-authority/historical-transitions.v1/" + key + ".json"


class HistoricalTransitionStore:
    def __init__(self, root: Path, root_identity: RootIdentity) -> None:
        self._root, self._root_identity = root, root_identity

    def persist(self, record: HistoricalTransition) -> None:
        if type(record) is not HistoricalTransition:
            raise SharingError("invalid_arguments")
        atomic_write_new(
            root=self._root,
            relative=_path(record.request.operation_id),
            data=record.canonical_bytes(),
            expected_root_identity=self._root_identity,
        )

    def read(self, operation_id: str) -> HistoricalTransition:
        record = self.read_optional(operation_id)
        if record is None:
            raise SharingError("binding_mismatch")
        return record

    def read_optional(self, operation_id: str) -> HistoricalTransition | None:
        """Only actual absence is optional; malformed retained intent still refuses."""
        raw = read_confined(
            root=self._root,
            relative=_path(operation_id),
            maximum_bytes=_MAX_BYTES,
            expected_root_identity=self._root_identity,
        )
        if raw is None:
            return None
        record = decode_historical_bytes(raw, HistoricalTransition.from_bytes)
        if record.request.operation_id != operation_id:
            raise SharingError("binding_mismatch")
        return record
