"""Immutable consumer segment custody; these contracts grant no admission authority."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from open_brain_engine.core.models import CaptureWhyOrigin, ContentOrigin, Intent

from .capture_lifecycle_contracts import (
    LifecycleError,
    SegmentBinding,
    canonical_bytes,
    decode_canonical,
    validate_capture_id,
    validate_digest,
)
from .capture_recovery import CaptureReservationIdentity
from .consumer_contracts import segment_key as derive_segment_key
from .contracts import CaptureAction, CaptureSubmissionPath, JournalEnvelope, TextPayload

MAX_CONSUMER_SEGMENT_BYTES = 1024 * 1024
_SOURCE_FIELDS = frozenset({
    "namespace_id", "conversation_key", "segment_key", "source_id", "expected_head",
    "head_version", "route_version", "lifecycle_version", "lifecycle", "availability",
    "space_id", "effective_privacy_sha256", "control_epoch",
})
_SEGMENT_FIELDS = frozenset({
    "contract", "conversation_key", "conversation_revision_sha256", "normalized_sha256",
    "segment_index", "segment_key", "capture", "reservation", "source",
})
_RESERVATION_FIELDS = frozenset(CaptureReservationIdentity.__dataclass_fields__)


def _counter(value: object) -> None:
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise LifecycleError("invalid_arguments")


def _identity(value: object, prefix: str) -> None:
    if type(value) is not str or not value.startswith(prefix + "_"):
        raise LifecycleError("invalid_arguments")
    suffix = value[len(prefix) + 1:]
    try:
        parsed = UUID(suffix)
    except ValueError:
        raise LifecycleError("invalid_arguments") from None
    if parsed.version != 4 or str(parsed) != suffix:
        raise LifecycleError("invalid_arguments")


@dataclass(frozen=True, slots=True)
class ConsumerSourceBinding:
    namespace_id: str
    conversation_key: str
    segment_key: str
    source_id: str
    expected_head: str | None
    head_version: int
    route_version: int
    lifecycle_version: int
    lifecycle: str
    availability: str
    space_id: str | None
    effective_privacy_sha256: str | None
    control_epoch: int

    def __post_init__(self) -> None:
        if type(self.namespace_id) is not str or re.fullmatch(
            r"[\x21-\x7e]{1,128}", self.namespace_id
        ) is None:
            raise LifecycleError("invalid_arguments")
        validate_digest(self.conversation_key)
        validate_digest(self.segment_key)
        _identity(self.source_id, "source")
        for value in (
            self.head_version, self.route_version, self.lifecycle_version, self.control_epoch
        ):
            _counter(value)
        if type(self.lifecycle) is not str or self.lifecycle not in {"active", "retired"}:
            raise LifecycleError("invalid_arguments")
        if type(self.availability) is not str or self.availability not in {
            "available", "missing", "inaccessible", "unknown"
        }:
            raise LifecycleError("invalid_arguments")
        if self.space_id is not None:
            _identity(self.space_id, "space")
        if self.effective_privacy_sha256 is not None:
            validate_digest(self.effective_privacy_sha256)
        if self.expected_head is None:
            if (
                self.head_version != 0 or self.route_version != 0 or self.lifecycle_version != 0
                or self.lifecycle != "active" or self.availability != "available"
                or self.space_id is not None or self.effective_privacy_sha256 is not None
            ):
                raise LifecycleError("invalid_arguments")
        else:
            validate_capture_id(self.expected_head)
            if self.head_version < 1 or self.effective_privacy_sha256 is None:
                raise LifecycleError("invalid_arguments")

    def value(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: object) -> ConsumerSourceBinding:
        if type(value) is not dict or set(value) != _SOURCE_FIELDS:
            raise LifecycleError("invalid_arguments")
        return cls(**cast(dict[str, Any], value))


@dataclass(frozen=True, slots=True)
class ConsumerSegmentEnvelope:
    conversation_key: str
    conversation_revision_sha256: str
    normalized_sha256: str
    segment_index: int
    segment_key: str
    envelope: JournalEnvelope
    identities: CaptureReservationIdentity
    source: ConsumerSourceBinding

    def __post_init__(self) -> None:
        for value in (
            self.conversation_key, self.conversation_revision_sha256,
            self.normalized_sha256, self.segment_key,
        ):
            validate_digest(value)
        if type(self.segment_index) is not int or not 0 <= self.segment_index < 128:
            raise LifecycleError("invalid_arguments")
        if self.segment_key != derive_segment_key(self.conversation_key, self.segment_index):
            raise LifecycleError("invalid_arguments")
        if (
            type(self.envelope) is not JournalEnvelope
            or type(self.identities) is not CaptureReservationIdentity
            or type(self.source) is not ConsumerSourceBinding
        ):
            raise LifecycleError("invalid_arguments")
        submission = self.envelope.submission
        if (
            self.identities.canonical
            or submission.submission_path is not CaptureSubmissionPath.PUBLIC_JOB
            or submission.action is not CaptureAction.QUICK
            or type(submission.payload) is not TextPayload
            or submission.source_origin is not ContentOrigin.THIRD_PARTY
            or submission.provenance.content_origin is not ContentOrigin.THIRD_PARTY
            or submission.capture_why_origin is not CaptureWhyOrigin.AUTOMATION_ABSENT
            or submission.provenance.owner_context is not CaptureWhyOrigin.AUTOMATION_ABSENT
            or submission.capture_why is not None
            or submission.intent not in (None, Intent.REFERENCE)
            or submission.space_id is not None
            or submission.role_claim["capabilities"] != ("capture.accept",)
            or submission.provenance.source_ref != submission.source_reference
            or self.source.conversation_key != self.conversation_key
            or self.source.segment_key != self.segment_key
        ):
            raise LifecycleError("invalid_arguments")
        expected = (
            rf"urn:open-brain:consumer-chat:(chatgpt|claude):{self.conversation_key}:"
            rf"{self.segment_index}"
        )
        if re.fullmatch(expected, submission.source_reference) is None:
            raise LifecycleError("invalid_arguments")
        try:
            if JournalEnvelope.from_bytes(self.envelope.to_bytes()) != self.envelope:
                raise LifecycleError("invalid_arguments")
            for privacy in (submission.privacy, self.envelope.admitted_privacy):
                if (
                    privacy.authority.cloud is not False
                    or privacy.authority.external_egress is not False
                ):
                    raise LifecycleError("invalid_arguments")
            self.to_bytes()
        except (ValueError, TypeError, AttributeError):
            raise LifecycleError("invalid_arguments") from None

    @property
    def body(self) -> bytes:
        return cast(TextPayload, self.envelope.submission.payload).text.encode("utf-8")

    def to_bytes(self) -> bytes:
        raw = canonical_bytes({
            "contract": "engine.consumer-segment.v1",
            "conversation_key": self.conversation_key,
            "conversation_revision_sha256": self.conversation_revision_sha256,
            "normalized_sha256": self.normalized_sha256,
            "segment_index": self.segment_index,
            "segment_key": self.segment_key,
            "capture": self.envelope.to_dict(),
            "reservation": asdict(self.identities),
            "source": self.source.value(),
        })
        if len(raw) > MAX_CONSUMER_SEGMENT_BYTES:
            raise LifecycleError("invalid_arguments")
        return raw

    @classmethod
    def from_bytes(cls, raw: bytes) -> ConsumerSegmentEnvelope:
        try:
            if type(raw) is not bytes or not 0 < len(raw) <= MAX_CONSUMER_SEGMENT_BYTES:
                raise LifecycleError("invalid_evidence")
            value = decode_canonical(raw)
            if (
                type(value) is not dict or set(value) != _SEGMENT_FIELDS
                or value["contract"] != "engine.consumer-segment.v1"
                or type(value["reservation"]) is not dict
                or set(value["reservation"]) != _RESERVATION_FIELDS
            ):
                raise LifecycleError("invalid_evidence")
            result = cls(
                conversation_key=value["conversation_key"],
                conversation_revision_sha256=value["conversation_revision_sha256"],
                normalized_sha256=value["normalized_sha256"],
                segment_index=value["segment_index"],
                segment_key=value["segment_key"],
                envelope=JournalEnvelope.from_bytes(canonical_bytes(value["capture"])),
                identities=CaptureReservationIdentity(**value["reservation"]),
                source=ConsumerSourceBinding.from_value(value["source"]),
            )
            if result.to_bytes() != raw:
                raise LifecycleError("invalid_evidence")
            return result
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise LifecycleError("invalid_evidence") from None

    @property
    def binding(self) -> SegmentBinding:
        return SegmentBinding(
            self.identities.capture_id,
            sha256(self.body).hexdigest(),
            sha256(self.to_bytes()).hexdigest(),
        )
