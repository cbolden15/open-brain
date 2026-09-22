"""Public engine values and task contracts."""

from __future__ import annotations

import json
import re
import unicodedata
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from html import unescape
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import unquote

from open_brain_engine.core.ids import canonicalize_source_url, portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    Intent,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.providers.base import ProviderMode

from .normalization import (
    _DECIMAL,
    _EVENT_TYPE,
    _MAX_FILE_BYTES,
    _MAX_REASON,
    _MAX_TEXT,
    _MEDIA_TYPE,
    _UNIT,
    _attribute_list,
    _attributes,
    _delivery_id,
    _optional_text,
    _optional_timestamp,
    _pairs,
    _portable_id,
    _privacy,
    _role_claim,
    _text,
)
from .privacy_projection import _NARROWED_REASON
from .t03_contracts import EffectiveAuthority, SourceRouteRequest, SourceRouteResponse

if TYPE_CHECKING:
    from .privacy_repairs import PrivacyRepairReceipt, PrivacyRepairRequest
    from .source_intake import SourceRevisionReceipt, SourceRevisionSubmission
    from .t03_contracts import (
        DecisionHistoryRequest,
        DecisionHistoryResponse,
        EffectiveAuthority,
        HistoryListRequest,
        HistoryListResponse,
        RecordReadRequest,
        RecordReadResponse,
        RelationshipDecideRequest,
        RelationshipDecideResponse,
        RelationshipListRequest,
        RelationshipListResponse,
        SearchPageRequest,
        SearchPageResponse,
    )

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CaptureAction(StrEnum):
    QUICK = "quick"
    CANONICAL_NOTE = "canonical_note"


class CaptureSubmissionPath(StrEnum):
    OWNER = "owner"
    PUBLIC_JOB = "public_job"
    DESTINATION_BOUND = "destination_bound"


class DecisionOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class CaptureFault(StrEnum):
    AFTER_CAPTURE_RESERVATION = "after_capture_reservation"
    AFTER_BLOB_WRITE = "after_blob_write"
    AFTER_SOURCE_WRITE = "after_source_write"
    AFTER_AUTOMATIC_PROPOSAL_WRITE = "after_automatic_proposal_write"
    AFTER_AUTOMATIC_DECISION_WRITE = "after_automatic_decision_write"
    AFTER_CANONICAL_PAGE_WRITE = "after_canonical_page_write"
    AFTER_PUBLICATION_WRITE = "after_publication_write"
    AFTER_INDEX_UPDATE = "after_index_update"
    AFTER_SPACE_RESERVATION = "after_space_reservation"
    AFTER_SPACE_WRITE = "after_space_write"
    AFTER_ROUTE_RESERVATION = "after_route_reservation"
    AFTER_ROUTE_SOURCE_WRITE = "after_route_source_write"
    AFTER_PROPOSAL_RESERVATION = "after_proposal_reservation"
    AFTER_PROPOSAL_WRITE = "after_proposal_write"
    AFTER_DECISION_RESERVATION = "after_decision_reservation"
    AFTER_DECISION_WRITE = "after_decision_write"
    AFTER_REVIEW_PAGE_WRITE = "after_review_page_write"
    AFTER_REVIEW_PUBLICATION_WRITE = "after_review_publication_write"


class PortabilityFault(StrEnum):
    AFTER_STAGE_CREATED = "after_stage_created"
    AFTER_PORTABLE_FILE = "after_portable_file"
    AFTER_MANIFEST = "after_manifest"
    AFTER_PROFILE = "after_profile"
    AFTER_MATERIALIZATION = "after_materialization"
    AFTER_INDEX = "after_index"
    AFTER_READY = "after_ready"
    BEFORE_PROMOTION = "before_promotion"
    AFTER_PROMOTION = "after_promotion"


class ManagedWorkspaceFault(StrEnum):
    AFTER_OPERATION_PREPARED = "after_operation_prepared"
    AFTER_TARGET_WRITE = "after_target_write"
    AFTER_OPERATION_PROMOTED = "after_operation_promoted"


class InjectedFault(RuntimeError):
    """Synthetic process interruption at one named durable boundary."""

    def __init__(self, point: CaptureFault | PortabilityFault | ManagedWorkspaceFault) -> None:
        self.point = point
        super().__init__(point.value)


class Payload(Protocol):
    @property
    def family(self) -> str: ...

    def to_dict(self) -> dict[str, object]: ...

    def search_text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TextPayload:
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _text(self.text, field="text", maximum=_MAX_TEXT))

    @property
    def family(self) -> str:
        return "text"

    def to_dict(self) -> dict[str, object]:
        return {"family": self.family, "text": self.text}

    def search_text(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class ReferencePayload:
    url: str
    supplied_text: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", canonicalize_source_url(self.url))
        if self.supplied_text is not None:
            object.__setattr__(
                self,
                "supplied_text",
                _text(self.supplied_text, field="supplied text", maximum=_MAX_TEXT),
            )

    @property
    def family(self) -> str:
        return "reference_or_file"

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"family": self.family, "kind": "reference", "url": self.url}
        if self.supplied_text is not None:
            value["supplied_text"] = self.supplied_text
        return value

    def search_text(self) -> str:
        return " ".join(part for part in (self.url, self.supplied_text) if part)


@dataclass(frozen=True, slots=True)
class FilePayload:
    file_name: str
    media_type: str
    data: bytes

    def __post_init__(self) -> None:
        name = _text(self.file_name, field="file name", maximum=255)
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("invalid file name")
        if not isinstance(self.media_type, str) or _MEDIA_TYPE.fullmatch(self.media_type) is None:
            raise ValueError("invalid media type")
        if not isinstance(self.data, bytes) or len(self.data) > _MAX_FILE_BYTES:
            raise ValueError("invalid file payload")
        object.__setattr__(self, "file_name", name)

    @property
    def family(self) -> str:
        return "reference_or_file"

    @property
    def digest(self) -> str:
        return sha256(self.data).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "blob_sha256": self.digest,
            "family": self.family,
            "file_name": self.file_name,
            "kind": "file",
            "media_type": self.media_type,
        }

    def search_text(self) -> str:
        try:
            decoded = self.data.decode("utf-8") if self.media_type.startswith("text/") else ""
        except UnicodeDecodeError:
            decoded = ""
        return f"{self.file_name} {self.media_type} {decoded}"


@dataclass(frozen=True, slots=True)
class EventPayload:
    event_type: str
    occurrence_at: str | None
    attributes: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or _EVENT_TYPE.fullmatch(self.event_type) is None:
            raise ValueError("invalid event type")
        object.__setattr__(self, "occurrence_at", _optional_timestamp(self.occurrence_at))
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    @property
    def family(self) -> str:
        return "event"

    def to_dict(self) -> dict[str, object]:
        return {
            "attributes": _attribute_list(self.attributes),
            "event_type": self.event_type,
            "family": self.family,
            "occurrence_at": self.occurrence_at,
        }

    def search_text(self) -> str:
        return " ".join((self.event_type, self.occurrence_at or "", *(_pairs(self.attributes))))


@dataclass(frozen=True, slots=True)
class MeasurementPayload:
    value: str
    unit: str
    occurrence_at: str | None
    dimensions: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or _DECIMAL.fullmatch(self.value) is None:
            raise ValueError("invalid measurement value")
        if not isinstance(self.unit, str) or _UNIT.fullmatch(self.unit) is None:
            raise ValueError("invalid measurement unit")
        object.__setattr__(self, "occurrence_at", _optional_timestamp(self.occurrence_at))
        object.__setattr__(self, "dimensions", _attributes(self.dimensions))

    @property
    def family(self) -> str:
        return "measurement"

    def to_dict(self) -> dict[str, object]:
        return {
            "dimensions": _attribute_list(self.dimensions),
            "family": self.family,
            "occurrence_at": self.occurrence_at,
            "unit": self.unit,
            "value": self.value,
        }

    def search_text(self) -> str:
        return " ".join(
            (self.value, self.unit, self.occurrence_at or "", *(_pairs(self.dimensions)))
        )


@dataclass(frozen=True, slots=True)
class CaptureReceipt:
    capture_id: str
    payload_family: str
    state: str
    enrichment_state: str
    space_id: str | None
    canonical_path: str | None
    duplicate: bool = False
    # An unbound tier fails closed to ``unknown``; constructors that know the
    # submission always set both, so they differ only when admission narrowed.
    requested_tier: PrivacyTier = PrivacyTier.UNKNOWN
    final_admitted_tier: PrivacyTier = PrivacyTier.UNKNOWN
    # Destination-bound receipts bind their immutable request identity and the
    # trusted authority's destination Brain and issuer epoch; every other path
    # leaves them unset so existing receipt bytes stay unchanged.
    delivery_id: str | None = None
    request_sha256: str | None = None
    destination_brain_id: str | None = None
    issuer_epoch: int | None = None


# One canonical-boundary rescan signal for a submission: a tier narrows the
# admitted decision, and ``None`` means the boundary has no narrowing signal.
type BoundaryClassifier = Callable[[CaptureSubmission], PrivacyTier | None]


@dataclass(frozen=True, slots=True)
class InboxItem:
    capture_id: str
    payload_family: str
    state: str
    space_id: str | None
    intent: str | None
    capture_why: str | None
    title: str | None = None
    preview: str = ""


@dataclass(frozen=True, slots=True)
class SpaceRecord:
    space_id: str
    name: str
    slug: str


@dataclass(frozen=True, slots=True)
class RoutedCapture:
    capture_id: str
    space_id: str


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    title: str
    markdown: str
    proposed_kind: str = "page_update"
    supplied_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _text(self.title, field="proposal title", maximum=200))
        object.__setattr__(
            self,
            "markdown",
            _text(self.markdown, field="proposal markdown", maximum=_MAX_TEXT),
        )
        if self.proposed_kind not in {"page_update", "event", "measurement", "action"}:
            raise ValueError("invalid proposal kind")
        if self.supplied_reason is not None:
            object.__setattr__(
                self,
                "supplied_reason",
                _text(self.supplied_reason, field="proposal reason", maximum=_MAX_REASON),
            )


MAX_PATCH_OPERATIONS = 16
MAX_PATCH_REPLACEMENT_BYTES = 16 * 1024
MAX_PATCH_TOTAL_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class PatchOperation:
    """One UTF-8 byte-range replacement in a canonical Markdown body."""

    start_byte: int
    end_byte: int
    replacement: str

    def __post_init__(self) -> None:
        if (
            type(self.start_byte) is not int
            or type(self.end_byte) is not int
            or not 0 <= self.start_byte <= self.end_byte <= MAX_PATCH_TOTAL_BYTES
        ):
            raise ValueError("invalid patch operation")
        replacement = self.replacement
        if (
            not isinstance(replacement, str)
            or "\x00" in replacement
            or any(
                unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
                for character in replacement
            )
            or len(replacement.encode("utf-8")) > MAX_PATCH_REPLACEMENT_BYTES
        ):
            raise ValueError("invalid patch operation")
        object.__setattr__(self, "replacement", replacement)

    def to_dict(self) -> dict[str, object]:
        return {
            "end_byte": self.end_byte,
            "replacement": self.replacement,
            "start_byte": self.start_byte,
        }


@dataclass(frozen=True, slots=True)
class PatchDraft:
    """A revision-bound set of body-only edits for one existing canonical page."""

    target_page_id: str
    expected_page_sha256: str
    operations: tuple[PatchOperation, ...]

    def __post_init__(self) -> None:
        _portable_id(self.target_page_id, "page")
        if (
            not isinstance(self.expected_page_sha256, str)
            or _HEX64.fullmatch(self.expected_page_sha256) is None
        ):
            raise ValueError("invalid patch draft")
        if (
            not isinstance(self.operations, tuple)
            or not 1 <= len(self.operations) <= MAX_PATCH_OPERATIONS
            or any(not isinstance(operation, PatchOperation) for operation in self.operations)
        ):
            raise ValueError("invalid patch draft")
        prior_end = -1
        total = 0
        for operation in self.operations:
            if operation.start_byte < prior_end:
                raise ValueError("overlapping patch operations")
            prior_end = operation.end_byte
            total += len(operation.replacement.encode("utf-8"))
        if total > MAX_PATCH_TOTAL_BYTES:
            raise ValueError("patch replacement limit exceeded")

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_page_sha256": self.expected_page_sha256,
            "operations": [operation.to_dict() for operation in self.operations],
            "target_page_id": self.target_page_id,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentRequest:
    capture_id: str
    payload_family: str
    source_text: str

    def __post_init__(self) -> None:
        _portable_id(self.capture_id, "capture")
        if self.payload_family not in {
            "text",
            "reference_or_file",
            "event",
            "measurement",
        }:
            raise ValueError("invalid enrichment payload family")
        object.__setattr__(
            self,
            "source_text",
            _text(self.source_text, field="enrichment source", maximum=_MAX_TEXT + 512),
        )


class EnrichmentProvider(Protocol):
    def enrich(self, request: EnrichmentRequest) -> Sequence[ProposalDraft]: ...


class EnrichmentUnavailable(RuntimeError):
    """The selected enrichment provider is unavailable without changing capture state."""


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal_id: str
    capture_id: str
    proposed_kind: str
    status: str
    space_id: str | None
    sibling_proposal_ids: tuple[str, ...]
    terminal_decision_id: str | None
    title: str = ""
    capture_ids: tuple[str, ...] = ()
    selected_capture_ids: tuple[str, ...] = ()
    page_id: str | None = None
    target_page_id: str | None = None
    operation: str = "create"
    review_digest: str | None = None
    draft_type: str = "full_page"


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    """Projected evidence; its digest covers the displayed excerpt."""

    capture_id: str
    excerpt: str
    sha256: str
    projection_applied: bool


@dataclass(frozen=True, slots=True)
class ReviewProposal:
    """Public inspection of an immutable proposal and its decision binding."""

    proposal_id: str
    status: str
    title: str
    markdown: str
    space_id: str | None
    page_id: str | None
    target_page_id: str | None
    operation: str
    capture_ids: tuple[str, ...]
    selected_capture_ids: tuple[str, ...]
    evidence: tuple[ReviewEvidence, ...]
    review_digest: str
    expected_page_sha256: str | None
    expected_publication_id: str | None
    projection_applied: bool
    patch: PatchDraft | None = None
    patch_diff: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    proposal_id: str
    outcome: DecisionOutcome
    page_id: str | None
    publication_id: str | None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    result_id: str
    capture_id: str
    record_type: str
    payload_family: str
    space_id: str | None
    title: str
    excerpt: str
    trust: str
    provenance: PublicProvenance
    explanation: str


@dataclass(frozen=True, slots=True)
class PageResult:
    page_id: str
    title: str
    markdown: str
    trust: str


_PUBLIC_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|secret|token)"
    r"(\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)"
)
_PUBLIC_POSIX_PATH = re.compile(r"(?<![:/\w])/(?:[^\s<>\"']+)")
_PUBLIC_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s<>\"']+")
_PUBLIC_BARE_SHA256 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_PUBLIC_LITERAL_MARKER = "[protected]"
_PUBLIC_PATH_MARKER = "[private-path]"
_PUBLIC_CREDENTIAL_MARKER = "[redacted]"
_PUBLIC_OUTPUT_TOKEN = re.compile(r"\S+")
_PUBLIC_OUTPUT_DECODING_PASSES = 4


def project_public_result_text(
    value: str,
    *,
    protected_literals: tuple[str, ...] = (),
) -> str:
    """Project one public result field without changing its durable source value."""
    if not isinstance(value, str) or not isinstance(protected_literals, tuple):
        raise ValueError("invalid public result text")
    if any(not isinstance(literal, str) or not literal for literal in protected_literals):
        raise ValueError("invalid protected literal")

    sensitive_literals = set(protected_literals)
    sensitive_literals.update(
        match.group(0) for match in _PUBLIC_CREDENTIAL_ASSIGNMENT.finditer(value)
    )
    sensitive_literals.update(match.group(0) for match in _PUBLIC_WINDOWS_PATH.finditer(value))
    sensitive_literals.update(match.group(0) for match in _PUBLIC_POSIX_PATH.finditer(value))
    result = value
    for literal in sorted(sensitive_literals, key=len, reverse=True):
        result = re.sub(
            re.escape(literal),
            _PUBLIC_LITERAL_MARKER,
            result,
            flags=re.IGNORECASE,
        )
        digest = sha256(literal.encode("utf-8")).hexdigest()
        result = re.sub(
            rf"(?<![0-9A-Fa-f]){re.escape(digest)}(?![0-9A-Fa-f])",
            _PUBLIC_LITERAL_MARKER,
            result,
            flags=re.IGNORECASE,
        )
    protected_values = frozenset(
        (
            *protected_literals,
            *(sha256(literal.encode("utf-8")).hexdigest() for literal in protected_literals),
        )
    )
    result = _PUBLIC_OUTPUT_TOKEN.sub(
        lambda match: _project_public_output_token(match.group(0), protected_values),
        result,
    )
    result = _PUBLIC_CREDENTIAL_ASSIGNMENT.sub(rf"\1\2{_PUBLIC_CREDENTIAL_MARKER}", result)
    result = _PUBLIC_BARE_SHA256.sub(_PUBLIC_LITERAL_MARKER, result)
    result = _PUBLIC_WINDOWS_PATH.sub(_PUBLIC_PATH_MARKER, result)
    return _PUBLIC_POSIX_PATH.sub(_PUBLIC_PATH_MARKER, result)


def _project_public_output_token(token: str, protected_values: frozenset[str]) -> str:
    variants, converged = _public_output_decoded_variants(token)
    folded_variants = tuple(variant.casefold() for variant in variants)
    contains_protected = any(
        protected.casefold() in variant
        for protected in protected_values
        for variant in folded_variants
    )
    contains_sensitive_shape = any(
        _PUBLIC_CREDENTIAL_ASSIGNMENT.search(variant) is not None
        or _PUBLIC_BARE_SHA256.search(variant) is not None
        or _PUBLIC_WINDOWS_PATH.search(variant) is not None
        or _PUBLIC_POSIX_PATH.search(variant) is not None
        for variant in variants
    )
    if not converged or contains_protected or contains_sensitive_shape:
        return _PUBLIC_LITERAL_MARKER
    return token


def _public_output_decoded_variants(value: str) -> tuple[tuple[str, ...], bool]:
    variants = [value]
    for _ in range(_PUBLIC_OUTPUT_DECODING_PASSES):
        decoded = unquote(unescape(variants[-1]))
        if decoded == variants[-1]:
            return tuple(variants), True
        variants.append(decoded)
    return tuple(variants), unquote(unescape(variants[-1])) == variants[-1]


def project_public_space(space: SpaceRecord) -> SpaceRecord:
    """Project a durable space record for public representations only."""
    if not isinstance(space, SpaceRecord):
        raise ValueError("invalid public space")
    return SpaceRecord(
        space_id=space.space_id,
        name=project_public_result_text(space.name),
        slug=space.space_id,
    )


def project_public_capture_receipt(receipt: CaptureReceipt) -> CaptureReceipt:
    """Project a durable receipt without disclosing its canonical storage path."""
    if not isinstance(receipt, CaptureReceipt):
        raise ValueError("invalid public capture receipt")
    return CaptureReceipt(
        capture_id=receipt.capture_id,
        payload_family=receipt.payload_family,
        state=receipt.state,
        enrichment_state=receipt.enrichment_state,
        space_id=receipt.space_id,
        canonical_path=(receipt.capture_id if receipt.canonical_path is not None else None),
        duplicate=receipt.duplicate,
        requested_tier=receipt.requested_tier,
        final_admitted_tier=receipt.final_admitted_tier,
        delivery_id=receipt.delivery_id,
        request_sha256=receipt.request_sha256,
        destination_brain_id=receipt.destination_brain_id,
        issuer_epoch=receipt.issuer_epoch,
    )


@dataclass(frozen=True, slots=True)
class PortabilityReceipt:
    """Bounded public outcome for one Portable Brain operation."""

    status: str
    portable_files: int
    captures: int
    batches: int
    blobs: int
    history_records: int
    schema_version: int = 1
    index_generation: int | None = None
    duplicate: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"validated", "exported", "imported", "rebuilt"}:
            raise ValueError("invalid portability receipt status")
        for value in (
            self.portable_files,
            self.captures,
            self.batches,
            self.blobs,
            self.history_records,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("invalid portability receipt count")
        if self.schema_version not in {1, 2, 3, 4, 5}:
            raise ValueError("invalid portability receipt schema version")
        if self.index_generation is not None and (
            type(self.index_generation) is not int or self.index_generation < 1
        ):
            raise ValueError("invalid portability index generation")


@dataclass(frozen=True, slots=True)
class ReconciliationReceipt:
    """Bounded public outcome for canonical Markdown reconciliation."""

    status: str
    scanned_files: int
    page_updates: int
    space_updates: int

    def __post_init__(self) -> None:
        if self.status not in {"reconciled", "noop"}:
            raise ValueError("invalid reconciliation receipt status")
        for value in (self.scanned_files, self.page_updates, self.space_updates):
            if type(value) is not int or value < 0:
                raise ValueError("invalid reconciliation receipt count")


class ManagedWorkspaceFailure(RuntimeError):
    """A bounded managed-workspace failure without note content or an absolute path."""

    def __init__(self, code: str) -> None:
        if code not in {
            "active_consent_required",
            "budget_exhausted",
            "conflict_open",
            "inactive_note",
            "ineligible_source",
            "invalid_observation",
            "invalid_policy",
            "invalid_request",
            "invalid_suggestion",
            "operation_conflict",
            "operation_replay_mismatch",
            "request_replay_mismatch",
            "stale_request",
            "stale_observation",
            "target_changed",
            "unsafe_workspace",
            "unknown_note",
            "unknown_request",
            "unknown_suggestion",
            "unknown_workspace",
            "workspace_recovery_required",
        }:
            raise ValueError("invalid managed-workspace failure")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ManagedNoteObservation:
    note_id: str
    relative_path: str
    accepted_revision_id: str
    materialized_sha256: str | None
    observed_sha256: str | None
    present: bool
    changed: bool

    def __post_init__(self) -> None:
        _portable_id(self.note_id, "page")
        _portable_id(self.accepted_revision_id, "revision")
        if (
            not isinstance(self.relative_path, str)
            or not self.relative_path
            or len(self.relative_path) > _MAX_TEXT
        ):
            raise ValueError("invalid managed note path")
        for value in (self.materialized_sha256, self.observed_sha256):
            if value is not None and _HEX64.fullmatch(value) is None:
                raise ValueError("invalid managed note digest")
        if type(self.present) is not bool or type(self.changed) is not bool:
            raise ValueError("invalid managed note observation")
        if self.present is not (self.observed_sha256 is not None):
            raise ValueError("invalid managed note presence")


@dataclass(frozen=True, slots=True)
class ManagedWorkspaceObservation:
    workspace_id: str
    generation: int
    notes: tuple[ManagedNoteObservation, ...]

    def __post_init__(self) -> None:
        _portable_id(self.workspace_id, "workspace")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("invalid managed workspace generation")
        if not isinstance(self.notes, tuple) or len({note.note_id for note in self.notes}) != len(
            self.notes
        ):
            raise ValueError("invalid managed workspace observation")


@dataclass(frozen=True, slots=True)
class ManagedWorkspaceConflictSummary:
    conflict_id: str
    note_id: str
    relative_path: str

    def __post_init__(self) -> None:
        _portable_id(self.conflict_id, "conflict")
        _portable_id(self.note_id, "page")
        if (
            not isinstance(self.relative_path, str)
            or not self.relative_path
            or len(self.relative_path) > _MAX_TEXT
        ):
            raise ValueError("invalid managed conflict path")


@dataclass(frozen=True, slots=True)
class ManagedWorkspaceConflictReview:
    conflict_id: str
    note_id: str
    relative_path: str
    accepted_revision_id: str
    accepted_body: str
    workspace_body: str

    def __post_init__(self) -> None:
        ManagedWorkspaceConflictSummary(
            conflict_id=self.conflict_id,
            note_id=self.note_id,
            relative_path=self.relative_path,
        )
        _portable_id(self.accepted_revision_id, "revision")
        for body in (self.accepted_body, self.workspace_body):
            if (
                not isinstance(body, str)
                or len(body.encode("utf-8")) > _MAX_FILE_BYTES
                or any(ord(character) < 32 and character not in "\n\r\t" for character in body)
            ):
                raise ValueError("invalid managed conflict body")


@dataclass(frozen=True, slots=True)
class ManagedGraphSource:
    note_id: str
    revision_id: str
    relative_path: str
    body: str
    body_sha256: str
    privacy_sha256: str

    def __post_init__(self) -> None:
        _portable_id(self.note_id, "page")
        _portable_id(self.revision_id, "revision")
        if not isinstance(self.relative_path, str):
            raise ValueError("invalid managed graph source")
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or len(self.relative_path) > _MAX_TEXT
            or "\\" in self.relative_path
            or "\x00" in self.relative_path
            or path.is_absolute()
            or path.as_posix() != self.relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix.casefold() != ".md"
            or not isinstance(self.body, str)
            or len(self.body.encode("utf-8")) > 16 * 1024
            or _HEX64.fullmatch(self.body_sha256) is None
            or _HEX64.fullmatch(self.privacy_sha256) is None
        ):
            raise ValueError("invalid managed graph source")


@dataclass(frozen=True, slots=True)
class ManagedGraphSnapshot:
    workspace_id: str
    observation_generation: int
    policy_generation: int
    snapshot_sha256: str
    sources: tuple[ManagedGraphSource, ...]

    def __post_init__(self) -> None:
        _portable_id(self.workspace_id, "workspace")
        if (
            type(self.observation_generation) is not int
            or self.observation_generation < 0
            or type(self.policy_generation) is not int
            or self.policy_generation < 0
            or not isinstance(self.snapshot_sha256, str)
            or _HEX64.fullmatch(self.snapshot_sha256) is None
            or not isinstance(self.sources, tuple)
            or not all(isinstance(source, ManagedGraphSource) for source in self.sources)
            or len(self.sources) > 64
            or len({source.note_id for source in self.sources}) != len(self.sources)
            or sum(len(source.body.encode("utf-8")) for source in self.sources) > 16 * 1024
        ):
            raise ValueError("invalid managed graph snapshot")


@dataclass(frozen=True, slots=True)
class ManagedWorkspaceStatus:
    workspace_id: str
    connected: bool
    observation_generation: int
    policy_generation: int
    active_notes: int
    inactive_notes: int
    open_conflicts: int
    pending_suggestions: int

    def __post_init__(self) -> None:
        _portable_id(self.workspace_id, "workspace")
        if type(self.connected) is not bool:
            raise ValueError("invalid managed workspace connection state")
        for value in (
            self.observation_generation,
            self.policy_generation,
            self.active_notes,
            self.inactive_notes,
            self.open_conflicts,
            self.pending_suggestions,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("invalid managed workspace status count")


@dataclass(frozen=True, slots=True)
class ManagedWorkspaceReceipt:
    status: str
    workspace_id: str
    note_id: str | None = None
    generation: int | None = None
    duplicate: bool = False

    def __post_init__(self) -> None:
        if self.status not in {
            "accepted",
            "conflict_resolved",
            "deactivated",
            "materialized",
            "refreshed",
            "restored",
            "setup",
        }:
            raise ValueError("invalid managed workspace receipt")
        _portable_id(self.workspace_id, "workspace")
        if self.note_id is not None:
            _portable_id(self.note_id, "page")
        if self.generation is not None and (
            type(self.generation) is not int or self.generation < 0
        ):
            raise ValueError("invalid managed workspace generation")
        if type(self.duplicate) is not bool:
            raise ValueError("invalid managed workspace duplicate marker")


class ManagedProvider(StrEnum):
    OPENAI_API = "openai_api"
    ANTHROPIC_API = "anthropic_api"
    CLAUDE_SUBSCRIPTION = "claude_subscription"
    GEMINI_API = "gemini_api"


class ManagedAccessMode(StrEnum):
    API_KEY = "api_key"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True, slots=True)
class ManagedExclusion:
    kind: str
    subject: str
    relative_path: str

    def __post_init__(self) -> None:
        if self.kind not in {"folder", "note"}:
            raise ValueError("invalid managed exclusion kind")
        if self.kind == "note":
            _portable_id(self.subject, "page")
        elif self.subject != self.relative_path:
            raise ValueError("invalid managed folder exclusion")
        if not isinstance(self.relative_path, str):
            raise ValueError("invalid managed exclusion path")
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or len(self.relative_path) > _MAX_TEXT
            or "\\" in self.relative_path
            or "\x00" in self.relative_path
            or path.is_absolute()
            or path.as_posix() != self.relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("invalid managed exclusion path")


@dataclass(frozen=True, slots=True)
class ManagedPolicyReceipt:
    status: str
    workspace_id: str
    policy_generation: int
    duplicate: bool = False

    def __post_init__(self) -> None:
        if self.status not in {"consent_granted", "consent_revoked", "exclusion_updated"}:
            raise ValueError("invalid managed policy receipt")
        _portable_id(self.workspace_id, "workspace")
        if type(self.policy_generation) is not int or self.policy_generation < 0:
            raise ValueError("invalid managed policy generation")
        if type(self.duplicate) is not bool:
            raise ValueError("invalid managed policy duplicate marker")


@dataclass(frozen=True, slots=True)
class ManagedInferenceSource:
    note_id: str
    revision_id: str
    privacy_sha256: str

    def __post_init__(self) -> None:
        _portable_id(self.note_id, "page")
        _portable_id(self.revision_id, "revision")
        if _HEX64.fullmatch(self.privacy_sha256) is None:
            raise ValueError("invalid managed inference privacy digest")


@dataclass(frozen=True, slots=True)
class ManagedInferenceRequest:
    request_id: str
    workspace_id: str
    provider: ManagedProvider
    access_mode: ManagedAccessMode
    adapter_identity: str
    policy_generation: int
    sources: tuple[ManagedInferenceSource, ...]
    prompt: str
    effective_privacy: PrivacyDecision
    max_output_bytes: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        _portable_id(self.request_id, "request")
        _portable_id(self.workspace_id, "workspace")
        object.__setattr__(self, "provider", ManagedProvider(self.provider))
        object.__setattr__(self, "access_mode", ManagedAccessMode(self.access_mode))
        if (
            not isinstance(self.adapter_identity, str)
            or not self.adapter_identity.startswith(f"{self.provider.value}:")
            or not self.adapter_identity.removeprefix(f"{self.provider.value}:")
        ):
            raise ValueError("invalid managed inference adapter")
        if type(self.policy_generation) is not int or self.policy_generation < 0:
            raise ValueError("invalid managed inference policy generation")
        if not isinstance(self.sources, tuple) or not 0 < len(self.sources) <= 64:
            raise ValueError("invalid managed inference sources")
        if len({source.note_id for source in self.sources}) != len(self.sources):
            raise ValueError("invalid managed inference sources")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("invalid managed inference prompt")
        if not isinstance(self.effective_privacy, PrivacyDecision):
            raise ValueError("invalid managed inference privacy")
        if type(self.max_output_bytes) is not int or self.max_output_bytes < 1:
            raise ValueError("invalid managed inference output limit")
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1:
            raise ValueError("invalid managed inference timeout")


@dataclass(frozen=True, slots=True)
class ManagedSuggestion:
    suggestion_id: str
    workspace_id: str
    source_note_id: str
    source_revision_id: str
    target_note_id: str
    target_revision_id: str
    source_quote: str
    target_quote: str
    provider: ManagedProvider
    model: str

    def __post_init__(self) -> None:
        _portable_id(self.suggestion_id, "suggestion")
        _portable_id(self.workspace_id, "workspace")
        _portable_id(self.source_note_id, "page")
        _portable_id(self.source_revision_id, "revision")
        _portable_id(self.target_note_id, "page")
        _portable_id(self.target_revision_id, "revision")
        if self.source_note_id == self.target_note_id:
            raise ValueError("invalid managed suggestion identity")
        for value in (self.source_quote, self.target_quote, self.model):
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
                raise ValueError("invalid managed suggestion text")
        object.__setattr__(self, "provider", ManagedProvider(self.provider))


@dataclass(frozen=True, slots=True)
class ManagedInferenceReceipt:
    status: str
    request_id: str
    suggestion_id: str | None = None
    duplicate: bool = False

    def __post_init__(self) -> None:
        if self.status not in {
            "cancelled",
            "dispatching",
            "failed",
            "suggestion_accepted",
            "suggestion_recorded",
        }:
            raise ValueError("invalid managed inference receipt")
        _portable_id(self.request_id, "request")
        if self.suggestion_id is not None:
            _portable_id(self.suggestion_id, "suggestion")
        if type(self.duplicate) is not bool:
            raise ValueError("invalid managed inference duplicate marker")


class MarkdownImportFailure(RuntimeError):
    """Bounded import failure whose details are safe for machine output."""

    def __init__(self, code: str, *, details: Mapping[str, object] | None = None) -> None:
        if code not in {
            "import_confirmation_required",
            "import_directory_unavailable",
            "import_root_changed",
            "import_scan_incomplete",
            "invalid_privacy_manifest",
            "large_vault_confirmation_required",
            "overlapping_import_root",
        }:
            raise ValueError("invalid Markdown import failure")
        super().__init__(code)
        self.code = code
        self.details = MappingProxyType(dict(details or {}))


class MarkdownImportCancelled(RuntimeError):
    """The owner declined a new-root import before any import state was written."""


class MarkdownImportInterrupted(RuntimeError):
    """Import stopped at a safe point before missing-path finalization."""


_MAX_PRIVACY_MANIFEST_BYTES = 65_536


def _manifest_root_path(key: object) -> str:
    """Validate one manifest key as an exact repository-relative root path."""
    if (
        not isinstance(key, str)
        or not key
        or key.startswith("/")
        or "\\" in key
        or any(component in {"", ".", ".."} for component in key.split("/"))
    ):
        raise ValueError("invalid privacy manifest root")
    return key


def _manifest_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate privacy manifest key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CapturePrivacyManifest:
    """A validated owner per-root privacy policy for one Markdown import.

    The manifest is one small JSON object passed by absolute path whose exact
    keys are repository-relative root paths mapping to one privacy tier each.
    Unknown keys, unknown tiers, paths outside the import root, and
    overlapping roots are rejected before any note is imported.
    """

    roots: Mapping[str, PrivacyTier]

    def __post_init__(self) -> None:
        if not isinstance(self.roots, Mapping):
            raise ValueError("invalid privacy manifest")
        for key, tier in self.roots.items():
            _manifest_root_path(key)
            if not isinstance(tier, PrivacyTier):
                raise ValueError("invalid privacy manifest")

    @classmethod
    def load(cls, path: str | Path) -> CapturePrivacyManifest:
        location = Path(path)
        if not location.is_absolute():
            raise MarkdownImportFailure(
                "invalid_privacy_manifest", details={"reason": "relative_path"}
            )
        try:
            raw = location.read_bytes()
        except OSError:
            raise MarkdownImportFailure(
                "invalid_privacy_manifest", details={"reason": "unreadable"}
            ) from None
        if len(raw) > _MAX_PRIVACY_MANIFEST_BYTES:
            raise MarkdownImportFailure("invalid_privacy_manifest", details={"reason": "too_large"})
        try:
            document = json.loads(raw.decode("utf-8"), object_pairs_hook=_manifest_object_pairs)
        except UnicodeDecodeError:
            raise MarkdownImportFailure(
                "invalid_privacy_manifest", details={"reason": "invalid_json"}
            ) from None
        except json.JSONDecodeError:
            raise MarkdownImportFailure(
                "invalid_privacy_manifest", details={"reason": "invalid_json"}
            ) from None
        except ValueError:
            # Duplicate keys surface through the object-pairs hook.
            raise MarkdownImportFailure(
                "invalid_privacy_manifest", details={"reason": "duplicate_key"}
            ) from None
        if not isinstance(document, dict):
            raise MarkdownImportFailure(
                "invalid_privacy_manifest", details={"reason": "invalid_shape"}
            )
        roots: dict[str, PrivacyTier] = {}
        for key, value in document.items():
            try:
                normalized_key = _manifest_root_path(key)
            except ValueError:
                raise MarkdownImportFailure(
                    "invalid_privacy_manifest", details={"reason": "invalid_root_path"}
                ) from None
            try:
                roots[normalized_key] = PrivacyTier(value)
            except TypeError, ValueError:
                raise MarkdownImportFailure(
                    "invalid_privacy_manifest", details={"reason": "unknown_tier"}
                ) from None
        ordered = sorted(roots)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if right.startswith(left + "/"):
                raise MarkdownImportFailure(
                    "invalid_privacy_manifest", details={"reason": "overlapping_roots"}
                )
        return cls(roots=MappingProxyType(roots))

    def tier_for(self, relative_path: str) -> PrivacyTier | None:
        """The most specific matching root's tier, or None when nothing matches."""
        best: tuple[str, PrivacyTier] | None = None
        for root, tier in self.roots.items():
            if (relative_path == root or relative_path.startswith(root + "/")) and (
                best is None or len(root) > len(best[0])
            ):
                best = (root, tier)
        return None if best is None else best[1]


@dataclass(frozen=True, slots=True)
class MarkdownImportPreflight:
    canonical_path: Path
    selected_markdown_files: int
    aggregate_bytes: int

    def __post_init__(self) -> None:
        if not self.canonical_path.is_absolute():
            raise ValueError("invalid Markdown import preflight")
        for value in (self.selected_markdown_files, self.aggregate_bytes):
            if type(value) is not int or value < 0:
                raise ValueError("invalid Markdown import preflight")


@dataclass(frozen=True, slots=True)
class MarkdownImportProgress:
    visited_entries: int
    processed_markdown_files: int

    def __post_init__(self) -> None:
        for value in (self.visited_entries, self.processed_markdown_files):
            if type(value) is not int or value < 0:
                raise ValueError("invalid Markdown import progress")


@dataclass(frozen=True, slots=True)
class MarkdownImportEntry:
    outcome: str
    path: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {
            "failed",
            "imported",
            "missing",
            "skipped",
            "unchanged",
            "updated",
        }:
            raise ValueError("invalid Markdown import outcome")
        if not isinstance(self.path, str) or not self.path or len(self.path) > _MAX_TEXT:
            raise ValueError("invalid Markdown import path")
        if self.reason is not None and self.reason not in {
            "dot_directory",
            "file_changed",
            "file_too_large",
            "hardlink",
            "invalid_content",
            "invalid_path",
            "invalid_utf8",
            "non_markdown",
            "path_collision",
            "special_file",
            "symlink",
            "unreadable",
        }:
            raise ValueError("invalid Markdown import reason")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"outcome": self.outcome, "path": self.path}
        if self.reason is not None:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True, slots=True)
class MarkdownImportSummary:
    entries: tuple[MarkdownImportEntry, ...]
    entries_omitted: int
    failed: int
    imported: int
    missing: int
    missing_finalized: bool
    selected: int
    skipped: int
    unchanged: int
    updated: int

    def __post_init__(self) -> None:
        if len(self.entries) > 100:
            raise ValueError("invalid Markdown import summary")
        for value in (
            self.entries_omitted,
            self.failed,
            self.imported,
            self.missing,
            self.selected,
            self.skipped,
            self.unchanged,
            self.updated,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("invalid Markdown import summary")
        if type(self.missing_finalized) is not bool:
            raise ValueError("invalid Markdown import summary")

    @property
    def status(self) -> str:
        return "partial" if self.failed else "completed"

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "entries_omitted": self.entries_omitted,
            "failed": self.failed,
            "history_retained_after_source_removal": True,
            "imported": self.imported,
            "missing": self.missing,
            "missing_finalized": self.missing_finalized,
            "selected": self.selected,
            "skipped": self.skipped,
            "status": self.status,
            "unchanged": self.unchanged,
            "updated": self.updated,
        }


@dataclass(frozen=True, slots=True)
class LocalEngineContext:
    """Engine-owned values supplied by a deployment profile compiler."""

    root: Path
    root_identity: tuple[int, int]
    tenant_id: str
    owner_actor_id: str
    owner_role_claim: Mapping[str, object]
    provider_mode: ProviderMode
    starter_spaces: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.root, Path)
            or not self.root.is_absolute()
            or not isinstance(self.root_identity, tuple)
            or len(self.root_identity) != 2
            or any(type(value) is not int or value < 0 for value in self.root_identity)
        ):
            raise ValueError("invalid local root identity")


@dataclass(frozen=True, slots=True)
class PublicProvenance(Mapping[str, object]):
    """Metadata-safe provenance returned by public retrieval capabilities."""

    capture_id: str
    source_origin: str
    source_record_id: str | None = None
    capture_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _portable_id(self.capture_id, "capture")
        if not self.capture_ids:
            object.__setattr__(self, "capture_ids", (self.capture_id,))
        if (
            not isinstance(self.capture_ids, tuple)
            or not 1 <= len(self.capture_ids) <= 32
            or len(set(self.capture_ids)) != len(self.capture_ids)
            or self.capture_ids[0] != self.capture_id
        ):
            raise ValueError("invalid public source identities")
        for capture_id in self.capture_ids:
            _portable_id(capture_id, "capture")
        if self.source_record_id is None:
            object.__setattr__(self, "source_record_id", self.capture_id)
        else:
            _portable_id(self.source_record_id, "capture")
        if self.source_origin not in {
            ContentOrigin.OWNER_AUTHORED.value,
            ContentOrigin.THIRD_PARTY.value,
            ContentOrigin.MIXED.value,
            ContentOrigin.UNKNOWN.value,
        }:
            raise ValueError("invalid public source origin")

    def as_dict(self) -> dict[str, object]:
        source_record_id = self.source_record_id
        if source_record_id is None:
            raise RuntimeError("public provenance is unavailable")
        result: dict[str, object] = {
            "capture_id": self.capture_id,
            "source_origin": self.source_origin,
            "source_record_id": source_record_id,
        }
        if len(self.capture_ids) > 1:
            result["capture_ids"] = list(self.capture_ids)
        return result

    def __getitem__(self, key: str) -> object:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


@dataclass(frozen=True, slots=True)
class PublicJobCaptureContext:
    """Profile-bound, capture-only identity injected into a public job adapter."""

    tenant_id: str
    actor_id: str
    role_claim: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _portable_id(self.tenant_id, "tenant"))
        object.__setattr__(self, "actor_id", _portable_id(self.actor_id, "actor"))
        object.__setattr__(
            self,
            "role_claim",
            _capture_role_claim(self.role_claim, tenant_id=self.tenant_id, actor_id=self.actor_id),
        )

    @classmethod
    def create(
        cls,
        *,
        profile: LocalEngineContext,
        actor_id: str,
        role_claim: Mapping[str, object],
    ) -> PublicJobCaptureContext:
        context = cls(
            tenant_id=profile.tenant_id,
            actor_id=actor_id,
            role_claim=role_claim,
        )
        context.validate_profile(profile)
        return context

    def validate_profile(self, profile: LocalEngineContext) -> None:
        if self.tenant_id != profile.tenant_id:
            raise ValueError("public-job tenant does not match the local profile")
        if (
            self.actor_id == profile.owner_actor_id
            or self.role_claim == profile.owner_role_claim
            or self.role_claim["role_id"] == profile.owner_role_claim["role_id"]
            or self.role_claim["role_claim_id"] == profile.owner_role_claim["role_claim_id"]
        ):
            raise ValueError("public-job context cannot use an owner role")
        capabilities = self.role_claim["capabilities"]
        if capabilities != ("capture.accept",):
            raise ValueError("public-job role has unsupported authority")


class CaptureAdmissionResult(StrEnum):
    """Stable refusal values for bounded capture admission; no partial state exists."""

    ENVELOPE_TOO_LARGE = "envelope_too_large"
    BODY_TOO_LARGE = "body_too_large"
    RATE_LIMITED = "rate_limited"
    ADMISSION_BUSY = "admission_busy"
    WRITER_QUEUE_FULL = "writer_queue_full"
    STORAGE_HIGH = "storage_high"
    STORAGE_CRITICAL = "storage_critical"
    TIER_NOT_PERMITTED = "tier_not_permitted"


_RETRYABLE_ADMISSION_RESULTS = frozenset(
    {
        CaptureAdmissionResult.RATE_LIMITED,
        CaptureAdmissionResult.ADMISSION_BUSY,
        CaptureAdmissionResult.WRITER_QUEUE_FULL,
        CaptureAdmissionResult.STORAGE_HIGH,
    }
)


class CaptureAdmissionError(ValueError):
    """A capture request refused before any record, revision, blob, or receipt exists."""

    def __init__(self, result: CaptureAdmissionResult) -> None:
        self._result = CaptureAdmissionResult(result)
        super().__init__(f"capture admission refused: {self._result.value}")

    @property
    def result(self) -> CaptureAdmissionResult:
        """The stable refusal value bound to this rejection."""
        return self._result

    @property
    def retryable(self) -> bool:
        """True when an identical retry may later be admitted unchanged."""
        return self._result in _RETRYABLE_ADMISSION_RESULTS


@dataclass(frozen=True, slots=True)
class AdmissionLimits:
    """Validated capture admission bounds with safe non-zero local defaults."""

    max_envelope_bytes: int = 8 * 1024 * 1024
    max_body_bytes: int = 4 * 1024 * 1024
    requests_per_minute_per_principal: int = 120
    max_concurrent_admissions: int = 8
    max_writer_waiters: int = 16
    storage_high_free_bytes: int = 2 * 1024 * 1024 * 1024
    storage_critical_free_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_envelope_bytes",
            "max_body_bytes",
            "requests_per_minute_per_principal",
            "max_concurrent_admissions",
            "max_writer_waiters",
            "storage_high_free_bytes",
            "storage_critical_free_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("invalid admission limits")
        if not self.storage_critical_free_bytes < self.storage_high_free_bytes:
            raise ValueError("invalid admission limits")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_envelope_bytes": self.max_envelope_bytes,
            "max_body_bytes": self.max_body_bytes,
            "requests_per_minute_per_principal": self.requests_per_minute_per_principal,
            "max_concurrent_admissions": self.max_concurrent_admissions,
            "max_writer_waiters": self.max_writer_waiters,
            "storage_high_free_bytes": self.storage_high_free_bytes,
            "storage_critical_free_bytes": self.storage_critical_free_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AdmissionLimits:
        if not isinstance(value, Mapping) or set(value) != {
            "max_envelope_bytes",
            "max_body_bytes",
            "requests_per_minute_per_principal",
            "max_concurrent_admissions",
            "max_writer_waiters",
            "storage_high_free_bytes",
            "storage_critical_free_bytes",
        }:
            raise ValueError("invalid admission limits")
        return cls(**cast(dict[str, Any], dict(value)))


@dataclass(frozen=True, slots=True)
class CaptureSubmission:
    """One versioned capture request for an owner or injected public-job capability."""

    payload: Payload
    delivery_id: str
    source_origin: ContentOrigin
    source_reference: str
    provenance: Provenance
    privacy: PrivacyDecision
    tenant_id: str
    actor_id: str
    role_claim: Mapping[str, object]
    action: CaptureAction = CaptureAction.QUICK
    space_id: str | None = None
    intent: Intent | None = None
    capture_why: str | None = None
    capture_why_origin: CaptureWhyOrigin = CaptureWhyOrigin.AUTOMATION_ABSENT
    title: str | None = None
    occurrence_at: str | None = None
    schema_version: int = 1
    submission_path: CaptureSubmissionPath = CaptureSubmissionPath.OWNER
    # The destination-bound path alone carries the trusted authority binding;
    # every other path leaves both unset and its digest bytes unchanged.
    destination_brain_id: str | None = None
    issuer_epoch: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.payload,
            TextPayload | ReferencePayload | FilePayload | EventPayload | MeasurementPayload,
        ):
            raise ValueError("invalid capture payload")
        _delivery_id(self.delivery_id)
        object.__setattr__(self, "action", CaptureAction(self.action))
        object.__setattr__(self, "submission_path", CaptureSubmissionPath(self.submission_path))
        if self.schema_version != 1:
            raise ValueError("invalid capture submission schema version")
        if (self.destination_brain_id is None) != (self.issuer_epoch is None):
            raise ValueError("invalid destination binding")
        if self.destination_brain_id is not None:
            if self.submission_path is not CaptureSubmissionPath.DESTINATION_BOUND:
                raise ValueError("destination binding requires the destination-bound path")
            if (
                re.fullmatch(r"brn_[a-z2-7]{26}", self.destination_brain_id) is None
                or type(self.issuer_epoch) is not int
            ):
                raise ValueError("invalid destination binding")
        try:
            source_origin = ContentOrigin(self.source_origin)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid source origin") from error
        if source_origin not in {
            ContentOrigin.OWNER_AUTHORED,
            ContentOrigin.THIRD_PARTY,
            ContentOrigin.UNKNOWN,
        }:
            raise ValueError("invalid source origin")
        object.__setattr__(self, "source_origin", source_origin)
        source_reference = _text(self.source_reference, field="source reference", maximum=_MAX_TEXT)
        object.__setattr__(self, "source_reference", source_reference)
        if not isinstance(self.provenance, Provenance):
            raise ValueError("invalid provenance")
        if self.provenance.source_ref != source_reference:
            raise ValueError("capture provenance does not match the source reference")
        if self.provenance.content_origin is not source_origin:
            raise ValueError("capture provenance does not match the source origin")
        if not isinstance(self.privacy, PrivacyDecision):
            raise ValueError("invalid privacy")
        object.__setattr__(self, "tenant_id", _portable_id(self.tenant_id, "tenant"))
        object.__setattr__(self, "actor_id", _portable_id(self.actor_id, "actor"))
        object.__setattr__(
            self,
            "role_claim",
            _capture_role_claim(self.role_claim, tenant_id=self.tenant_id, actor_id=self.actor_id),
        )
        if self.space_id is not None:
            _portable_id(self.space_id, "space")
        intent = _intent(self.intent)
        object.__setattr__(self, "intent", intent)
        capture_why = _optional_text(self.capture_why, field="capture reason", maximum=_MAX_REASON)
        try:
            capture_why_origin = CaptureWhyOrigin(self.capture_why_origin)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid capture reason origin") from error
        if capture_why_origin is CaptureWhyOrigin.OWNER_AUTHORED:
            if capture_why is None or self.provenance.owner_context is not capture_why_origin:
                raise ValueError("invalid capture reason origin")
        elif capture_why is not None or self.provenance.owner_context is not capture_why_origin:
            raise ValueError("invalid capture reason origin")
        object.__setattr__(self, "capture_why", capture_why)
        object.__setattr__(self, "capture_why_origin", capture_why_origin)
        object.__setattr__(
            self,
            "title",
            _optional_text(self.title, field="title", maximum=200),
        )
        occurrence_at = _optional_timestamp(self.occurrence_at)
        payload_occurrence_at = (
            self.payload.occurrence_at
            if isinstance(self.payload, EventPayload | MeasurementPayload)
            else None
        )
        if occurrence_at != payload_occurrence_at:
            raise ValueError("capture occurrence must match the payload")
        object.__setattr__(self, "occurrence_at", occurrence_at)
        if self.submission_path in {
            CaptureSubmissionPath.PUBLIC_JOB,
            CaptureSubmissionPath.DESTINATION_BOUND,
        }:
            if source_origin not in {ContentOrigin.THIRD_PARTY, ContentOrigin.UNKNOWN}:
                raise ValueError("public-job source origin is not allowed")
            if self.action is not CaptureAction.QUICK:
                raise ValueError("public-job capture cannot use canonical-note authority")
            if self.space_id is not None:
                raise ValueError("public-job capture cannot route to a space")
            if self.intent not in {None, Intent.REFERENCE, Intent.HOLD}:
                raise ValueError("public-job capture cannot assign an owner intent")

    @classmethod
    def for_local_owner(
        cls,
        *,
        profile: LocalEngineContext,
        payload: Payload,
        delivery_id: str,
        action: CaptureAction = CaptureAction.QUICK,
        space_id: str | None = None,
        intent: Intent | str | None = None,
        capture_why: str | None = None,
        title: str | None = None,
        privacy_tier: PrivacyTier | str | None = None,
    ) -> CaptureSubmission:
        payload_bytes = portable_canonical_json_bytes(payload.to_dict())
        source_origin = (
            ContentOrigin.THIRD_PARTY
            if isinstance(payload, ReferencePayload)
            else ContentOrigin.OWNER_AUTHORED
        )
        source_reference = (
            payload.url
            if isinstance(payload, ReferencePayload)
            else "urn:open-brain:local:" + sha256(payload_bytes).hexdigest()
        )
        capture_why_origin = (
            CaptureWhyOrigin.OWNER_AUTHORED
            if capture_why is not None
            else CaptureWhyOrigin.AUTOMATION_ABSENT
        )
        occurrence_at = (
            payload.occurrence_at
            if isinstance(payload, EventPayload | MeasurementPayload)
            else None
        )
        return cls(
            payload=payload,
            delivery_id=delivery_id,
            source_origin=source_origin,
            source_reference=source_reference,
            provenance=Provenance.create(
                source_ref=source_reference,
                content_origin=source_origin,
                owner_context=capture_why_origin,
            ),
            privacy=(
                _local_privacy() if privacy_tier is None else owner_privacy_for_tier(privacy_tier)
            ),
            tenant_id=profile.tenant_id,
            actor_id=profile.owner_actor_id,
            role_claim=_role_claim(profile),
            action=action,
            space_id=space_id,
            intent=_intent(intent),
            capture_why=capture_why,
            capture_why_origin=capture_why_origin,
            title=title,
            occurrence_at=occurrence_at,
        )

    @classmethod
    def for_public_job(
        cls,
        *,
        context: PublicJobCaptureContext,
        payload: Payload,
        delivery_id: str,
        source_origin: ContentOrigin | str,
        source_reference: str,
        provenance: Provenance,
        privacy: PrivacyDecision,
        intent: Intent | str | None = None,
        title: str | None = None,
    ) -> CaptureSubmission:
        if not isinstance(context, PublicJobCaptureContext):
            raise ValueError("invalid public-job context")
        occurrence_at = (
            payload.occurrence_at
            if isinstance(payload, EventPayload | MeasurementPayload)
            else None
        )
        return cls(
            payload=payload,
            delivery_id=delivery_id,
            source_origin=ContentOrigin(source_origin),
            source_reference=source_reference,
            provenance=provenance,
            privacy=privacy,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            role_claim=context.role_claim,
            intent=_intent(intent),
            capture_why_origin=CaptureWhyOrigin.AUTOMATION_ABSENT,
            title=title,
            occurrence_at=occurrence_at,
            submission_path=CaptureSubmissionPath.PUBLIC_JOB,
        )

    @classmethod
    def for_destination_bound(
        cls,
        *,
        profile: LocalEngineContext,
        authority: EffectiveAuthority,
        payload: Payload,
        delivery_id: str,
        requested_tier: PrivacyTier | str | None = None,
        title: str | None = None,
    ) -> CaptureSubmission:
        """Build one destination-bound request under a trusted startup policy.

        The authority's allowed capture tier set is the sole tier authority: a
        missing tier becomes ``unknown``, and a tier outside the set is refused
        here, before any engine call, so a refusal can leave no partial state.
        The requested tier and the authority's destination Brain and issuer
        epoch bindings all enter this path's immutable request digest.
        """
        from .t03_contracts import EffectiveAuthority as _EffectiveAuthority

        if not isinstance(authority, _EffectiveAuthority):
            raise ValueError("invalid destination-bound authority")
        if authority.owner:
            raise ValueError("destination-bound authority cannot be an owner")
        if authority.brain_id is None or authority.issuer_epoch is None:
            raise ValueError("destination-bound authority is not destination bound")
        tier = _privacy_tier(requested_tier)
        if tier is None:
            tier = PrivacyTier.UNKNOWN
        if tier not in authority.allowed_capture_tiers:
            raise CaptureAdmissionError(CaptureAdmissionResult.TIER_NOT_PERMITTED)
        return _destination_bound_submission(
            destination_brain_id=authority.brain_id,
            issuer_epoch=authority.issuer_epoch,
            tenant_id=profile.tenant_id,
            principal_id=authority.principal_id,
            payload=payload,
            delivery_id=delivery_id,
            requested_tier=tier,
            title=title,
        )

    def validate_profile(self, profile: LocalEngineContext) -> None:
        if self.submission_path is CaptureSubmissionPath.OWNER:
            expected = self.for_local_owner(
                profile=profile,
                payload=self.payload,
                delivery_id=self.delivery_id,
                action=self.action,
                space_id=self.space_id,
                intent=self.intent,
                capture_why=self.capture_why,
                title=self.title,
                privacy_tier=self.privacy.tier,
            )
            if self != expected:
                raise ValueError("capture submission does not match the local profile")
            return
        PublicJobCaptureContext(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            role_claim=self.role_claim,
        ).validate_profile(profile)

    def durable_source_origin(self) -> str:
        return "owner" if self.source_origin is ContentOrigin.OWNER_AUTHORED else "third_party"

    @property
    def requested_tier(self) -> PrivacyTier:
        """The tier requested at submission time; admission may still narrow it."""
        return self.privacy.tier

    def request_value(self) -> dict[str, object]:
        """A stable replay value; owner submissions retain the Phase 1 bytes exactly."""
        legacy: dict[str, object] = {
            "action": self.action.value,
            "capture_why": self.capture_why,
            "intent": None if self.intent is None else self.intent.value,
            "payload": self.payload.to_dict(),
            "source_origin": self.durable_source_origin(),
            "space_id": self.space_id,
            "title": self.title,
        }
        if self.submission_path is CaptureSubmissionPath.OWNER:
            return legacy
        remote: dict[str, object] = {
            "actor_id": self.actor_id,
            "capture_why": self.capture_why,
            "capture_why_origin": self.capture_why_origin.value,
            "schema_version": self.schema_version,
            "intent": legacy["intent"],
            "payload": legacy["payload"],
            "privacy": self.privacy.to_dict(),
            "provenance": self.provenance.to_dict(),
            "role_claim": _mutable_role_claim(self.role_claim),
            "source_origin": self.source_origin.value,
            "source_reference": self.source_reference,
            "space_id": self.space_id,
            "tenant_id": self.tenant_id,
            "title": self.title,
        }
        if self.submission_path is CaptureSubmissionPath.DESTINATION_BOUND:
            remote["destination_brain_id"] = self.destination_brain_id
            remote["issuer_epoch"] = self.issuer_epoch
        return remote

    def request_sha256(self) -> str:
        return sha256(portable_canonical_json_bytes(self.request_value())).hexdigest()


_JOURNAL_V1_KEYS = frozenset({"contract_version", "submission", "admitted_privacy"})
_CAPTURE_SUBMISSION_KEYS = frozenset(
    {
        "schema_version",
        "payload",
        "delivery_id",
        "source_origin",
        "source_reference",
        "provenance",
        "privacy",
        "tenant_id",
        "actor_id",
        "role_claim",
        "action",
        "space_id",
        "intent",
        "capture_why",
        "capture_why_origin",
        "title",
        "occurrence_at",
        "submission_path",
        "destination_brain_id",
        "issuer_epoch",
    }
)
_CUSTODY_RECEIPT_KEYS = frozenset(
    {
        "contract_version",
        "status",
        "ingestion_id",
        "brain_id",
        "issuer_epoch",
        "delivery_id",
        "request_sha256",
        "requested_tier",
        "final_admitted_tier",
        "queued_at",
        "protection_acknowledgement",
    }
)
_PRIVACY_RESTRICTIVENESS = {
    PrivacyTier.PUBLIC: 0,
    PrivacyTier.WORK: 1,
    PrivacyTier.PERSONAL: 2,
    PrivacyTier.SECRET: 3,
    PrivacyTier.UNKNOWN: 4,
}


def _exact_mapping(value: object, keys: frozenset[str], *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"invalid {label}")
    return dict(value)


def _journal_payload_value(payload: Payload) -> dict[str, object]:
    value = payload.to_dict()
    if isinstance(payload, FilePayload):
        value["data_base64"] = b64encode(payload.data).decode("ascii")
    return value


def _payload_from_journal_value(value: object) -> Payload:
    payload = _exact_mapping(
        value,
        frozenset(value) if isinstance(value, Mapping) else frozenset(),
        label="journal payload",
    )
    family = payload.get("family")
    try:
        if family == "text" and set(payload) == {"family", "text"}:
            return TextPayload(cast(str, payload["text"]))
        if (
            family == "reference_or_file"
            and payload.get("kind") == "reference"
            and set(payload)
            in (
                {"family", "kind", "url"},
                {"family", "kind", "url", "supplied_text"},
            )
        ):
            return ReferencePayload(
                cast(str, payload["url"]), cast(str | None, payload.get("supplied_text"))
            )
        if (
            family == "reference_or_file"
            and payload.get("kind") == "file"
            and set(payload)
            == {"family", "kind", "file_name", "media_type", "blob_sha256", "data_base64"}
        ):
            encoded = payload["data_base64"]
            if not isinstance(encoded, str):
                raise ValueError("invalid journal file payload")
            data = b64decode(encoded.encode("ascii"), validate=True)
            result = FilePayload(
                cast(str, payload["file_name"]), cast(str, payload["media_type"]), data
            )
            if result.digest != payload["blob_sha256"]:
                raise ValueError("invalid journal file payload")
            return result
        if family == "event" and set(payload) == {
            "family",
            "event_type",
            "occurrence_at",
            "attributes",
        }:
            attributes = payload["attributes"]
            if not isinstance(attributes, list):
                raise ValueError("invalid journal event payload")
            pairs: dict[str, str] = {}
            for row in attributes:
                item = _exact_mapping(row, frozenset({"name", "value"}), label="journal attribute")
                name, text = item["name"], item["value"]
                if not isinstance(name, str) or not isinstance(text, str) or name in pairs:
                    raise ValueError("invalid journal event payload")
                pairs[name] = text
            return EventPayload(
                cast(str, payload["event_type"]), cast(str | None, payload["occurrence_at"]), pairs
            )
        if family == "measurement" and set(payload) == {
            "family",
            "value",
            "unit",
            "occurrence_at",
            "dimensions",
        }:
            dimensions = payload["dimensions"]
            if not isinstance(dimensions, list):
                raise ValueError("invalid journal measurement payload")
            pairs = {}
            for row in dimensions:
                item = _exact_mapping(row, frozenset({"name", "value"}), label="journal dimension")
                name, text = item["name"], item["value"]
                if not isinstance(name, str) or not isinstance(text, str) or name in pairs:
                    raise ValueError("invalid journal measurement payload")
                pairs[name] = text
            return MeasurementPayload(
                cast(str, payload["value"]),
                cast(str, payload["unit"]),
                cast(str | None, payload["occurrence_at"]),
                pairs,
            )
    except BinasciiError, UnicodeError, TypeError, ValueError:
        raise ValueError("invalid journal payload") from None
    raise ValueError("invalid journal payload")


def _journal_submission_value(submission: CaptureSubmission) -> dict[str, object]:
    return {
        "schema_version": submission.schema_version,
        "payload": _journal_payload_value(submission.payload),
        "delivery_id": submission.delivery_id,
        "source_origin": submission.source_origin.value,
        "source_reference": submission.source_reference,
        "provenance": submission.provenance.to_dict(),
        "privacy": submission.privacy.to_dict(),
        "tenant_id": submission.tenant_id,
        "actor_id": submission.actor_id,
        "role_claim": _mutable_role_claim(submission.role_claim),
        "action": submission.action.value,
        "space_id": submission.space_id,
        "intent": None if submission.intent is None else submission.intent.value,
        "capture_why": submission.capture_why,
        "capture_why_origin": submission.capture_why_origin.value,
        "title": submission.title,
        "occurrence_at": submission.occurrence_at,
        "submission_path": submission.submission_path.value,
        "destination_brain_id": submission.destination_brain_id,
        "issuer_epoch": submission.issuer_epoch,
    }


def _submission_from_journal_value(value: object) -> CaptureSubmission:
    data = _exact_mapping(value, _CAPTURE_SUBMISSION_KEYS, label="journal submission")
    try:
        return CaptureSubmission(
            payload=_payload_from_journal_value(data["payload"]),
            delivery_id=cast(str, data["delivery_id"]),
            source_origin=ContentOrigin(cast(str, data["source_origin"])),
            source_reference=cast(str, data["source_reference"]),
            provenance=Provenance.from_dict(cast(Mapping[str, object], data["provenance"])),
            privacy=PrivacyDecision.from_dict(cast(Mapping[str, object], data["privacy"])),
            tenant_id=cast(str, data["tenant_id"]),
            actor_id=cast(str, data["actor_id"]),
            role_claim=cast(Mapping[str, object], data["role_claim"]),
            action=CaptureAction(cast(str, data["action"])),
            space_id=cast(str | None, data["space_id"]),
            intent=cast(str | None, data["intent"]),
            capture_why=cast(str | None, data["capture_why"]),
            capture_why_origin=CaptureWhyOrigin(cast(str, data["capture_why_origin"])),
            title=cast(str | None, data["title"]),
            occurrence_at=cast(str | None, data["occurrence_at"]),
            schema_version=cast(int, data["schema_version"]),
            submission_path=CaptureSubmissionPath(cast(str, data["submission_path"])),
            destination_brain_id=cast(str | None, data["destination_brain_id"]),
            issuer_epoch=cast(int | None, data["issuer_epoch"]),
        )
    except TypeError, ValueError:
        raise ValueError("invalid journal submission") from None


@dataclass(frozen=True, slots=True)
class JournalEnvelope:
    """Exact `journal.v1` bytes for a fully normalized admitted capture."""

    submission: CaptureSubmission
    admitted_privacy: PrivacyDecision
    contract_version: str = "journal.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "journal.v1" or not isinstance(
            self.submission, CaptureSubmission
        ):
            raise ValueError("invalid journal envelope")
        if not isinstance(self.admitted_privacy, PrivacyDecision):
            raise ValueError("invalid journal envelope")
        if (
            _PRIVACY_RESTRICTIVENESS[self.admitted_privacy.tier]
            < _PRIVACY_RESTRICTIVENESS[self.submission.requested_tier]
        ):
            raise ValueError("journal admission cannot widen privacy")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "submission": _journal_submission_value(self.submission),
            "admitted_privacy": self.admitted_privacy.to_dict(),
        }

    def to_bytes(self) -> bytes:
        return portable_canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, value: bytes) -> JournalEnvelope:
        if not isinstance(value, bytes) or not value:
            raise ValueError("invalid journal envelope")
        try:
            decoded = json.loads(
                value.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
            )
            data = _exact_mapping(decoded, _JOURNAL_V1_KEYS, label="journal envelope")
            if data["contract_version"] != "journal.v1":
                raise ValueError("invalid journal envelope")
            result = cls(
                submission=_submission_from_journal_value(data["submission"]),
                admitted_privacy=PrivacyDecision.from_dict(
                    cast(Mapping[str, object], data["admitted_privacy"])
                ),
            )
            if result.to_bytes() != value:
                raise ValueError("invalid journal envelope")
            return result
        except UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError:
            raise ValueError("invalid journal envelope") from None


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CaptureCustodyReceipt:
    """The bounded durable-custody receipt returned before canonical capture."""

    ingestion_id: str
    brain_id: str
    issuer_epoch: int
    delivery_id: str
    request_sha256: str
    requested_tier: PrivacyTier
    final_admitted_tier: PrivacyTier
    queued_at: str
    protection_acknowledgement: None = None
    contract_version: str = "capture-custody.v1"
    status: str = "queued"

    def __post_init__(self) -> None:
        if self.contract_version != "capture-custody.v1" or self.status != "queued":
            raise ValueError("invalid capture custody receipt")
        _portable_id(self.ingestion_id, "ingestion")
        if (
            re.fullmatch(r"brn_[a-z2-7]{26}", self.brain_id) is None
            or type(self.issuer_epoch) is not int
            or self.issuer_epoch <= 0
        ):
            raise ValueError("invalid capture custody receipt")
        _delivery_id(self.delivery_id)
        if (
            not isinstance(self.request_sha256, str)
            or _HEX64.fullmatch(self.request_sha256) is None
        ):
            raise ValueError("invalid capture custody receipt")
        try:
            requested = PrivacyTier(self.requested_tier)
            admitted = PrivacyTier(self.final_admitted_tier)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid capture custody receipt") from error
        if _PRIVACY_RESTRICTIVENESS[admitted] < _PRIVACY_RESTRICTIVENESS[requested]:
            raise ValueError("capture custody receipt widens privacy")
        if (
            _optional_timestamp(self.queued_at) is None
            or self.protection_acknowledgement is not None
        ):
            raise ValueError("invalid capture custody receipt")
        object.__setattr__(self, "requested_tier", requested)
        object.__setattr__(self, "final_admitted_tier", admitted)
        object.__setattr__(self, "queued_at", _optional_timestamp(self.queued_at))

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "status": self.status,
            "ingestion_id": self.ingestion_id,
            "brain_id": self.brain_id,
            "issuer_epoch": self.issuer_epoch,
            "delivery_id": self.delivery_id,
            "request_sha256": self.request_sha256,
            "requested_tier": self.requested_tier.value,
            "final_admitted_tier": self.final_admitted_tier.value,
            "queued_at": self.queued_at,
            "protection_acknowledgement": None,
        }


def verify_capture_custody_receipt(value: Mapping[str, object] | bytes) -> CaptureCustodyReceipt:
    """Decode the closed v1 receipt, rejecting unknown fields and altered bytes."""
    try:
        if isinstance(value, bytes):
            decoded = json.loads(
                value.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys
            )
            if portable_canonical_json_bytes(decoded) != value:
                raise ValueError("noncanonical receipt")
        else:
            decoded = value
        data = _exact_mapping(decoded, _CUSTODY_RECEIPT_KEYS, label="capture custody receipt")
        return CaptureCustodyReceipt(
            ingestion_id=cast(str, data["ingestion_id"]),
            brain_id=cast(str, data["brain_id"]),
            issuer_epoch=cast(int, data["issuer_epoch"]),
            delivery_id=cast(str, data["delivery_id"]),
            request_sha256=cast(str, data["request_sha256"]),
            requested_tier=PrivacyTier(cast(str, data["requested_tier"])),
            final_admitted_tier=PrivacyTier(cast(str, data["final_admitted_tier"])),
            queued_at=cast(str, data["queued_at"]),
            protection_acknowledgement=data["protection_acknowledgement"],
            contract_version=cast(str, data["contract_version"]),
            status=cast(str, data["status"]),
        )
    except UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError:
        raise ValueError("invalid capture custody receipt") from None


type CaptureOutcome = CaptureReceipt | CaptureCustodyReceipt


def _destination_bound_submission(
    *,
    destination_brain_id: str,
    issuer_epoch: int,
    tenant_id: str,
    principal_id: str,
    payload: Payload,
    delivery_id: str,
    requested_tier: PrivacyTier | str | None,
    title: str | None,
) -> CaptureSubmission:
    """The single destination-bound construction shared by every digest caller."""
    tier = _privacy_tier(requested_tier)
    if tier is None:
        tier = PrivacyTier.UNKNOWN
    payload_bytes = portable_canonical_json_bytes(payload.to_dict())
    source_reference = "urn:open-brain:destination:" + sha256(payload_bytes).hexdigest()
    actor_id = "actor_" + _destination_bound_identifier("actor", tenant_id, principal_id)
    occurrence_at = (
        payload.occurrence_at if isinstance(payload, EventPayload | MeasurementPayload) else None
    )
    return CaptureSubmission(
        payload=payload,
        delivery_id=delivery_id,
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=owner_privacy_for_tier(tier),
        tenant_id=tenant_id,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_"
            + _destination_bound_identifier("role-claim", tenant_id, principal_id),
            "role_id": "role_" + _destination_bound_identifier("role", tenant_id, principal_id),
            "tenant_id": tenant_id,
        },
        title=title,
        occurrence_at=occurrence_at,
        submission_path=CaptureSubmissionPath.DESTINATION_BOUND,
        destination_brain_id=destination_brain_id,
        issuer_epoch=issuer_epoch,
    )


def destination_bound_request_sha256(
    *,
    destination_brain_id: str,
    issuer_epoch: int,
    tenant_id: str,
    principal_id: str,
    payload: Payload,
    requested_tier: PrivacyTier | str | None = None,
    title: str | None = None,
) -> str:
    """The one destination-bound request digest, computable without an authority.

    The preimage is exactly the ``request_value()`` of the destination-bound
    submission these inputs produce through ``CaptureSubmission
    .for_destination_bound``: both build through the same shared construction,
    so there is no second preimage. The delivery ID stays outside the digest
    because the destination treats it as the replay dedupe key, not as request
    identity.
    """
    submission = _destination_bound_submission(
        destination_brain_id=destination_brain_id,
        issuer_epoch=issuer_epoch,
        tenant_id=tenant_id,
        principal_id=principal_id,
        payload=payload,
        delivery_id="delivery.destination.request-sha256",
        requested_tier=requested_tier,
        title=title,
    )
    return submission.request_sha256()


class CaptureTask(Protocol):
    def accept(
        self,
        payload: Payload,
        *,
        delivery_id: str,
        action: CaptureAction = CaptureAction.QUICK,
        space_id: str | None = None,
        intent: str | None = None,
        capture_why: str | None = None,
        title: str | None = None,
        privacy_tier: PrivacyTier | None = None,
    ) -> CaptureOutcome: ...

    def submit(self, submission: CaptureSubmission) -> CaptureOutcome: ...

    def public_job_sink(self, context: PublicJobCaptureContext) -> PublicJobCaptureSink: ...


class PublicJobCaptureSink:
    """A capture-only capability for one validated non-owner public-job identity."""

    def __init__(
        self,
        capture: CaptureTask,
        *,
        context: PublicJobCaptureContext,
        brain_fingerprint: str | None = None,
    ) -> None:
        if not isinstance(context, PublicJobCaptureContext) or (
            brain_fingerprint is not None
            and re.fullmatch(r"[0-9a-f]{64}", brain_fingerprint) is None
        ):
            raise ValueError("invalid public-job context")
        self._capture = capture
        self._context = context
        self._brain_fingerprint = brain_fingerprint

    @property
    def context(self) -> PublicJobCaptureContext:
        """Expose only the validated capture actor and role claim bound to this sink."""
        return self._context

    @property
    def brain_fingerprint(self) -> str | None:
        """Opaque identity of the exact Brain that created this capability."""
        return self._brain_fingerprint

    @staticmethod
    def fingerprint_for(root: str, root_identity: tuple[int, int], tenant_id: str) -> str:
        """Bind a sink to the exact Brain tuple using the collector's stable encoding."""
        value = [root, root_identity, tenant_id]
        encoded = json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def submit(
        self,
        payload: Payload,
        *,
        delivery_id: str,
        source_origin: ContentOrigin | str,
        source_reference: str,
        provenance: Provenance,
        privacy: PrivacyDecision,
        intent: Intent | str | None = None,
        title: str | None = None,
    ) -> CaptureOutcome:
        return self._capture.submit(
            CaptureSubmission.for_public_job(
                context=self._context,
                payload=payload,
                delivery_id=delivery_id,
                source_origin=source_origin,
                source_reference=source_reference,
                provenance=provenance,
                privacy=privacy,
                intent=intent,
                title=title,
            )
        )


class InboxSpaceTask(Protocol):
    def list(
        self, *, unassigned_only: bool = False, limit: int | None = None, offset: int = 0
    ) -> tuple[InboxItem, ...]: ...

    def spaces(self, *, limit: int | None = None, offset: int = 0) -> tuple[SpaceRecord, ...]: ...

    def create_space(self, name: str, *, delivery_id: str) -> SpaceRecord: ...

    def rename_space(self, space_id: str, name: str, *, delivery_id: str) -> SpaceRecord: ...

    def route(self, capture_id: str, space_id: str, *, delivery_id: str) -> RoutedCapture: ...


class ReviewTask(Protocol):
    def propose(
        self,
        capture_id: str | Sequence[str],
        drafts: Sequence[ProposalDraft | PatchDraft],
        *,
        delivery_id: str,
        target_page_id: str | None = None,
    ) -> tuple[ProposalRecord, ...]: ...

    def propose_append(
        self,
        capture_id: str | Sequence[str],
        *,
        target_page_id: str,
        append_markdown: str,
        delivery_id: str,
    ) -> tuple[ProposalRecord, ...]: ...

    def list(
        self,
        *,
        capture_id: str | None = None,
        status: str | None = None,
        space_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[ProposalRecord, ...]: ...

    def show(self, proposal_id: str) -> ReviewProposal: ...

    def decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        delivery_id: str,
        edited_markdown: str | None = None,
        expected_review_digest: str | None = None,
    ) -> DecisionRecord: ...


class ScopedRetrievalTask(Protocol):
    def search(
        self,
        query: str,
        *,
        space_id: str | None = None,
        payload_family: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> tuple[RetrievalResult, ...]: ...

    def fetch(self, result_id: str) -> RetrievalResult | None: ...

    def read_page(self, result_id: str) -> PageResult | None: ...


class RetrievalTask(Protocol):
    def search_page(
        self, request: SearchPageRequest, *, authority: EffectiveAuthority
    ) -> SearchPageResponse: ...

    def read_record(
        self, request: RecordReadRequest, *, authority: EffectiveAuthority
    ) -> RecordReadResponse: ...

    def search(
        self,
        query: str,
        *,
        space_id: str | None = None,
        payload_family: str | None = None,
        record_type: str | None = None,
        limit: int = 10,
    ) -> tuple[RetrievalResult, ...]: ...

    def fetch(self, result_id: str) -> RetrievalResult | None: ...

    def read_page(self, result_id: str) -> PageResult | None: ...

    def scoped(self, *, allowed_space_ids: frozenset[str]) -> ScopedRetrievalTask: ...


class PortabilityTask(Protocol):
    def validate(self, source: Path) -> PortabilityReceipt: ...

    def export(
        self,
        destination: Path,
        *,
        export_id: str,
        authority: EffectiveAuthority | None = None,
    ) -> PortabilityReceipt: ...

    def import_clean(
        self, source: Path, destination: Path, *, import_id: str
    ) -> PortabilityReceipt: ...

    def rebuild_index(self) -> PortabilityReceipt: ...


class ReconciliationTask(Protocol):
    def reconcile(self) -> ReconciliationReceipt: ...


class MarkdownImportTask(Protocol):
    def import_directory(
        self,
        directory: str,
        *,
        allow_large_vault: bool = False,
        confirm: Callable[[MarkdownImportPreflight], bool] | None = None,
        progress: Callable[[MarkdownImportProgress], None] | None = None,
        interrupted: Callable[[], bool] | None = None,
        privacy_tier: PrivacyTier | str | None = None,
        privacy_manifest: str | Path | None = None,
    ) -> MarkdownImportSummary: ...


class ManagedWorkspaceTask(Protocol):
    def status(self) -> ManagedWorkspaceStatus | None: ...

    def setup(self, directory: str, *, operation_id: str) -> ManagedWorkspaceReceipt: ...

    def refresh(self, workspace_id: str, *, operation_id: str) -> ManagedWorkspaceReceipt: ...

    def observe(self, workspace_id: str) -> ManagedWorkspaceObservation: ...

    def open_conflicts(
        self, workspace_id: str, *, limit: int = 64
    ) -> tuple[ManagedWorkspaceConflictSummary, ...]: ...

    def review_conflict(
        self, workspace_id: str, note_id: str
    ) -> ManagedWorkspaceConflictReview: ...

    def note_id_for_path(self, workspace_id: str, relative_path: str) -> str: ...

    def graph_snapshot(self, workspace_id: str) -> ManagedGraphSnapshot: ...

    def accept_observed(
        self,
        workspace_id: str,
        note_id: str,
        *,
        generation: int,
        operation_id: str,
    ) -> ManagedWorkspaceReceipt: ...

    def materialize(
        self, workspace_id: str, note_id: str, *, operation_id: str
    ) -> ManagedWorkspaceReceipt: ...

    def deactivate(
        self, workspace_id: str, note_id: str, *, operation_id: str
    ) -> ManagedWorkspaceReceipt: ...

    def restore(
        self, workspace_id: str, note_id: str, *, operation_id: str
    ) -> ManagedWorkspaceReceipt: ...

    def resolve_conflict(
        self,
        workspace_id: str,
        note_id: str,
        choice: str,
        *,
        conflict_id: str | None = None,
        operation_id: str,
    ) -> ManagedWorkspaceReceipt: ...


class ManagedPolicyTask(Protocol):
    def active_exclusions(
        self, workspace_id: str, *, limit: int = 64
    ) -> tuple[ManagedExclusion, ...]: ...

    def grant_consent(
        self,
        workspace_id: str,
        provider: ManagedProvider,
        access_mode: ManagedAccessMode,
        *,
        operation_id: str,
    ) -> ManagedPolicyReceipt: ...

    def revoke_consent(
        self,
        workspace_id: str,
        provider: ManagedProvider,
        access_mode: ManagedAccessMode,
        *,
        operation_id: str,
    ) -> ManagedPolicyReceipt: ...

    def set_exclusion(
        self,
        workspace_id: str,
        kind: str,
        subject: str,
        *,
        excluded: bool,
        operation_id: str,
    ) -> ManagedPolicyReceipt: ...


class ManagedInferenceTask(Protocol):
    def suggestions(self, workspace_id: str) -> tuple[ManagedSuggestion, ...]: ...

    def suggestion(self, workspace_id: str, suggestion_id: str) -> ManagedSuggestion: ...

    def prepare(
        self,
        workspace_id: str,
        provider: ManagedProvider,
        access_mode: ManagedAccessMode,
        adapter_identity: str,
        note_ids: tuple[str, ...],
        *,
        request_id: str,
        max_output_bytes: int,
        timeout_seconds: int,
    ) -> ManagedInferenceRequest: ...

    def release(self, request_id: str) -> ManagedInferenceRequest: ...

    def record_suggestion(
        self,
        request_id: str,
        *,
        source_note_id: str,
        target_note_id: str,
        source_quote: str,
        target_quote: str,
        model: str,
    ) -> ManagedSuggestion: ...

    def fail(self, request_id: str) -> ManagedInferenceReceipt: ...

    def accept_suggestion(
        self, workspace_id: str, suggestion_id: str, *, operation_id: str
    ) -> ManagedInferenceReceipt: ...

    def recover_abandoned_sessions(self) -> int: ...


class SourceTask(Protocol):
    def submit_revision(self, submission: SourceRevisionSubmission) -> SourceRevisionReceipt: ...

    def fence_intake(self, *, expected_epoch: int, authority: EffectiveAuthority) -> int: ...

    def route(
        self, request: SourceRouteRequest, *, authority: EffectiveAuthority
    ) -> SourceRouteResponse: ...


class HistoryTask(Protocol):
    def list_history(
        self, request: HistoryListRequest, *, authority: EffectiveAuthority
    ) -> HistoryListResponse: ...

    def read_history(
        self, request: RecordReadRequest, *, authority: EffectiveAuthority
    ) -> RecordReadResponse: ...


class RelationshipTask(Protocol):
    def decide(
        self, request: RelationshipDecideRequest, *, authority: EffectiveAuthority
    ) -> RelationshipDecideResponse: ...
    def list_relationships(
        self, request: RelationshipListRequest, *, authority: EffectiveAuthority
    ) -> RelationshipListResponse: ...
    def list_decisions(
        self, request: DecisionHistoryRequest, *, authority: EffectiveAuthority
    ) -> DecisionHistoryResponse: ...


class PrivacyRepairTask(Protocol):
    def repair_privacy(
        self,
        request: PrivacyRepairRequest,
        *,
        authority: EffectiveAuthority,
    ) -> PrivacyRepairReceipt: ...


@dataclass(frozen=True, slots=True)
class EngineTaskSet:
    """The public task identities exposed by one opened local engine root."""

    profile: LocalEngineContext
    capture: CaptureTask
    inbox: InboxSpaceTask
    review: ReviewTask
    retrieval: RetrievalTask
    portability: PortabilityTask
    reconciliation: ReconciliationTask
    markdown_import: MarkdownImportTask
    managed_workspace: ManagedWorkspaceTask
    managed_policy: ManagedPolicyTask
    managed_inference: ManagedInferenceTask
    sources: SourceTask | None = None
    history: HistoryTask | None = None
    relationships: RelationshipTask | None = None
    privacy_repair: PrivacyRepairTask | None = None

    @property
    def spaces(self) -> InboxSpaceTask:
        return self.inbox


class _LocalEngineOperations:
    """Typed mixin host; the concrete local facade supplies the named operations."""

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def _local_privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(_privacy())


# The G5 canonical-boundary narrowing reasons cover every tier except PUBLIC
# (public can never be a narrowing result); the owner explicit tier admits
# PUBLIC through the closed policy-public reason, so no second mapping of the
# narrowing-covered tiers exists.
_OWNER_EXPLICIT_REASON: dict[PrivacyTier, PrivacyReason] = {
    PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
    **_NARROWED_REASON,
}


def owner_privacy_for_tier(tier: PrivacyTier | str) -> PrivacyDecision:
    """The canonical owner decision for one explicit privacy tier.

    The reason is the per-tier canonical reason (the G5 narrowing mapping plus
    the closed public policy reason), the policy version stays the fixed local
    one, and the local owner path grants no egress authority, which every
    local-only reason forbids anyway.
    """
    normalized = _privacy_tier(tier)
    assert normalized is not None
    return PrivacyDecision.create(
        tier=normalized,
        reason=_OWNER_EXPLICIT_REASON[normalized],
        policy_version=_local_privacy().policy_version,
        authority=Authority(cloud=False, external_egress=False),
    )


def _privacy_tier(value: PrivacyTier | str | None) -> PrivacyTier | None:
    try:
        return None if value is None else PrivacyTier(value)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid privacy tier") from error


def _destination_bound_identifier(domain: str, tenant_id: str, principal_id: str) -> str:
    """One stable UUIDv4-shaped portable identifier for a destination principal."""
    from uuid import UUID

    digest = sha256(
        f"open-brain:destination-bound:{domain}:{tenant_id}:{principal_id}".encode()
    ).digest()
    return str(UUID(bytes=digest[:16], version=4))


def _capture_role_claim(
    value: Mapping[str, object], *, tenant_id: str, actor_id: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "actor_id",
        "capabilities",
        "role_claim_id",
        "role_id",
        "tenant_id",
    }:
        raise ValueError("invalid capture role")
    if value["tenant_id"] != tenant_id or value["actor_id"] != actor_id:
        raise ValueError("capture role does not match its context")
    role_claim_id = value["role_claim_id"]
    role_id = value["role_id"]
    if not isinstance(role_claim_id, str) or not isinstance(role_id, str):
        raise ValueError("invalid capture role")
    _portable_id(role_claim_id, "role_claim")
    _portable_id(role_id, "role")
    capabilities = value["capabilities"]
    if (
        not isinstance(capabilities, tuple | list)
        or any(not isinstance(capability, str) for capability in capabilities)
        or tuple(sorted(set(capabilities))) != tuple(capabilities)
    ):
        raise ValueError("invalid capture role")
    return MappingProxyType(
        {
            "actor_id": actor_id,
            "capabilities": tuple(capabilities),
            "role_claim_id": _portable_id(role_claim_id, "role_claim"),
            "role_id": _portable_id(role_id, "role"),
            "tenant_id": tenant_id,
        }
    )


def _mutable_role_claim(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "actor_id": value["actor_id"],
        "capabilities": list(cast(tuple[str, ...], value["capabilities"])),
        "role_claim_id": value["role_claim_id"],
        "role_id": value["role_id"],
        "tenant_id": value["tenant_id"],
    }


def _intent(value: Intent | str | None) -> Intent | None:
    try:
        return None if value is None else Intent(value)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid intent") from error
