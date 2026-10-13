"""Version-two historical evidence; independent stored commitments and closed lineage.

These types carry evidence, not permission. Runtime owner authorization, stored
identity verification and fenced CAS remain the admitting operation's duty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Self, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .historical_contracts import (
    HistoricalDestination,
    HistoricalSourceCAS,
    _ClosedComponent,
)
from .historical_observation_v2 import MAX_HISTORICAL_V2_BYTES, decode_historical_observation_v2
from .sharing_contracts import (
    _OPERATION,
    _PROVIDER,
    SharingError,
    _digest,
    _identity,
    _text,
    _version,
)
from .source_intake import SourceRevisionObservedDelivery


@dataclass(frozen=True, slots=True, kw_only=True)
class RetainedCaptureEvidenceV2(_ClosedComponent):
    capture_id: str
    source_sha256: str
    retained_delivery_id: str
    capture_request_sha256: str
    revision_request_sha256: str | None
    alias_evidence_sha256: str | None
    correspondence_scheme: str
    privacy_sha256: str
    accepted_receipt_id: str | None = None

    def __post_init__(self) -> None:
        _identity(self.capture_id, "capture_")
        _text(self.retained_delivery_id)
        if self.correspondence_scheme not in (
            "direct_revision_alias",
            "schema_seven_alias",
            "portable_import_projection_v1",
        ):
            raise SharingError("invalid_arguments")
        if self.correspondence_scheme == "portable_import_projection_v1":
            if self.revision_request_sha256 is not None or self.alias_evidence_sha256 is not None:
                raise SharingError("invalid_arguments")
            _identity(self.accepted_receipt_id, "receipt_")
        else:
            if self.accepted_receipt_id is not None:
                raise SharingError("invalid_arguments")
            _digest(self.revision_request_sha256)
            _digest(self.alias_evidence_sha256)
        for digest in (
            self.source_sha256,
            self.capture_request_sha256,
            self.privacy_sha256,
        ):
            _digest(digest)


def _consistent_source_witnesses(
    original: HistoricalSourceCAS, retained: HistoricalSourceCAS
) -> None:
    if original.expected_control_epoch != retained.expected_control_epoch:
        raise SharingError("invalid_arguments")
    if original.source_id == retained.source_id and original != retained:
        raise SharingError("invalid_arguments")


class _HistoricalRequest(_ClosedComponent):
    __slots__ = ()
    operation_id: str
    destination: HistoricalDestination
    source_cas: HistoricalSourceCAS
    expected_claim_generation: int
    dto_version: int

    def _validate(self) -> None:
        if (
            _version(self.dto_version, minimum=1) != 2
            or type(self.destination) is not HistoricalDestination
            or type(self.source_cas) is not HistoricalSourceCAS
        ):
            raise SharingError("invalid_arguments")
        _text(self.operation_id, pattern=_OPERATION)
        _version(self.expected_claim_generation)

    def value(self) -> dict[str, object]:
        value = super().value()
        for key, item in value.items():
            if isinstance(item, _ClosedComponent):
                value[key] = item.value()
            elif isinstance(item, tuple):
                value[key] = list(item)
            elif type(item) is SourceRevisionObservedDelivery:
                value[key] = json.loads(item.custody_bytes())
        return value

    @classmethod
    def from_value(cls, value: object) -> Self:
        names = {field.name for field in fields(cast(Any, cls))}
        if type(value) is not dict or set(value) != names:
            raise SharingError("invalid_arguments")
        decoded = dict(value)
        components: dict[str, type[_ClosedComponent]] = {
            "destination": HistoricalDestination,
            "source_cas": HistoricalSourceCAS,
            "capture_source_cas": HistoricalSourceCAS,
            "copy_source_cas": HistoricalSourceCAS,
            "retained_capture": RetainedCaptureEvidenceV2,
            "retained_copy": RetainedCaptureEvidenceV2,
            "retained_original": RetainedCaptureEvidenceV2,
        }
        for key, component in components.items():
            if key in decoded:
                decoded[key] = component.from_value(decoded[key])
        if "observed_delivery" in decoded:
            decoded["observed_delivery"] = decode_historical_observation_v2(
                decoded["observed_delivery"]
            )
        try:
            return cast(Self, cast(Any, cls)(**decoded))
        except TypeError:
            raise SharingError("invalid_arguments") from None

    @classmethod
    def from_json(cls, raw: bytes) -> Self:
        if type(raw) is not bytes or len(raw) > MAX_HISTORICAL_V2_BYTES:
            raise SharingError("invalid_arguments")
        from .historical_contracts import _unique_object

        try:
            value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_object)
        except UnicodeDecodeError, json.JSONDecodeError:
            raise SharingError("invalid_arguments") from None
        return cls.from_value(value)

    def canonical_bytes(self) -> bytes:
        raw = portable_canonical_json_bytes(self.value())
        if len(raw) > MAX_HISTORICAL_V2_BYTES:
            raise SharingError("invalid_arguments")
        return raw

    @property
    def request_sha256(self) -> str:
        return sha256(b"open-brain-historical-request.v2\0" + self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalBaselineRequestV2(_HistoricalRequest):
    operation_id: str
    destination: HistoricalDestination
    source_cas: HistoricalSourceCAS
    retained_original: RetainedCaptureEvidenceV2
    observed_delivery: SourceRevisionObservedDelivery
    expected_claim_generation: int
    dto_version: int = 2

    def __post_init__(self) -> None:
        self._validate()
        if (
            type(self.retained_original) is not RetainedCaptureEvidenceV2
            or type(self.observed_delivery) is not SourceRevisionObservedDelivery
        ):
            raise SharingError("invalid_arguments")
        observed = decode_historical_observation_v2(
            json.loads(self.observed_delivery.custody_bytes())
        )
        source = self.source_cas
        if (
            observed.binding.destination_brain_id != self.destination.brain_id
            or observed.binding.issuer_epoch != self.destination.issuer_epoch
            or observed.submission.expected_head != source.expected_head
            or self.retained_original.capture_id != source.expected_head
            or observed.expected_lifecycle_version != source.expected_lifecycle_version
            or observed.submission.expected_control_epoch != source.expected_control_epoch
        ):
            raise SharingError("invalid_arguments")
        source.require_active()
        self.canonical_bytes()


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalClaimRequestV2(_HistoricalRequest):
    operation_id: str
    destination: HistoricalDestination
    source_cas: HistoricalSourceCAS
    capture_source_cas: HistoricalSourceCAS
    retained_capture: RetainedCaptureEvidenceV2
    claim_role: str
    expected_claim_generation: int
    dto_version: int = 2

    def __post_init__(self) -> None:
        self._validate()
        _text(self.claim_role)
        if (
            type(self.retained_capture) is not RetainedCaptureEvidenceV2
            or type(self.capture_source_cas) is not HistoricalSourceCAS
            or self.claim_role not in ("baseline_original", "historical_copy")
        ):
            raise SharingError("invalid_arguments")
        if self.claim_role == "baseline_original" and self.capture_source_cas != self.source_cas:
            raise SharingError("invalid_arguments")
        _consistent_source_witnesses(self.source_cas, self.capture_source_cas)
        # A denial claim may protect a retired/missing/historical source. Stored
        # evidence and exact state CAS must still be verified by the operation.


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalCopyRelationRequestV2(_HistoricalRequest):
    operation_id: str
    destination: HistoricalDestination
    source_cas: HistoricalSourceCAS
    copy_source_cas: HistoricalSourceCAS
    baseline_operation_id: str
    copy_claim_operation_id: str
    retained_copy: RetainedCaptureEvidenceV2
    approval_evidence_sha256: str
    provider_ids: tuple[str, ...]
    expected_relation_version: int
    expected_claim_generation: int
    dto_version: int = 2

    def __post_init__(self) -> None:
        self._validate()
        _text(self.baseline_operation_id, pattern=_OPERATION)
        _text(self.copy_claim_operation_id, pattern=_OPERATION)
        _digest(self.approval_evidence_sha256)
        _version(self.expected_relation_version)
        if (
            type(self.retained_copy) is not RetainedCaptureEvidenceV2
            or type(self.copy_source_cas) is not HistoricalSourceCAS
        ):
            raise SharingError("invalid_arguments")
        _consistent_source_witnesses(self.source_cas, self.copy_source_cas)
        if (
            self.retained_copy.capture_id == self.source_cas.expected_head
            or self.retained_copy.capture_id != self.copy_source_cas.expected_head
        ):
            raise SharingError("invalid_arguments")
        if type(self.provider_ids) not in (tuple, list) or not 1 <= len(self.provider_ids) <= 16:
            raise SharingError("invalid_arguments")
        providers = tuple(_text(item, pattern=_PROVIDER, limit=64) for item in self.provider_ids)
        if providers != tuple(sorted(set(providers))):
            raise SharingError("invalid_arguments")
        object.__setattr__(self, "provider_ids", providers)
        self.source_cas.require_active()
        self.copy_source_cas.require_active()


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalRevocationRequestV2(_HistoricalRequest):
    operation_id: str
    destination: HistoricalDestination
    source_cas: HistoricalSourceCAS
    relation_operation_id: str
    expected_relation_version: int
    expected_claim_generation: int
    reason_code: str
    dto_version: int = 2

    def __post_init__(self) -> None:
        self._validate()
        _text(self.relation_operation_id, pattern=_OPERATION)
        _version(self.expected_relation_version, minimum=1)
        _text(self.reason_code, limit=96)
        # Revocation removes authority even when the source is already inactive.


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoricalReceiptV2(_ClosedComponent):
    """Immutable operation evidence, not a current eligibility/durability grant."""

    operation_id: str
    request_sha256: str
    destination: HistoricalDestination
    source_id: str
    original_capture_id: str
    copy_capture_id: str | None
    outcome: str
    claim_generation: int
    relation_version: int | None
    recorded_at: str
    receipt_sha256: str
    dto_version: int = 2

    def value(self) -> dict[str, object]:
        value = super().value()
        value["destination"] = self.destination.value()
        return value

    @classmethod
    def from_value(cls, value: object) -> Self:
        names = {field.name for field in fields(cls)}
        if type(value) is not dict or set(value) != names:
            raise SharingError("invalid_arguments")
        decoded = dict(value)
        decoded["destination"] = HistoricalDestination.from_value(decoded["destination"])
        try:
            return cls(**decoded)
        except TypeError:
            raise SharingError("invalid_arguments") from None

    def canonical_bytes(self) -> bytes:
        return portable_canonical_json_bytes(self.value())

    def __post_init__(self) -> None:
        if _version(self.dto_version, minimum=1) != 2:
            raise SharingError("invalid_arguments")
        if type(self.destination) is not HistoricalDestination:
            raise SharingError("invalid_arguments")
        _text(self.operation_id, pattern=_OPERATION)
        _digest(self.request_sha256)
        _identity(self.source_id, "source_")
        _identity(self.original_capture_id, "capture_")
        _version(self.claim_generation, minimum=1)
        _text(self.outcome)
        _text(self.recorded_at, limit=32)
        try:
            instant = datetime.fromisoformat(self.recorded_at)
        except ValueError:
            raise SharingError("invalid_arguments") from None
        if (
            not self.recorded_at.endswith("Z")
            or instant.tzinfo != UTC
            or instant.isoformat().replace("+00:00", "Z") != self.recorded_at
        ):
            raise SharingError("invalid_arguments")
        if self.copy_capture_id is not None:
            _identity(self.copy_capture_id, "capture_")
            if self.copy_capture_id == self.original_capture_id:
                raise SharingError("invalid_arguments")
        if self.outcome in ("historical_copy_linked", "historical_copy_revoked"):
            if self.copy_capture_id is None:
                raise SharingError("invalid_arguments")
            _version(self.relation_version, minimum=1)
        elif self.outcome in ("baseline_adopted", "denied_claim_recorded"):
            if self.relation_version is not None:
                raise SharingError("invalid_arguments")
            if self.outcome == "baseline_adopted" and self.copy_capture_id is not None:
                raise SharingError("invalid_arguments")
        else:
            raise SharingError("invalid_arguments")
        _digest(self.receipt_sha256)
        body = {key: item for key, item in self.value().items() if key != "receipt_sha256"}
        digest = sha256(
            b"open-brain-historical-receipt.v2\0" + portable_canonical_json_bytes(body)
        ).hexdigest()
        if digest != self.receipt_sha256:
            raise SharingError("invalid_arguments")
