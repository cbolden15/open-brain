"""Provider-neutral contracts for optional operator integrations."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol
from unicodedata import category
from urllib.parse import unquote

if TYPE_CHECKING:
    from .config import IntegrationConfig

_OPAQUE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_FORBIDDEN_OPTIONAL_IMPORT_ROOTS = frozenset(
    {"open_brain", "open_brain_connectors", "open_brain_engine", "open_brain_legacy"}
)
_PUBLIC_TEXT_RESIDUAL_PATTERNS = (
    re.compile(
        r"(?ix)\b(?:api[\s_-]*key|authorization|bearer|credential|password|secret|token)"
        r"\b(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+"
    ),
    re.compile(
        r"(?ix)\b(?:raw|provider|captured)[\s_-]*payload\b"
        r"(?:\s*[:=]\s*|\s+)[^\s,;]+"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,31}://[^\s]+"),
    re.compile(r"(?<![\w.])(?:~)?/(?:[^/\s]+)(?:/[^/\s]+)*"),
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/][^\s]+"),
    re.compile(r"(?<!\\)\\\\[^\\\s]+\\[^\s]+"),
)
_RUNTIME_TRAVERSAL_PATTERN = re.compile(
    r"(?:^|[^A-Za-z0-9_.-])\.\.(?:$|[\\/])"
)
_REDACTED_TEXT = "[redacted]"
_MAX_QUERY_LENGTH = 4096
_MAX_TITLE_LENGTH = 256
_MAX_EXCERPT_LENGTH = 4096
_MAX_MARKDOWN_LENGTH = 65_536
_MAX_RETRIEVAL_RESULTS = 8
_MAX_SYNC_COUNT = 1_000_000
_MAX_RUNTIME_REFS = 64
_MAX_AUDIT_FINDINGS = 16


class IntegrationScope(StrEnum):
    """The data boundary an integration is permitted to serve."""

    WORK = "work"
    PERSONAL = "personal"


class Capability(StrEnum):
    """Stable, provider-neutral names for optional integration seams."""

    FINANCE = "finance"
    MAIL_CALENDAR = "mail_calendar"
    MESSAGING = "messaging"
    RELATIONSHIPS = "relationships"
    LIFE_OS = "life_os"
    DEV_WORKFLOW = "dev_workflow"
    REPOSITORY_IDENTITY = "repository_identity"
    WORK_CONTEXT = "work_context"
    MCP = "mcp"
    UI = "ui"
    OBSIDIAN = "obsidian"
    HOOKS = "hooks"
    SOCIAL_LEARNING = "social_learning"

    @property
    def scope(self) -> IntegrationScope:
        return _CAPABILITY_SCOPES[self]


class OptionalProvider(StrEnum):
    """Closed provider identities backed by declared package extras."""

    OPENAI = "openai"


_CAPABILITY_SCOPES: Mapping[Capability, IntegrationScope] = MappingProxyType(
    {
        Capability.FINANCE: IntegrationScope.PERSONAL,
        Capability.MAIL_CALENDAR: IntegrationScope.PERSONAL,
        Capability.MESSAGING: IntegrationScope.PERSONAL,
        Capability.RELATIONSHIPS: IntegrationScope.PERSONAL,
        Capability.LIFE_OS: IntegrationScope.PERSONAL,
        Capability.DEV_WORKFLOW: IntegrationScope.WORK,
        Capability.REPOSITORY_IDENTITY: IntegrationScope.WORK,
        Capability.WORK_CONTEXT: IntegrationScope.WORK,
        Capability.MCP: IntegrationScope.WORK,
        Capability.UI: IntegrationScope.WORK,
        Capability.OBSIDIAN: IntegrationScope.WORK,
        Capability.HOOKS: IntegrationScope.WORK,
        Capability.SOCIAL_LEARNING: IntegrationScope.WORK,
    }
)

_SYNC_CAPABILITIES = frozenset(
    {
        Capability.FINANCE,
        Capability.MAIL_CALENDAR,
        Capability.MESSAGING,
        Capability.RELATIONSHIPS,
        Capability.LIFE_OS,
        Capability.DEV_WORKFLOW,
        Capability.SOCIAL_LEARNING,
    }
)


class UnavailableReason(StrEnum):
    """Safe reasons an optional integration cannot run."""

    DISABLED = "disabled"
    OPTIONAL_DEPENDENCY = "optional_dependency"
    LOAD_FAILURE = "load_failure"


@dataclass(frozen=True, slots=True)
class IntegrationOutcome:
    """A fixed-schema availability result with no provider-defined payload."""

    available: bool
    capability: Capability
    reason: UnavailableReason | None = None

    def __post_init__(self) -> None:
        if (
            type(self.available) is not bool
            or not isinstance(self.capability, Capability)
            or (self.reason is not None and not isinstance(self.reason, UnavailableReason))
            or self.available != (self.reason is None)
        ):
            raise ValueError("invalid integration outcome")

    @classmethod
    def available_for(cls, *, capability: Capability) -> IntegrationOutcome:
        return cls(available=True, capability=capability)

    @classmethod
    def unavailable(
        cls, *, capability: Capability, reason: UnavailableReason
    ) -> IntegrationOutcome:
        return cls(available=False, capability=capability, reason=reason)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "available": self.available,
            "capability": self.capability.value,
        }
        if self.reason is not None:
            result["reason"] = self.reason.value
        return result


class TrustLabel(StrEnum):
    """Stable trust labels that survive retrieval and presentation adapters."""

    VERIFIED_WORK = "verified_work"
    UNREVIEWED_THIRD_PARTY = "unreviewed_third_party"


class RedactionPolicyVersion(StrEnum):
    """Closed public-text redaction policy versions."""

    V1 = "public_text_v1"


@dataclass(frozen=True, slots=True, init=False)
class RedactionReceipt:
    """Immutable proof that a closed factory produced one public text value."""

    policy_version: RedactionPolicyVersion
    source_digest: str
    text_digest: str
    redaction_count: int

    def __init__(self, *_: object, **__: object) -> None:
        raise TypeError("RedactionReceipt must be created by the redaction factory")

    @classmethod
    def _create(cls, *, source: str) -> RedactionReceipt:
        if not _is_input_text(source, maximum=_MAX_MARKDOWN_LENGTH):
            raise ValueError("invalid public text source")
        text, redaction_count = _sanitize_public_text(source)
        receipt = object.__new__(cls)
        object.__setattr__(receipt, "policy_version", RedactionPolicyVersion.V1)
        object.__setattr__(receipt, "source_digest", _sha256(source))
        object.__setattr__(receipt, "text_digest", _sha256(text))
        object.__setattr__(receipt, "redaction_count", redaction_count)
        return receipt

    def verify(self, *, source: str, text: str) -> bool:
        if not _is_input_text(source, maximum=_MAX_MARKDOWN_LENGTH):
            return False
        sanitized_text, redaction_count = _sanitize_public_text(source)
        return (
            self.policy_version is RedactionPolicyVersion.V1
            and self.source_digest == _sha256(source)
            and text == sanitized_text
            and self.text_digest == _sha256(text)
            and self.redaction_count == redaction_count
        )

    def verifies_text(self, text: str) -> bool:
        return (
            self.policy_version is RedactionPolicyVersion.V1
            and self.text_digest == _sha256(text)
            and self.redaction_count in {0, 1}
            and not _contains_public_text_residual(text)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version.value,
            "source_digest": self.source_digest,
            "text_digest": self.text_digest,
            "redaction_count": self.redaction_count,
        }


@dataclass(frozen=True, slots=True, init=False)
class RedactedText:
    """Bounded public text that can only be created by the closed redaction factory."""

    text: str
    receipt: RedactionReceipt

    def __init__(self, *_: object, **__: object) -> None:
        raise TypeError("RedactedText must be created by the redaction factory")

    @classmethod
    def redact(cls, source: str) -> RedactedText:
        if not _is_input_text(source, maximum=_MAX_MARKDOWN_LENGTH):
            raise ValueError("invalid public text source")
        text, _ = _sanitize_public_text(source)
        value = object.__new__(cls)
        object.__setattr__(value, "text", text)
        object.__setattr__(
            value,
            "receipt",
            RedactionReceipt._create(source=source),
        )
        return value

    def to_dict(self) -> dict[str, object]:
        if not _is_redacted_text(self, maximum=_MAX_MARKDOWN_LENGTH):
            raise ValueError("invalid redacted text")
        return {
            "text": self.text,
            "redaction_receipt": self.receipt.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """A bounded, work-only retrieval request."""

    question: str
    limit: int = 5
    scope: IntegrationScope = IntegrationScope.WORK

    def __post_init__(self) -> None:
        if (
            not _is_input_text(self.question, maximum=_MAX_QUERY_LENGTH)
            or not _is_int_in_range(self.limit, minimum=1, maximum=_MAX_RETRIEVAL_RESULTS)
            or self.scope is not IntegrationScope.WORK
        ):
            raise ValueError("invalid retrieval request")


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """A bounded and redacted work retrieval result."""

    result_id: str
    rank: int
    title: RedactedText
    excerpt: RedactedText
    trust: TrustLabel

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.result_id)
            or not _is_int_in_range(self.rank, minimum=1, maximum=_MAX_RETRIEVAL_RESULTS)
            or not _is_redacted_text(self.title, maximum=_MAX_TITLE_LENGTH)
            or not _is_redacted_text(self.excerpt, maximum=_MAX_EXCERPT_LENGTH)
            or not isinstance(self.trust, TrustLabel)
        ):
            raise ValueError("invalid retrieval hit")

    def to_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "rank": self.rank,
            "title": self.title.to_dict(),
            "excerpt": self.excerpt.to_dict(),
            "trust": self.trust.value,
        }


@dataclass(frozen=True, slots=True)
class RetrievalBatch:
    """A fixed-size work-only retrieval result set."""

    retrieval_id: str
    hits: tuple[RetrievalHit, ...]
    truncated: bool

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.retrieval_id)
            or not isinstance(self.hits, tuple)
            or len(self.hits) > _MAX_RETRIEVAL_RESULTS
            or any(not isinstance(hit, RetrievalHit) for hit in self.hits)
            or tuple(hit.rank for hit in self.hits) != tuple(range(1, len(self.hits) + 1))
            or len({hit.result_id for hit in self.hits}) != len(self.hits)
            or type(self.truncated) is not bool
        ):
            raise ValueError("invalid retrieval batch")

    def to_dict(self) -> dict[str, object]:
        return {
            "retrieval_id": self.retrieval_id,
            "scope": IntegrationScope.WORK.value,
            "results": [hit.to_dict() for hit in self.hits],
            "truncated": self.truncated,
        }


class WorkRetriever(Protocol):
    """Work-only retrieval boundary used by MCP, UI, and context adapters."""

    def search(self, request: RetrievalRequest) -> RetrievalBatch: ...


class FeedbackOutcome(StrEnum):
    """Allow-listed metadata-only retrieval feedback outcomes."""

    USED = "used"
    CITED = "cited"
    IGNORED = "ignored"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class RetrievalFeedbackRequest:
    """Metadata-only feedback; questions and result text are intentionally absent."""

    retrieval_id: str
    outcome: FeedbackOutcome
    result_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.retrieval_id)
            or not isinstance(self.outcome, FeedbackOutcome)
            or not _is_opaque_id_tuple(self.result_ids, maximum=_MAX_RETRIEVAL_RESULTS)
        ):
            raise ValueError("invalid retrieval feedback request")


@dataclass(frozen=True, slots=True)
class RetrievalFeedbackReceipt:
    """Stable structural acknowledgement for retrieval feedback."""

    retrieval_id: str
    outcome: FeedbackOutcome
    result_count: int
    recorded: bool = True

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.retrieval_id)
            or not isinstance(self.outcome, FeedbackOutcome)
            or not _is_int_in_range(
                self.result_count, minimum=0, maximum=_MAX_RETRIEVAL_RESULTS
            )
            or type(self.recorded) is not bool
        ):
            raise ValueError("invalid retrieval feedback receipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "retrieval_id": self.retrieval_id,
            "outcome": self.outcome.value,
            "recorded": self.recorded,
            "result_count": self.result_count,
        }


class RetrievalFeedback(Protocol):
    """Metadata-only feedback sink for retrieval adapters."""

    def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt: ...


@dataclass(frozen=True, slots=True)
class PageReadRequest:
    """Read a logical page without exposing a filesystem path."""

    page_id: str

    def __post_init__(self) -> None:
        if not _is_opaque_id(self.page_id):
            raise ValueError("invalid page read request")


@dataclass(frozen=True, slots=True)
class PageDocument:
    """A bounded, redacted page returned through a public adapter."""

    page_id: str
    title: RedactedText
    markdown: RedactedText
    trust: TrustLabel

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.page_id)
            or not _is_redacted_text(self.title, maximum=_MAX_TITLE_LENGTH)
            or not _is_redacted_text(self.markdown, maximum=_MAX_MARKDOWN_LENGTH)
            or not isinstance(self.trust, TrustLabel)
        ):
            raise ValueError("invalid page document")

    def to_dict(self) -> dict[str, object]:
        return {
            "page_id": self.page_id,
            "title": self.title.to_dict(),
            "markdown": self.markdown.to_dict(),
            "trust": self.trust.value,
        }


class PageReader(Protocol):
    """Read-only logical page boundary used by UI and vault adapters."""

    def read(self, request: PageReadRequest) -> PageDocument | None: ...


class VaultWriteDisposition(StrEnum):
    """Allow-listed results for review-bound vault writes."""

    CREATED = "created"
    UPDATED = "updated"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class VaultWriteRequest:
    """A logical vault write that is bound to an approved review."""

    page_id: str
    title: str
    markdown: str
    review_id: str

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.page_id)
            or not _is_input_text(self.title, maximum=_MAX_TITLE_LENGTH)
            or not _is_input_text(self.markdown, maximum=_MAX_MARKDOWN_LENGTH)
            or not _is_opaque_id(self.review_id)
        ):
            raise ValueError("invalid vault write request")


@dataclass(frozen=True, slots=True)
class VaultWriteResult:
    """A structural result with no path or document payload."""

    page_id: str
    disposition: VaultWriteDisposition
    bytes_written: int

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.page_id)
            or not isinstance(self.disposition, VaultWriteDisposition)
            or not _is_int_in_range(
                self.bytes_written, minimum=0, maximum=_MAX_MARKDOWN_LENGTH * 4
            )
        ):
            raise ValueError("invalid vault write result")

    def to_dict(self) -> dict[str, object]:
        return {
            "page_id": self.page_id,
            "disposition": self.disposition.value,
            "bytes_written": self.bytes_written,
        }


class VaultAdapter(PageReader, Protocol):
    """Logical, root-confined vault boundary for later concrete adapters."""

    def write(self, request: VaultWriteRequest) -> VaultWriteResult: ...


class SyncStatus(StrEnum):
    """Stable structural outcomes for optional provider synchronization."""

    COMPLETED = "completed"
    DRY_RUN = "dry_run"
    RETRYABLE = "retryable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ProviderSyncRequest:
    """Typed provider sync input with opaque resource and cursor references."""

    capability: Capability
    resource_ref: str
    cursor_ref: str | None
    dry_run: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capability, Capability)
            or self.capability not in _SYNC_CAPABILITIES
            or not _is_opaque_id(self.resource_ref)
            or (self.cursor_ref is not None and not _is_opaque_id(self.cursor_ref))
            or type(self.dry_run) is not bool
        ):
            raise ValueError("invalid provider sync request")


@dataclass(frozen=True, slots=True)
class ProviderSyncResult:
    """Allow-listed sync counts and status; provider payloads stay behind the port."""

    capability: Capability
    status: SyncStatus
    created: int
    updated: int
    removed: int
    next_cursor_ref: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capability, Capability)
            or self.capability not in _SYNC_CAPABILITIES
            or not isinstance(self.status, SyncStatus)
            or any(
                not _is_int_in_range(count, minimum=0, maximum=_MAX_SYNC_COUNT)
                for count in (self.created, self.updated, self.removed)
            )
            or (
                self.next_cursor_ref is not None
                and not _is_opaque_id(self.next_cursor_ref)
            )
        ):
            raise ValueError("invalid provider sync result")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "capability": self.capability.value,
            "status": self.status.value,
            "created": self.created,
            "updated": self.updated,
            "removed": self.removed,
        }
        if self.next_cursor_ref is not None:
            result["next_cursor_ref"] = self.next_cursor_ref
        return result


class ProviderSync(Protocol):
    """Shared typed sync boundary for optional provider adapters."""

    def sync(self, request: ProviderSyncRequest) -> ProviderSyncResult: ...


class ReviewWriteKind(StrEnum):
    """Review-bound work write categories."""

    JOURNAL = "journal"
    PROPOSAL = "proposal"
    GOTCHA = "gotcha"


class ReviewDisposition(StrEnum):
    """Stable structural results for review-bound writes."""

    QUEUED = "queued"
    DUPLICATE = "duplicate"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ReviewWriteRequest:
    """An opaque content reference bound to an existing review."""

    request_id: str
    kind: ReviewWriteKind
    content_ref: str
    review_id: str

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.request_id)
            or not isinstance(self.kind, ReviewWriteKind)
            or not _is_opaque_id(self.content_ref)
            or not _is_opaque_id(self.review_id)
        ):
            raise ValueError("invalid review write request")


@dataclass(frozen=True, slots=True)
class ReviewWriteResult:
    """A review-bound structural write result."""

    request_id: str
    disposition: ReviewDisposition
    review_id: str

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.request_id)
            or not isinstance(self.disposition, ReviewDisposition)
            or not _is_opaque_id(self.review_id)
        ):
            raise ValueError("invalid review write result")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "disposition": self.disposition.value,
            "review_id": self.review_id,
        }


class ReviewBoundWriter(Protocol):
    """Work writes that cannot bypass an approved review reference."""

    def submit(self, request: ReviewWriteRequest) -> ReviewWriteResult: ...


class HookKind(StrEnum):
    """Allow-listed hook kinds."""

    POST_COMMIT = "post_commit"


class HookSignalStatus(StrEnum):
    """Stable non-blocking hook signal outcomes."""

    EMITTED = "emitted"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PostCommitSignal:
    """Content-minimal post-commit metadata."""

    signal_id: str
    repository_id: str
    revision_id: str

    def __post_init__(self) -> None:
        if not all(
            _is_opaque_id(value)
            for value in (self.signal_id, self.repository_id, self.revision_id)
        ):
            raise ValueError("invalid post-commit signal")


@dataclass(frozen=True, slots=True)
class HookEmitResult:
    """A redacted hook emission result."""

    signal_id: str
    status: HookSignalStatus

    def __post_init__(self) -> None:
        if not _is_opaque_id(self.signal_id) or not isinstance(
            self.status, HookSignalStatus
        ):
            raise ValueError("invalid hook emit result")

    def to_dict(self) -> dict[str, object]:
        return {"signal_id": self.signal_id, "status": self.status.value}


class PostCommitSignalPort(Protocol):
    """Injected best-effort boundary receiving bounded opaque commit metadata."""

    def emit(self, signal: PostCommitSignal) -> HookEmitResult: ...


class HookInstallStatus(StrEnum):
    """Allow-listed hook installation outcomes."""

    PLANNED = "planned"
    INSTALLED = "installed"
    ALREADY_INSTALLED = "already_installed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class HookInstallRequest:
    """A path-free hook request; non-dry-run use requires a bound capability."""

    repository_id: str
    hook_kind: HookKind
    dry_run: bool = True

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.repository_id)
            or not isinstance(self.hook_kind, HookKind)
            or type(self.dry_run) is not bool
        ):
            raise ValueError("invalid hook install request")


@dataclass(frozen=True, slots=True)
class HookInstallResult:
    """A path-free structural hook installation result."""

    repository_id: str
    hook_kind: HookKind
    status: HookInstallStatus

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.repository_id)
            or not isinstance(self.hook_kind, HookKind)
            or not isinstance(self.status, HookInstallStatus)
        ):
            raise ValueError("invalid hook install result")

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "hook_kind": self.hook_kind.value,
            "status": self.status.value,
        }


class HookInstaller(Protocol):
    """Dry-run-first, capability-confined installation boundary."""

    def install(self, request: HookInstallRequest) -> HookInstallResult: ...


class RuntimeField(StrEnum):
    """Manifest fields an external-runtime audit may classify."""

    EXECUTABLE = "executable"
    ARGUMENT = "argument"
    WORKING_DIRECTORY = "working_directory"
    REFERENCED_FILE = "referenced_file"


class AuditFindingCode(StrEnum):
    """Stable redacted external-runtime audit finding codes."""

    FORBIDDEN_REFERENCE = "forbidden_reference"
    OUTSIDE_ALLOWED_ROOT = "outside_allowed_root"
    INVALID_MANIFEST = "invalid_manifest"


class AuditDisposition(StrEnum):
    """Closed external-runtime audit outcomes."""

    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Typed private audit input; it has no public serializer."""

    manifest_id: str
    executable_ref: str
    argument_refs: tuple[str, ...]
    working_directory_ref: str
    referenced_file_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.manifest_id)
            or not _is_runtime_ref(self.executable_ref)
            or not _is_runtime_ref_tuple(self.argument_refs)
            or not _is_runtime_ref(self.working_directory_ref)
            or not _is_runtime_ref_tuple(self.referenced_file_refs)
        ):
            raise ValueError("invalid runtime manifest")


@dataclass(frozen=True, slots=True)
class RuntimeAuditRequest:
    """Private allow/deny roots paired with one synthetic or configured manifest."""

    manifest: RuntimeManifest
    forbidden_roots: tuple[str, ...]
    allowed_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest, RuntimeManifest)
            or not _is_runtime_ref_tuple(self.forbidden_roots)
            or not _is_runtime_ref_tuple(self.allowed_roots)
            or not self.allowed_roots
        ):
            raise ValueError("invalid runtime audit request")


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """A stable finding that omits the sensitive matched value."""

    field: RuntimeField
    code: AuditFindingCode

    def __post_init__(self) -> None:
        if not isinstance(self.field, RuntimeField) or not isinstance(
            self.code, AuditFindingCode
        ):
            raise ValueError("invalid audit finding")

    def to_dict(self) -> dict[str, object]:
        return {"field": self.field.value, "code": self.code.value}


@dataclass(frozen=True, slots=True)
class RuntimeAuditResult:
    """A bounded external-runtime audit result with redacted findings."""

    audit_id: str
    disposition: AuditDisposition
    findings: tuple[AuditFinding, ...]

    def __post_init__(self) -> None:
        if (
            not _is_opaque_id(self.audit_id)
            or not isinstance(self.disposition, AuditDisposition)
            or not isinstance(self.findings, tuple)
            or len(self.findings) > _MAX_AUDIT_FINDINGS
            or any(not isinstance(finding, AuditFinding) for finding in self.findings)
            or (self.disposition is AuditDisposition.ALLOWED and bool(self.findings))
            or (self.disposition is AuditDisposition.DENIED and not self.findings)
        ):
            raise ValueError("invalid runtime audit result")

    def to_dict(self) -> dict[str, object]:
        return {
            "audit_id": self.audit_id,
            "disposition": self.disposition.value,
            "findings": [finding.to_dict() for finding in self.findings],
        }


class ExternalRuntimeAudit(Protocol):
    """Read-only inspection boundary; implementations never execute manifests."""

    def inspect(self, request: RuntimeAuditRequest) -> RuntimeAuditResult: ...


def _load_openai_provider() -> object:
    import openai  # type: ignore[import-not-found]  # noqa: PLC0415

    return openai


_OPTIONAL_PROVIDER_LOADERS: Mapping[OptionalProvider, Callable[[], object]] = MappingProxyType(
    {OptionalProvider.OPENAI: _load_openai_provider}
)


@dataclass(frozen=True, slots=True)
class OptionalIntegrationMetadata:
    """Optional-provider metadata with fail-closed lazy loading."""

    capability: Capability
    provider: OptionalProvider

    def __post_init__(self) -> None:
        if not isinstance(self.capability, Capability) or not isinstance(
            self.provider, OptionalProvider
        ):
            raise ValueError("invalid optional integration metadata")

    @property
    def scope(self) -> IntegrationScope:
        return self.capability.scope

    def load(self, *, config: IntegrationConfig | None = None) -> IntegrationOutcome:
        """Load only explicitly enabled packages and redact every load failure."""
        if config is None:
            return IntegrationOutcome.unavailable(
                capability=self.capability,
                reason=UnavailableReason.DISABLED,
            )

        from .config import IntegrationConfig

        if not isinstance(config, IntegrationConfig):
            raise ValueError("invalid integration configuration")
        if not config.live_adapter_enabled(self.capability):
            return IntegrationOutcome.unavailable(
                capability=self.capability,
                reason=UnavailableReason.DISABLED,
            )

        try:
            _loaded_optional_provider(self.provider)
        except ModuleNotFoundError:
            return IntegrationOutcome.unavailable(
                capability=self.capability,
                reason=UnavailableReason.OPTIONAL_DEPENDENCY,
            )
        except (Exception, SystemExit):
            return IntegrationOutcome.unavailable(
                capability=self.capability,
                reason=UnavailableReason.LOAD_FAILURE,
            )
        return IntegrationOutcome.available_for(capability=self.capability)


def _loaded_optional_provider(provider: OptionalProvider) -> object:
    if (
        not isinstance(provider, OptionalProvider)
        or provider.value.partition(".")[0] in _FORBIDDEN_OPTIONAL_IMPORT_ROOTS
        or (loader := _OPTIONAL_PROVIDER_LOADERS.get(provider)) is None
    ):
        raise ValueError("invalid optional integration provider")
    return loader()


@dataclass(frozen=True, slots=True)
class SyntheticIntegrationAdapter:
    """A deterministic local-only availability adapter for contract tests."""

    capability: Capability
    outcome: IntegrationOutcome

    def __post_init__(self) -> None:
        if (
            not isinstance(self.capability, Capability)
            or not isinstance(self.outcome, IntegrationOutcome)
            or self.outcome.capability is not self.capability
        ):
            raise ValueError("invalid synthetic integration adapter")

    @property
    def scope(self) -> IntegrationScope:
        return self.capability.scope

    def availability(self) -> IntegrationOutcome:
        return self.outcome


def _is_opaque_id(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID_PATTERN.fullmatch(value) is not None


def _is_input_text(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and not _has_unsafe_control(value)
    )


def _is_redacted_text(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, RedactedText)
        and 0 < len(value.text) <= maximum
        and isinstance(value.receipt, RedactionReceipt)
        and not _contains_public_text_residual(value.text)
        and value.receipt.verifies_text(value.text)
    )


def _sha256(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _decoded_variants(value: str) -> tuple[str, ...]:
    variants = [value]
    for _ in range(3):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            break
        variants.append(decoded)
    return tuple(variants)


def _contains_public_text_residual(value: str) -> bool:
    return any(
        pattern.search(variant) is not None
        for variant in _decoded_variants(value)
        for pattern in _PUBLIC_TEXT_RESIDUAL_PATTERNS
    )


def _sanitize_public_text(source: str) -> tuple[str, int]:
    if _contains_public_text_residual(source):
        return _REDACTED_TEXT, 1
    return source, 0


def _has_unsafe_control(value: str) -> bool:
    return any(ord(character) < 32 and character not in {"\n", "\t"} for character in value)


def _is_int_in_range(value: object, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _is_opaque_id_tuple(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) <= maximum
        and all(_is_opaque_id(item) for item in value)
        and len(set(value)) == len(value)
    )


def _is_runtime_ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 4096
        and all(
            not any(category(character) == "Cc" for character in variant)
            and _RUNTIME_TRAVERSAL_PATTERN.search(variant) is None
            for variant in _decoded_variants(value)
        )
    )


def _is_runtime_ref_tuple(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) <= _MAX_RUNTIME_REFS
        and all(_is_runtime_ref(item) for item in value)
    )
