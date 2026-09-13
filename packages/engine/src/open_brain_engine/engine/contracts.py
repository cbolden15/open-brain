"""Public engine values and task contracts."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from html import unescape
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.parse import unquote

from open_brain_engine.core.ids import canonicalize_source_url, portable_canonical_json_bytes
from open_brain_engine.core.models import (
    CaptureWhyOrigin,
    ContentOrigin,
    Intent,
    PrivacyDecision,
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

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CaptureAction(StrEnum):
    QUICK = "quick"
    CANONICAL_NOTE = "canonical_note"


class CaptureSubmissionPath(StrEnum):
    OWNER = "owner"
    PUBLIC_JOB = "public_job"


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


class InjectedFault(RuntimeError):
    """Synthetic process interruption at one named durable boundary."""

    def __init__(self, point: CaptureFault | PortabilityFault) -> None:
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


@dataclass(frozen=True, slots=True)
class InboxItem:
    capture_id: str
    payload_family: str
    state: str
    space_id: str | None
    intent: str | None
    capture_why: str | None


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
    result = _PUBLIC_CREDENTIAL_ASSIGNMENT.sub(
        rf"\1\2{_PUBLIC_CREDENTIAL_MARKER}", result
    )
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


class MarkdownImportFailure(RuntimeError):
    """Bounded import failure whose details are safe for machine output."""

    def __init__(self, code: str, *, details: Mapping[str, object] | None = None) -> None:
        if code not in {
            "import_confirmation_required",
            "import_directory_unavailable",
            "import_root_changed",
            "import_scan_incomplete",
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
class PublicProvenance(Mapping[str, str]):
    """Metadata-safe provenance returned by public retrieval capabilities."""

    capture_id: str
    source_origin: str
    source_record_id: str | None = None

    def __post_init__(self) -> None:
        _portable_id(self.capture_id, "capture")
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

    def as_dict(self) -> dict[str, str]:
        source_record_id = self.source_record_id
        if source_record_id is None:
            raise RuntimeError("public provenance is unavailable")
        return {
            "capture_id": self.capture_id,
            "source_origin": self.source_origin,
            "source_record_id": source_record_id,
        }

    def __getitem__(self, key: str) -> str:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return 3


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
        if self.submission_path is CaptureSubmissionPath.PUBLIC_JOB:
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
            privacy=_local_privacy(),
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
        return {
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

    def request_sha256(self) -> str:
        return sha256(portable_canonical_json_bytes(self.request_value())).hexdigest()


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
    ) -> CaptureReceipt: ...

    def submit(self, submission: CaptureSubmission) -> CaptureReceipt: ...

    def public_job_sink(self, context: PublicJobCaptureContext) -> PublicJobCaptureSink: ...


class PublicJobCaptureSink:
    """A capture-only capability for one validated non-owner public-job identity."""

    def __init__(self, capture: CaptureTask, *, context: PublicJobCaptureContext) -> None:
        if not isinstance(context, PublicJobCaptureContext):
            raise ValueError("invalid public-job context")
        self._capture = capture
        self._context = context

    @property
    def context(self) -> PublicJobCaptureContext:
        """Expose only the validated capture actor and role claim bound to this sink."""
        return self._context

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
    ) -> CaptureReceipt:
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
    def list(self, *, unassigned_only: bool = False) -> tuple[InboxItem, ...]: ...

    def spaces(self) -> tuple[SpaceRecord, ...]: ...

    def create_space(self, name: str, *, delivery_id: str) -> SpaceRecord: ...

    def rename_space(self, space_id: str, name: str, *, delivery_id: str) -> SpaceRecord: ...

    def route(self, capture_id: str, space_id: str, *, delivery_id: str) -> RoutedCapture: ...


class ReviewTask(Protocol):
    def propose(
        self,
        capture_id: str,
        drafts: Sequence[ProposalDraft],
        *,
        delivery_id: str,
    ) -> tuple[ProposalRecord, ...]: ...

    def list(
        self, *, capture_id: str | None = None, status: str | None = None
    ) -> tuple[ProposalRecord, ...]: ...

    def decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        delivery_id: str,
        edited_markdown: str | None = None,
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

    def export(self, destination: Path, *, export_id: str) -> PortabilityReceipt: ...

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
    ) -> MarkdownImportSummary: ...


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

    @property
    def spaces(self) -> InboxSpaceTask:
        return self.inbox


class _LocalEngineOperations:
    """Typed mixin host; the concrete local facade supplies the named operations."""

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def _local_privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(_privacy())


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
