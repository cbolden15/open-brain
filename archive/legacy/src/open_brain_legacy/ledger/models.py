"""Closed immutable values shared by the ledger scan and stage seams."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath
from typing import TypedDict

from open_brain_engine.core.ids import CaptureId, canonical_json_bytes, validate_identifier
from open_brain_engine.core.models import (
    CaptureSource,
    ContentKind,
    PrivacyDecision,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.ports import RedactionReceipt

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_TOPIC_LABEL = re.compile(r"[\\#>*_`\[\]()<>|]")
_ALLOWED_ROUTE_TIERS = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL})


class LedgerValidationError(ValueError):
    """A ledger transfer value is malformed or has lost its immutable binding."""


def _nfc_string(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LedgerValidationError(f"invalid {field}")
    normalized = unicodedata.normalize("NFC", value)
    if (not allow_empty and (not normalized or normalized.isspace())) or any(
        ord(character) < 32 for character in normalized
    ):
        raise LedgerValidationError(f"invalid {field}")
    return normalized


def _version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise LedgerValidationError("invalid ledger taxonomy version")
    return value


def _topic_label(value: object) -> str:
    normalized = _nfc_string(value, field="ledger topic label")
    if (
        len(normalized) > 128
        or any(not character.isprintable() for character in normalized)
        or _UNSAFE_TOPIC_LABEL.search(normalized)
    ):
        raise LedgerValidationError("invalid ledger topic label")
    return normalized


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise LedgerValidationError(f"invalid {field}")
    return value


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LedgerValidationError("invalid capture timestamp")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relative_components(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise LedgerValidationError("invalid ledger path prefix")
    components: list[str] = []
    for component in value:
        normalized = _nfc_string(component, field="ledger path component")
        if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
            raise LedgerValidationError("invalid ledger path prefix")
        components.append(normalized)
    return tuple(components)


def validate_source_locator(value: object) -> PurePosixPath:
    """Accept only an already-trusted, root-relative POSIX location capability."""
    if not isinstance(value, PurePosixPath) or value.is_absolute():
        raise LedgerValidationError("invalid ledger source locator")
    parts = value.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise LedgerValidationError("invalid ledger source locator")
    if any("\\" in part or any(ord(character) < 32 for character in part) for part in parts):
        raise LedgerValidationError("invalid ledger source locator")
    return PurePosixPath(*parts)


@dataclass(frozen=True, slots=True)
class LedgerRoute:
    """One trusted taxonomy route. It is never derived from event or model text."""

    path_prefix: tuple[str, ...]
    topic_id: str
    topic_label: str
    privacy_tier: PrivacyTier | None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if _relative_components(self.path_prefix) != self.path_prefix:
            raise LedgerValidationError("invalid ledger path prefix")
        normalized_id = _nfc_string(self.topic_id, field="ledger topic ID")
        if normalized_id != self.topic_id or not _IDENTIFIER.fullmatch(normalized_id):
            raise LedgerValidationError("invalid ledger topic ID")
        if _topic_label(self.topic_label) != self.topic_label:
            raise LedgerValidationError("invalid ledger topic label")
        if self.privacy_tier is not None and (
            not isinstance(self.privacy_tier, PrivacyTier)
            or self.privacy_tier not in _ALLOWED_ROUTE_TIERS
        ):
            raise LedgerValidationError("invalid ledger route privacy tier")

    @classmethod
    def create(
        cls,
        *,
        path_prefix: tuple[str, ...],
        topic_id: str,
        topic_label: str,
        privacy_tier: PrivacyTier | str | None,
    ) -> LedgerRoute:
        normalized_tier: PrivacyTier | None
        if privacy_tier is None:
            normalized_tier = None
        else:
            try:
                normalized_tier = PrivacyTier(privacy_tier)
            except (TypeError, ValueError) as error:
                raise LedgerValidationError("invalid ledger route privacy tier") from error
            if normalized_tier not in _ALLOWED_ROUTE_TIERS:
                raise LedgerValidationError("invalid ledger route privacy tier")
        normalized_id = _nfc_string(topic_id, field="ledger topic ID")
        if not _IDENTIFIER.fullmatch(normalized_id):
            raise LedgerValidationError("invalid ledger topic ID")
        return cls(
            path_prefix=_relative_components(path_prefix),
            topic_id=normalized_id,
            topic_label=_topic_label(topic_label),
            privacy_tier=normalized_tier,
        )

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "path_prefix": list(self.path_prefix),
            "topic_id": self.topic_id,
            "topic_label": self.topic_label,
            "privacy_tier": None if self.privacy_tier is None else self.privacy_tier.value,
        }


@dataclass(frozen=True, slots=True)
class LedgerTaxonomy:
    """Validated immutable configuration for deterministic longest-prefix routing."""

    version: str
    routes: tuple[LedgerRoute, ...]

    @classmethod
    def create(cls, *, version: str, routes: tuple[LedgerRoute, ...]) -> LedgerTaxonomy:
        if not isinstance(routes, tuple) or any(
            not isinstance(route, LedgerRoute) for route in routes
        ):
            raise LedgerValidationError("invalid ledger taxonomy routes")
        for route in routes:
            route.validate()
        normalized_version = _version(version)
        prefixes = [route.path_prefix for route in routes]
        if len(prefixes) != len(set(prefixes)):
            raise LedgerValidationError("ambiguous ledger taxonomy routes")
        return cls(
            version=normalized_version,
            routes=tuple(sorted(routes, key=lambda route: route.path_prefix)),
        )

    @classmethod
    def empty(cls) -> LedgerTaxonomy:
        return cls.create(version="ledger-v1", routes=())

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "routes": [route.to_dict() for route in self.routes]}

    def route_for(self, source_locator: PurePosixPath) -> LedgerRoute | None:
        locator = validate_source_locator(source_locator)
        for route in self.routes:
            route.validate()
        candidates = tuple(
            route
            for route in self.routes
            if locator.parts[: len(route.path_prefix)] == route.path_prefix
        )
        if not candidates:
            return None
        longest = max(len(route.path_prefix) for route in candidates)
        matches = tuple(route for route in candidates if len(route.path_prefix) == longest)
        if len(matches) != 1:
            raise LedgerValidationError("ambiguous ledger taxonomy route")
        return matches[0]


class _LedgerScanRecordValues(TypedDict):
    capture_id: CaptureId
    event_id: str
    event_digest_sha256: str
    event_type: str
    source_locator: PurePosixPath
    content_digest_sha256: str
    taxonomy_version: str
    route: LedgerRoute | None
    event_privacy_decision: PrivacyDecision
    privacy_decision: PrivacyDecision
    upstream_redaction_receipt: RedactionReceipt
    redacted_text: str
    capture_why: str
    captured_at: datetime
    capture_source: CaptureSource
    source_type: SourceType
    content_kind: ContentKind
    provenance: Provenance
    topic_id: str | None
    topic_label: str | None


@dataclass(frozen=True, slots=True)
class LedgerScanRecord:
    """Immutable verified handoff from a distillation item and event to one stage."""

    record_id: str
    capture_id: CaptureId
    event_id: str
    event_digest_sha256: str
    event_type: str
    source_locator: PurePosixPath
    content_digest_sha256: str
    taxonomy_version: str
    route: LedgerRoute | None
    event_privacy_decision: PrivacyDecision
    privacy_decision: PrivacyDecision
    upstream_redaction_receipt: RedactionReceipt
    redacted_text: str
    capture_why: str
    captured_at: datetime
    capture_source: CaptureSource
    source_type: SourceType
    content_kind: ContentKind
    provenance: Provenance
    topic_id: str | None
    topic_label: str | None

    @classmethod
    def create(
        cls,
        *,
        capture_id: CaptureId,
        event_id: str,
        event_digest_sha256: str,
        event_type: str,
        source_locator: PurePosixPath,
        content_digest_sha256: str,
        taxonomy_version: str,
        route: LedgerRoute | None,
        event_privacy_decision: PrivacyDecision,
        privacy_decision: PrivacyDecision,
        upstream_redaction_receipt: RedactionReceipt,
        redacted_text: str,
        capture_why: str,
        captured_at: datetime,
        capture_source: CaptureSource,
        source_type: SourceType,
        content_kind: ContentKind,
        provenance: Provenance,
        topic_id: str | None,
        topic_label: str | None,
    ) -> LedgerScanRecord:
        try:
            normalized_capture_id = CaptureId(validate_identifier(str(capture_id), prefix="cap_"))
        except ValueError as error:
            raise LedgerValidationError("invalid ledger capture ID") from error
        if not isinstance(event_id, str) or not _IDENTIFIER.fullmatch(event_id):
            raise LedgerValidationError("invalid ledger event ID")
        if not isinstance(event_type, str) or event_type != "capture.extracted":
            raise LedgerValidationError("invalid ledger event type")
        if not isinstance(route, LedgerRoute | None):
            raise LedgerValidationError("invalid ledger route")
        if route is not None:
            route.validate()
        if not isinstance(event_privacy_decision, PrivacyDecision) or not isinstance(
            privacy_decision, PrivacyDecision
        ):
            raise LedgerValidationError("invalid ledger privacy decision")
        if not isinstance(upstream_redaction_receipt, RedactionReceipt):
            raise LedgerValidationError("invalid ledger redaction receipt")
        if (
            not isinstance(capture_source, CaptureSource)
            or not isinstance(source_type, SourceType)
            or not isinstance(content_kind, ContentKind)
            or not isinstance(provenance, Provenance)
        ):
            raise LedgerValidationError("invalid ledger provenance")
        normalized_topic_id = (
            None if topic_id is None else _nfc_string(topic_id, field="ledger topic ID")
        )
        normalized_topic_label = None if topic_label is None else _topic_label(topic_label)
        if (normalized_topic_id is None) is not (normalized_topic_label is None):
            raise LedgerValidationError("invalid ledger topic")
        value = cls(
            record_id="",
            capture_id=normalized_capture_id,
            event_id=event_id,
            event_digest_sha256=_digest(event_digest_sha256, field="ledger event digest"),
            event_type=event_type,
            source_locator=validate_source_locator(source_locator),
            content_digest_sha256=_digest(content_digest_sha256, field="ledger content digest"),
            taxonomy_version=_version(taxonomy_version),
            route=route,
            event_privacy_decision=event_privacy_decision,
            privacy_decision=privacy_decision,
            upstream_redaction_receipt=upstream_redaction_receipt,
            redacted_text=_nfc_string(
                redacted_text, field="ledger redacted text", allow_empty=True
            ),
            capture_why=_nfc_string(capture_why, field="capture why"),
            captured_at=captured_at,
            capture_source=capture_source,
            source_type=source_type,
            content_kind=content_kind,
            provenance=provenance,
            topic_id=normalized_topic_id,
            topic_label=normalized_topic_label,
        )
        _timestamp(value.captured_at)
        return cls(record_id=value._expected_record_id(), **value._without_record_id())

    def _without_record_id(self) -> _LedgerScanRecordValues:
        return {
            "capture_id": self.capture_id,
            "event_id": self.event_id,
            "event_digest_sha256": self.event_digest_sha256,
            "event_type": self.event_type,
            "source_locator": self.source_locator,
            "content_digest_sha256": self.content_digest_sha256,
            "taxonomy_version": self.taxonomy_version,
            "route": self.route,
            "event_privacy_decision": self.event_privacy_decision,
            "privacy_decision": self.privacy_decision,
            "upstream_redaction_receipt": self.upstream_redaction_receipt,
            "redacted_text": self.redacted_text,
            "capture_why": self.capture_why,
            "captured_at": self.captured_at,
            "capture_source": self.capture_source,
            "source_type": self.source_type,
            "content_kind": self.content_kind,
            "provenance": self.provenance,
            "topic_id": self.topic_id,
            "topic_label": self.topic_label,
        }

    def _identity_dict(self) -> dict[str, object]:
        return self.to_dict(include_record_id=False)

    def _expected_record_id(self) -> str:
        return "ledger_" + sha256(canonical_json_bytes(self._identity_dict())).hexdigest()

    def validate(self) -> None:
        recreated = LedgerScanRecord.create(**self._without_record_id())
        if self.record_id != recreated.record_id:
            raise LedgerValidationError("ledger scan record binding mismatch")

    def to_dict(self, *, include_record_id: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "capture_id": str(self.capture_id),
            "event_id": self.event_id,
            "event_digest_sha256": self.event_digest_sha256,
            "event_type": self.event_type,
            "source_locator": self.source_locator.as_posix(),
            "content_digest_sha256": self.content_digest_sha256,
            "taxonomy_version": self.taxonomy_version,
            "route": None if self.route is None else self.route.to_dict(),
            "event_privacy_decision": self.event_privacy_decision.to_dict(),
            "privacy_decision": self.privacy_decision.to_dict(),
            "upstream_redaction_receipt": self.upstream_redaction_receipt.to_dict(),
            "redacted_text": self.redacted_text,
            "capture_why": self.capture_why,
            "captured_at": _timestamp(self.captured_at),
            "capture_source": self.capture_source.value,
            "source_type": self.source_type.value,
            "content_kind": self.content_kind.value,
            "provenance": self.provenance.to_dict(),
            "topic_id": self.topic_id,
            "topic_label": self.topic_label,
        }
        if include_record_id:
            return {"record_id": self.record_id, **value}
        return value

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(self.to_dict())
