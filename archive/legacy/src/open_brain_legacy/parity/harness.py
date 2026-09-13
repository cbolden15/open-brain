"""Closed, metadata-only normalization for synthetic Phase 7 parity evidence."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import ClassVar, Protocol

PARITY_HARNESS_VERSION = "phase7-wave0-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID = re.compile(r"^(?P<prefix>[a-z][a-z0-9]{1,31})_[0-9a-f]{16,64}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+.][a-z0-9][a-z0-9.-]*)?$")
_DENIED_ID_PREFIXES = frozenset(
    {
        "credential",
        "host",
        "hostname",
        "ip",
        "password",
        "path",
        "private",
        "secret",
        "token",
        "topology",
        "url",
    }
)


class ParityValidationError(ValueError):
    """Synthetic parity metadata or authority failed closed validation."""


class ParityFacet(StrEnum):
    """Closed Phase 7 comparison facet identifiers."""

    REQUEST_CONTENT = "PAR7-001"
    RAW_FILE_SET = "PAR7-002"
    QUEUE_RETRY = "PAR7-003"
    FRONTMATTER_PROVENANCE = "PAR7-004"
    ROUTING = "PAR7-005"
    LEDGER_CITATIONS = "PAR7-006"
    REVIEW_PROPOSALS = "PAR7-007"
    CLI_JSON = "PAR7-008"
    HEALTH_DOCTOR = "PAR7-009"
    SHADOW_OBSERVATION = "PAR7-010"


P7_W0_FACETS = (
    ParityFacet.REQUEST_CONTENT,
    ParityFacet.RAW_FILE_SET,
    ParityFacet.QUEUE_RETRY,
    ParityFacet.FRONTMATTER_PROVENANCE,
    ParityFacet.ROUTING,
    ParityFacet.LEDGER_CITATIONS,
    ParityFacet.REVIEW_PROPOSALS,
    ParityFacet.CLI_JSON,
    ParityFacet.HEALTH_DOCTOR,
)
P7_W1_SHADOW_VERSION = "phase7-wave1-shadow-v1"
P7_W1_SHADOW_FACETS = (ParityFacet.SHADOW_OBSERVATION,)


class ParitySide(StrEnum):
    LEGACY = "legacy"
    OPEN_BRAIN = "open-brain"


class EvidenceScope(StrEnum):
    SYNTHETIC = "synthetic"
    LIVE = "live"


class ComparisonOutcome(StrEnum):
    """Public comparisons either match or remain owner-gated."""

    MATCH = "match"
    BLOCKED_DIFFERENCE = "blocked-difference"


class ShadowExtractionClass(StrEnum):
    COMPLETE = "complete"
    PENDING = "pending"
    NO_CONTENT = "no_content"
    REJECTED = "rejected"
    FAILED = "failed"


class ShadowProviderClass(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    LOCAL = "local"
    CLOUD = "cloud"


class ShadowResourceClass(StrEnum):
    WITHIN_LIMIT = "within_limit"
    LIMIT_REACHED = "limit_reached"
    UNAVAILABLE = "unavailable"


class ShadowRedactionClass(StrEnum):
    CLEAN = "clean"
    REDACTED = "redacted"
    FAILED = "failed"
    RAW_RESIDUE = "raw_residue"


class RequestStatus(StrEnum):
    UNAVAILABLE = "unavailable"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    REJECTED = "rejected"


class QueueState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    QUARANTINED = "quarantined"
    ACKNOWLEDGED = "acknowledged"


class QueueErrorClass(StrEnum):
    INVALID_ITEM = "invalid_item"
    INVALID_DIGEST = "invalid_digest"
    INVALID_SCHEMA = "invalid_schema"
    IMMUTABLE_CONFLICT = "immutable_conflict"
    DURABILITY_FAILED = "durability_failed"
    RETRYABLE_FAILURE = "retryable_failure"
    RETRY_EXHAUSTED = "retry_exhausted"
    PRIVACY_HOLD = "privacy_hold"
    REDACTION_FAILED = "redaction_failed"
    EXTRACTION_FAILED = "extraction_failed"


class ContentKind(StrEnum):
    EVENT = "event"
    ARTICLE = "article"
    PRODUCT = "product"
    PLACE = "place"
    POST = "post"
    VIDEO = "video"
    OTHER = "other"


class PrivacyTier(StrEnum):
    PUBLIC = "public"
    WORK = "work"
    PERSONAL = "personal"
    SECRET = "secret"
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    YOUTUBE = "youtube"
    SOCIAL = "social"
    WEB = "web"
    TEXT = "text"


class ContentOrigin(StrEnum):
    OWNER_AUTHORED = "owner_authored"
    THIRD_PARTY = "third_party"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class OwnerContext(StrEnum):
    OWNER_AUTHORED = "owner_authored"
    AUTOMATION_ABSENT = "automation_absent"


class RoutingDestination(StrEnum):
    UNAVAILABLE = "unavailable"
    WORK = "work"
    PERSONAL = "personal"
    HOLD = "hold"
    QUARANTINE = "quarantine"
    REVIEW = "review"


class ReviewProposalState(StrEnum):
    OPEN = "open"
    APPLIED = "applied"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    BLOCKED = "blocked"


class ReviewActorKind(StrEnum):
    OWNER = "owner"
    SYSTEM = "system"


class ReviewIntent(StrEnum):
    IDEA = "idea"
    ACTION_CANDIDATE = "action_candidate"


class CliCommand(StrEnum):
    CAPTURE = "capture"
    CONFIG = "config"
    CRON = "cron"
    DIGEST = "digest"
    DOCTOR = "doctor"
    EXPLAIN = "explain"
    LEDGER = "ledger"
    MIGRATE = "migrate"
    OKF = "okf"
    PROPOSALS = "proposals"
    QUERY = "query"
    REGISTRY = "registry"
    RETENTION = "retention"
    REVIEW = "review"
    SHARE = "share"
    SOCIAL = "social"
    STATUS = "status"


class CliProfile(StrEnum):
    OPEN_BRAIN_STATUS = "open-brain-status"
    OPEN_BRAIN_CRON = "open-brain-cron"
    BRAIN_SYSTEM_STATUS = "brain-system-status"
    SUMMARIZER_CRON = "summarizer-cron"


class CliStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    DEFERRED = "deferred"
    INVALID = "invalid"


class CliExitClass(IntEnum):
    SUCCESS = 0
    FAILURE = 1
    USAGE = 2
    DEFERRED = 3
    LOCK_HELD = 75
    CONFIGURATION = 78


class DoctorProbe(StrEnum):
    CONFIGURATION = "configuration"
    QUEUE_AGE = "queue-age"
    SCHEMA = "schema"
    WRITER_OWNERSHIP = "writer-ownership"
    LOCK_STATE = "lock-state"
    BACKUP_EVIDENCE = "backup-evidence"
    STALE_REFERENCES = "stale-references"
    OPTIONAL_PROVIDER = "optional-provider"


class DoctorProbeState(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "unavailable"


class HealthFindingClass(StrEnum):
    CONFIGURATION_INVALID = "configuration-invalid"
    QUEUE_STALE = "queue-stale"
    SCHEMA_MISMATCH = "schema-mismatch"
    WRITER_OWNERSHIP_CONFLICT = "writer-ownership-conflict"
    LOCK_UNHEALTHY = "lock-unhealthy"
    BACKUP_EVIDENCE_MISSING = "backup-evidence-missing"
    STALE_REFERENCE = "stale-reference"
    OPTIONAL_PROVIDER_UNREADY = "optional-provider-unready"
    CONFIGURATION_UNAVAILABLE = "configuration-unavailable"
    QUEUE_AGE_UNAVAILABLE = "queue-age-unavailable"
    SCHEMA_UNAVAILABLE = "schema-unavailable"
    WRITER_OWNERSHIP_UNAVAILABLE = "writer-ownership-unavailable"
    LOCK_STATE_UNAVAILABLE = "lock-state-unavailable"
    BACKUP_EVIDENCE_UNAVAILABLE = "backup-evidence-unavailable"
    STALE_REFERENCES_UNAVAILABLE = "stale-references-unavailable"
    OPTIONAL_PROVIDER_UNAVAILABLE = "optional-provider-unavailable"
    PROBE_TIMEOUT = "probe-timeout"
    PROBE_FAILURE = "probe-failure"
    HISTORICAL_NONZERO = "historical-nonzero"


class HealthOutcome(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "unavailable"
    DIAGNOSIS_REQUIRED = "diagnosis-required"


QUEUE_STATE_MAP = MappingProxyType(
    {
        "pending": QueueState.PENDING,
        "processing": QueueState.PROCESSING,
        "quarantined": QueueState.QUARANTINED,
        "acknowledged": QueueState.ACKNOWLEDGED,
    }
)
QUEUE_ERROR_MAP = MappingProxyType(
    {
        "invalid_item": QueueErrorClass.INVALID_ITEM,
        "invalid_digest": QueueErrorClass.INVALID_DIGEST,
        "invalid_schema": QueueErrorClass.INVALID_SCHEMA,
        "immutable_conflict": QueueErrorClass.IMMUTABLE_CONFLICT,
        "durability_failed": QueueErrorClass.DURABILITY_FAILED,
        "retryable_failure": QueueErrorClass.RETRYABLE_FAILURE,
        "retry_exhausted": QueueErrorClass.RETRY_EXHAUSTED,
        "privacy_hold": QueueErrorClass.PRIVACY_HOLD,
        "redaction_failed": QueueErrorClass.REDACTION_FAILED,
        "extraction_failed": QueueErrorClass.EXTRACTION_FAILED,
    }
)
CONTENT_KIND_MAP = MappingProxyType(
    {
        "event": ContentKind.EVENT,
        "article": ContentKind.ARTICLE,
        "product": ContentKind.PRODUCT,
        "place": ContentKind.PLACE,
        "post": ContentKind.POST,
        "video": ContentKind.VIDEO,
        "other": ContentKind.OTHER,
    }
)
PRIVACY_TIER_MAP = MappingProxyType(
    {
        "public": PrivacyTier.PUBLIC,
        "work": PrivacyTier.WORK,
        "personal": PrivacyTier.PERSONAL,
        "secret": PrivacyTier.SECRET,
        "unknown": PrivacyTier.UNKNOWN,
    }
)
SOURCE_TYPE_MAP = MappingProxyType(
    {
        "youtube": SourceKind.YOUTUBE,
        "social": SourceKind.SOCIAL,
        "web": SourceKind.WEB,
        "text": SourceKind.TEXT,
    }
)
CONTENT_ORIGIN_MAP = MappingProxyType(
    {
        "owner_authored": ContentOrigin.OWNER_AUTHORED,
        "third_party": ContentOrigin.THIRD_PARTY,
        "mixed": ContentOrigin.MIXED,
        "unknown": ContentOrigin.UNKNOWN,
    }
)
OWNER_CONTEXT_MAP = MappingProxyType(
    {
        "owner_authored": OwnerContext.OWNER_AUTHORED,
        "automation_absent": OwnerContext.AUTOMATION_ABSENT,
    }
)
REVIEW_STATE_MAP = MappingProxyType(
    {
        "open": ReviewProposalState.OPEN,
        "applied": ReviewProposalState.APPLIED,
        "rejected": ReviewProposalState.REJECTED,
        "deferred": ReviewProposalState.DEFERRED,
        "blocked": ReviewProposalState.BLOCKED,
    }
)
REVIEW_ACTOR_MAP = MappingProxyType(
    {
        "owner": ReviewActorKind.OWNER,
        "system": ReviewActorKind.SYSTEM,
    }
)
REVIEW_INTENT_MAP = MappingProxyType(
    {
        "idea": ReviewIntent.IDEA,
        "action_candidate": ReviewIntent.ACTION_CANDIDATE,
    }
)
CLI_EXIT_CLASS_MAP = MappingProxyType(
    {
        0: CliExitClass.SUCCESS,
        1: CliExitClass.FAILURE,
        2: CliExitClass.USAGE,
        3: CliExitClass.DEFERRED,
        75: CliExitClass.LOCK_HELD,
        78: CliExitClass.CONFIGURATION,
    }
)
DOCTOR_PROBE_MAP = MappingProxyType(
    {
        "configuration": DoctorProbe.CONFIGURATION,
        "queue-age": DoctorProbe.QUEUE_AGE,
        "schema": DoctorProbe.SCHEMA,
        "writer-ownership": DoctorProbe.WRITER_OWNERSHIP,
        "lock-state": DoctorProbe.LOCK_STATE,
        "backup-evidence": DoctorProbe.BACKUP_EVIDENCE,
        "stale-references": DoctorProbe.STALE_REFERENCES,
        "optional-provider": DoctorProbe.OPTIONAL_PROVIDER,
    }
)
DOCTOR_STATE_MAP = MappingProxyType(
    {
        "healthy": DoctorProbeState.HEALTHY,
        "unhealthy": DoctorProbeState.UNHEALTHY,
        "unavailable": DoctorProbeState.UNAVAILABLE,
    }
)
DOCTOR_FINDING_MAP = MappingProxyType(
    {
        "configuration-invalid": HealthFindingClass.CONFIGURATION_INVALID,
        "queue-stale": HealthFindingClass.QUEUE_STALE,
        "schema-mismatch": HealthFindingClass.SCHEMA_MISMATCH,
        "writer-ownership-conflict": HealthFindingClass.WRITER_OWNERSHIP_CONFLICT,
        "lock-unhealthy": HealthFindingClass.LOCK_UNHEALTHY,
        "backup-evidence-missing": HealthFindingClass.BACKUP_EVIDENCE_MISSING,
        "stale-reference": HealthFindingClass.STALE_REFERENCE,
        "optional-provider-unready": HealthFindingClass.OPTIONAL_PROVIDER_UNREADY,
        "configuration-unavailable": HealthFindingClass.CONFIGURATION_UNAVAILABLE,
        "queue-age-unavailable": HealthFindingClass.QUEUE_AGE_UNAVAILABLE,
        "schema-unavailable": HealthFindingClass.SCHEMA_UNAVAILABLE,
        "writer-ownership-unavailable": HealthFindingClass.WRITER_OWNERSHIP_UNAVAILABLE,
        "lock-state-unavailable": HealthFindingClass.LOCK_STATE_UNAVAILABLE,
        "backup-evidence-unavailable": HealthFindingClass.BACKUP_EVIDENCE_UNAVAILABLE,
        "stale-references-unavailable": HealthFindingClass.STALE_REFERENCES_UNAVAILABLE,
        "optional-provider-unavailable": HealthFindingClass.OPTIONAL_PROVIDER_UNAVAILABLE,
        "probe-timeout": HealthFindingClass.PROBE_TIMEOUT,
        "probe-failure": HealthFindingClass.PROBE_FAILURE,
        "historical-nonzero": HealthFindingClass.HISTORICAL_NONZERO,
    }
)
HEALTH_OUTCOME_MAP = MappingProxyType(
    {
        "healthy": HealthOutcome.HEALTHY,
        "unhealthy": HealthOutcome.UNHEALTHY,
        "unavailable": HealthOutcome.UNAVAILABLE,
        "diagnosis-required": HealthOutcome.DIAGNOSIS_REQUIRED,
    }
)

_CLI_OUTPUT_KEYS = (
    "action",
    "action_count",
    "attempts",
    "backup_id",
    "candidate_count",
    "capture_id",
    "captures",
    "canonical",
    "checks",
    "claim_count",
    "cloud_enabled",
    "code",
    "command",
    "commands",
    "configuration",
    "decision_id",
    "disposition",
    "dry_run",
    "duplicate",
    "egress_enabled",
    "entry_count",
    "enrichment_state",
    "error",
    "event_count",
    "excerpt",
    "explanation",
    "findings",
    "held_count",
    "historical_diagnoses",
    "ledger",
    "ledger_route_count",
    "manifest_digest",
    "manifest_digest_sha256",
    "manifest_id",
    "message",
    "metrics",
    "migrate",
    "name",
    "network",
    "network_access",
    "output",
    "output_mode",
    "owner_gated",
    "page_id",
    "payload_family",
    "pipeline",
    "plan",
    "policy",
    "privacy_tier",
    "proposal_id",
    "proposals",
    "proposed_intent",
    "protected_count",
    "provider",
    "provenance",
    "publication_id",
    "rank",
    "ranked_claim_ids",
    "reason",
    "record_count",
    "record_type",
    "redacted",
    "redacted_count",
    "reject",
    "removed_count",
    "replayed",
    "request_id",
    "required_evidence",
    "restored_count",
    "result_id",
    "results",
    "retrieval_id",
    "review",
    "review_id",
    "reviews",
    "role",
    "run_count",
    "runs",
    "schema_version",
    "slug",
    "social",
    "source_ref_sha256",
    "source_type",
    "space_id",
    "spaces",
    "staged_digest_sha256",
    "state",
    "status",
    "strict",
    "tier",
    "title",
    "truncated",
    "trust",
    "window_seconds",
)
CLI_OUTPUT_KEY_MAP = MappingProxyType({key: key for key in _CLI_OUTPUT_KEYS})
CLI_PROFILE_FIELDS = MappingProxyType(
    {
        CliProfile.OPEN_BRAIN_STATUS: frozenset(
            {"command", "metrics", "schema_version", "status", "strict"}
        ),
        CliProfile.OPEN_BRAIN_CRON: frozenset(
            {"command", "run_count", "runs", "status", "window_seconds"}
        ),
        CliProfile.BRAIN_SYSTEM_STATUS: frozenset(
            {
                "capture_daily",
                "review_open",
                "index_age_seconds",
                "cron_failures",
                "cron_incidents_24h",
                "event_backlog",
                "event_backlog_ids",
                "stale_reviews",
                "backup_state",
                "retrieval",
            }
        ),
        CliProfile.SUMMARIZER_CRON: frozenset(),
    }
)

QUEUE_WORK_ITEM_FIELD_MAP = MappingProxyType(
    {
        "schema_version": "excluded:queue-schema-version-manifest-bound",
        "envelope": "excluded:raw-capture-payload",
        "available_at": "excluded:retry-schedule-time",
        "attempt_count": "normalized:QueueTransition.attempt_count",
        "last_error_code": "normalized:QueueTransition.last_error_code",
    }
)
PRIVACY_DECISION_FIELD_MAP = MappingProxyType(
    {
        "tier": "normalized:FrontmatterProvenanceMetadata.privacy_tier",
        "reason": "excluded:privacy-classification-reason",
        "policy_version": "excluded:privacy-policy-version",
        "authority": "excluded:runtime-authority-capability",
        "confirmation_ref": "excluded:owner-confirmation-reference",
    }
)
PROVENANCE_FIELD_MAP = MappingProxyType(
    {
        "source_ref": ("digest:FrontmatterProvenanceMetadata.source_ref_digest_sha256"),
        "content_origin": "normalized:FrontmatterProvenanceMetadata.content_origin",
        "owner_context": "normalized:FrontmatterProvenanceMetadata.owner_context",
    }
)
CAPTURE_ENVELOPE_FIELD_MAP = MappingProxyType(
    {
        "schema_version": "normalized:FrontmatterProvenanceMetadata.schema_version",
        "capture_id": "opaque:RequestContentMetadata.content_ids",
        "source_type": "normalized:FrontmatterProvenanceMetadata.source_kind",
        "content_kind": "normalized:FrontmatterProvenanceMetadata.content_kind",
        "source_url": ("digest:FrontmatterProvenanceMetadata.source_ref_digest_sha256"),
        "title": "excluded:raw-content-title",
        "shared_text": "excluded:raw-content-body",
        "captured_at": "excluded:capture-timestamp",
        "capture_why": "digest:ReviewProposal.capture_why_digest_sha256",
        "capture_why_origin": ("normalized:FrontmatterProvenanceMetadata.owner_context"),
        "capture_source": "excluded:capture-ingress-adapter",
        "provenance": "expanded:PROVENANCE_FIELD_MAP",
        "raw_assets": "digest:RawFileSetMetadata.file_digests_sha256",
        "privacy_decision": "expanded:PRIVACY_DECISION_FIELD_MAP",
    }
)
REVIEW_ACTOR_FIELD_MAP = MappingProxyType(
    {
        "kind": "normalized:ReviewProposal.actor_kind",
        "label": "digest:ReviewProposal.actor_label_digest_sha256",
    }
)
REVIEW_PROPOSAL_FIELD_MAP = MappingProxyType(
    {
        "schema_version": "normalized:ReviewProposal.schema_version",
        "review_id": "opaque:ReviewProposal.review_id",
        "capture_id": "opaque:ReviewProposal.capture_id",
        "source_ref": "digest:ReviewProposal.source_ref_digest_sha256",
        "privacy_tier": "normalized:ReviewProposal.privacy_tier",
        "proposed_intent": "normalized:ReviewProposal.proposed_intent",
        "proposal_reason": "digest:ReviewProposal.proposal_reason_digest_sha256",
        "capture_why": "digest:ReviewProposal.capture_why_digest_sha256",
        "state": "normalized:ReviewProposal.state",
        "created_at": "normalized:ReviewProposal.created_at",
        "created_by": "expanded:REVIEW_ACTOR_FIELD_MAP",
    }
)
DOCTOR_READING_FIELD_MAP = MappingProxyType(
    {
        "state": "normalized:HealthFinding.state",
        "count": "excluded:probe-metric-count",
        "age_seconds": "excluded:probe-metric-age",
        "observed_at": "excluded:probe-observation-time",
        "target": "excluded:deployment-target",
    }
)
DOCTOR_CHECK_FIELD_MAP = MappingProxyType(
    {
        "probe": "normalized:HealthFinding.probe",
        "state": "normalized:HealthFinding.state",
        "finding_class": "normalized:HealthFinding.finding_class",
        "count": "excluded:probe-metric-count",
        "age_seconds": "excluded:probe-metric-age",
        "observed_at": "excluded:probe-observation-time",
        "target": "excluded:deployment-target",
    }
)
DOCTOR_HISTORICAL_FIELD_MAP = MappingProxyType(
    {
        "job_id": "excluded:historical-job-identity",
        "observed_at": "excluded:historical-observation-time",
    }
)
DOCTOR_RESULT_FIELD_MAP = MappingProxyType(
    {
        "schema_version": "excluded:doctor-schema-version-manifest-bound",
        "role": "excluded:doctor-runtime-role",
        "strict": "excluded:doctor-strict-mode",
        "outcome": "normalized:HealthDoctorMetadata.outcome",
        "exit_code": "excluded:doctor-process-exit",
        "cutover_ready": "excluded:production-readiness-claim",
        "checks": "expanded:DOCTOR_CHECK_FIELD_MAP",
        "findings": "expanded:DOCTOR_CHECK_FIELD_MAP",
        "historical_diagnoses": "expanded:DOCTOR_HISTORICAL_FIELD_MAP",
    }
)
CLI_FIELD_MAP = MappingProxyType(
    {
        key: (
            f"normalized:CliJsonMetadata.{key}"
            if key in {"command", "status", "redacted"}
            else f"digest:CliJsonMetadata.field_digests.{key}"
        )
        for key in _CLI_OUTPUT_KEYS
    }
)

_AUTHORITATIVE_FIELD_MAPPINGS = MappingProxyType(
    {
        "QUEUE_WORK_ITEM_FIELD_MAP": QUEUE_WORK_ITEM_FIELD_MAP,
        "PRIVACY_DECISION_FIELD_MAP": PRIVACY_DECISION_FIELD_MAP,
        "PROVENANCE_FIELD_MAP": PROVENANCE_FIELD_MAP,
        "CAPTURE_ENVELOPE_FIELD_MAP": CAPTURE_ENVELOPE_FIELD_MAP,
        "REVIEW_ACTOR_FIELD_MAP": REVIEW_ACTOR_FIELD_MAP,
        "REVIEW_PROPOSAL_FIELD_MAP": REVIEW_PROPOSAL_FIELD_MAP,
        "DOCTOR_READING_FIELD_MAP": DOCTOR_READING_FIELD_MAP,
        "DOCTOR_CHECK_FIELD_MAP": DOCTOR_CHECK_FIELD_MAP,
        "DOCTOR_HISTORICAL_FIELD_MAP": DOCTOR_HISTORICAL_FIELD_MAP,
        "DOCTOR_RESULT_FIELD_MAP": DOCTOR_RESULT_FIELD_MAP,
        "CLI_FIELD_MAP": CLI_FIELD_MAP,
    }
)


@dataclass(frozen=True, slots=True)
class BuiltArtifactIdentity:
    """Caller-declared identity that is trusted only after external verification."""

    version: str
    digest_sha256: str
    distribution: ClassVar[str] = "open-brain"

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("BuiltArtifactIdentity is runtime-final")

    def __post_init__(self) -> None:
        _validated_artifact(self)

    def to_dict(self) -> dict[str, str]:
        return _artifact_dict(self)


def _validated_artifact(value: object) -> BuiltArtifactIdentity:
    if type(value) is not BuiltArtifactIdentity:
        raise ParityValidationError("invalid built artifact identity")
    if (
        not isinstance(value.version, str)
        or len(value.version) > 64
        or _VERSION.fullmatch(value.version) is None
    ):
        raise ParityValidationError("invalid built artifact version")
    _require_sha256(value.digest_sha256, "built artifact digest")
    return value


def _artifact_dict(value: object) -> dict[str, str]:
    artifact = _validated_artifact(value)
    return {
        "distribution": BuiltArtifactIdentity.distribution,
        "version": artifact.version,
        "digest_sha256": artifact.digest_sha256,
    }


@dataclass(frozen=True, slots=True)
class RequestContentMetadata:
    request_status: RequestStatus
    request_id: str
    content_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.request_status, RequestStatus):
            raise ParityValidationError("invalid request status")
        _require_opaque_id(self.request_id, "request ID")
        _require_opaque_ids(self.content_ids, "content IDs")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {
            "request_status": self.request_status.value,
            "request_id": self.request_id,
            "content_ids": sorted(self.content_ids),
        }


@dataclass(frozen=True, slots=True)
class RawFileSetMetadata:
    file_digests_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _require_sha256s(self.file_digests_sha256, "raw file-set digests")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {"file_digests_sha256": sorted(self.file_digests_sha256)}


@dataclass(frozen=True, slots=True)
class QueueTransition:
    from_state: QueueState
    to_state: QueueState
    attempt_count: int
    last_error_code: QueueErrorClass | None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            not isinstance(self.from_state, QueueState)
            or not isinstance(self.to_state, QueueState)
            or self.from_state is self.to_state
        ):
            raise ParityValidationError("invalid queue transition")
        _require_nonnegative_int(self.attempt_count, "queue attempt count")
        if self.last_error_code is not None and not isinstance(
            self.last_error_code, QueueErrorClass
        ):
            raise ParityValidationError("invalid queue error class")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "attempt_count": self.attempt_count,
            "last_error_code": (
                None if self.last_error_code is None else self.last_error_code.value
            ),
        }


@dataclass(frozen=True, slots=True)
class QueueRetryMetadata:
    transitions: tuple[QueueTransition, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.transitions, tuple) or not all(
            type(transition) is QueueTransition for transition in self.transitions
        ):
            raise ParityValidationError("invalid queue transition inventory")
        for transition in self.transitions:
            transition._validate()

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {"transitions": [transition._normalized() for transition in self.transitions]}


@dataclass(frozen=True, slots=True)
class FrontmatterProvenanceMetadata:
    schema_version: int
    content_kind: ContentKind
    privacy_tier: PrivacyTier
    source_kind: SourceKind
    source_ref_digest_sha256: str
    content_origin: ContentOrigin
    owner_context: OwnerContext
    redaction_policy_version: int

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _require_positive_int(self.schema_version, "frontmatter schema version")
        _require_positive_int(self.redaction_policy_version, "redaction policy version")
        if (
            not isinstance(self.content_kind, ContentKind)
            or not isinstance(self.privacy_tier, PrivacyTier)
            or not isinstance(self.source_kind, SourceKind)
            or not isinstance(self.content_origin, ContentOrigin)
            or not isinstance(self.owner_context, OwnerContext)
        ):
            raise ParityValidationError("invalid frontmatter or provenance class")
        _require_sha256(self.source_ref_digest_sha256, "source reference digest")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {
            "schema_version": self.schema_version,
            "content_kind": self.content_kind.value,
            "privacy_tier": self.privacy_tier.value,
            "source_kind": self.source_kind.value,
            "source_ref_digest_sha256": self.source_ref_digest_sha256,
            "content_origin": self.content_origin.value,
            "owner_context": self.owner_context.value,
            "redaction_policy_version": self.redaction_policy_version,
        }


@dataclass(frozen=True, slots=True)
class RoutingMetadata:
    destination: RoutingDestination

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.destination, RoutingDestination):
            raise ParityValidationError("invalid routing destination")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {"destination": self.destination.value}


@dataclass(frozen=True, slots=True)
class LedgerCitationMetadata:
    ledger_item_ids: tuple[str, ...]
    citation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _require_opaque_ids(self.ledger_item_ids, "ledger item IDs")
        _require_opaque_ids(self.citation_ids, "citation IDs")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {
            "ledger_item_ids": sorted(self.ledger_item_ids),
            "citation_ids": sorted(self.citation_ids),
        }


@dataclass(frozen=True, slots=True)
class ReviewProposal:
    schema_version: int
    review_id: str
    capture_id: str
    source_ref_digest_sha256: str
    privacy_tier: PrivacyTier
    proposed_intent: ReviewIntent
    proposal_reason_digest_sha256: str
    capture_why_digest_sha256: str
    state: ReviewProposalState
    created_at: datetime
    actor_kind: ReviewActorKind
    actor_label_digest_sha256: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ParityValidationError("invalid review schema version")
        _require_opaque_id(self.review_id, "review ID")
        _require_opaque_id(self.capture_id, "capture ID")
        _require_sha256(self.source_ref_digest_sha256, "review source reference digest")
        _require_sha256(self.proposal_reason_digest_sha256, "proposal reason digest")
        _require_sha256(self.capture_why_digest_sha256, "capture why digest")
        _require_sha256(self.actor_label_digest_sha256, "review actor label digest")
        if (
            not isinstance(self.privacy_tier, PrivacyTier)
            or not isinstance(self.proposed_intent, ReviewIntent)
            or not isinstance(self.state, ReviewProposalState)
            or not isinstance(self.actor_kind, ReviewActorKind)
        ):
            raise ParityValidationError("invalid review proposal class")
        _utc(self.created_at, "review creation time")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {
            "schema_version": self.schema_version,
            "review_id": self.review_id,
            "capture_id": self.capture_id,
            "source_ref_digest_sha256": self.source_ref_digest_sha256,
            "privacy_tier": self.privacy_tier.value,
            "proposed_intent": self.proposed_intent.value,
            "proposal_reason_digest_sha256": self.proposal_reason_digest_sha256,
            "capture_why_digest_sha256": self.capture_why_digest_sha256,
            "state": self.state.value,
            "created_at": _timestamp(self.created_at),
            "actor_kind": self.actor_kind.value,
            "actor_label_digest_sha256": self.actor_label_digest_sha256,
        }


@dataclass(frozen=True, slots=True)
class ReviewProposalsMetadata:
    proposals: tuple[ReviewProposal, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.proposals, tuple) or not all(
            type(proposal) is ReviewProposal for proposal in self.proposals
        ):
            raise ParityValidationError("invalid review proposal inventory")
        for proposal in self.proposals:
            proposal._validate()
        if len({proposal.review_id for proposal in self.proposals}) != len(self.proposals):
            raise ParityValidationError("duplicate review proposal")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        proposals = sorted(
            (proposal._normalized() for proposal in self.proposals),
            key=lambda proposal: str(proposal["review_id"]),
        )
        return {"proposals": proposals}


@dataclass(frozen=True, slots=True)
class CliJsonMetadata:
    profile: CliProfile
    command: CliCommand
    status: CliStatus
    exit_class: CliExitClass
    field_digests: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.profile, CliProfile):
            raise ParityValidationError("invalid CLI profile")
        if not isinstance(self.command, CliCommand) or not isinstance(self.status, CliStatus):
            raise ParityValidationError("invalid CLI JSON class")
        if not isinstance(self.exit_class, CliExitClass):
            raise ParityValidationError("invalid CLI exit code class")
        expected_exit_classes = {
            CliStatus.COMPLETED: CliExitClass.SUCCESS,
            CliStatus.FAILED: CliExitClass.FAILURE,
            CliStatus.UNAVAILABLE: CliExitClass.FAILURE,
            CliStatus.DEFERRED: CliExitClass.DEFERRED,
            CliStatus.INVALID: CliExitClass.USAGE,
        }
        if expected_exit_classes[self.status] is not self.exit_class:
            raise ParityValidationError("invalid CLI status and exit code class")
        if not isinstance(self.field_digests, tuple):
            raise ParityValidationError("invalid CLI output field inventory")
        keys: list[str] = []
        for item in self.field_digests:
            if type(item) is not tuple or len(item) != 2:
                raise ParityValidationError("invalid CLI output field inventory")
            key, digest = item
            if not isinstance(key, str) or key not in CLI_PROFILE_FIELDS[self.profile]:
                raise ParityValidationError("invalid CLI output field")
            _require_sha256(digest, "CLI output field digest")
            keys.append(key)
        if len(set(keys)) != len(keys):
            raise ParityValidationError("duplicate CLI output field")
        expected_command = {
            CliProfile.OPEN_BRAIN_STATUS: CliCommand.STATUS,
            CliProfile.OPEN_BRAIN_CRON: CliCommand.CRON,
            CliProfile.BRAIN_SYSTEM_STATUS: CliCommand.STATUS,
            CliProfile.SUMMARIZER_CRON: CliCommand.CRON,
        }[self.profile]
        if self.command is not expected_command:
            raise ParityValidationError("invalid CLI profile command")
        if frozenset(keys) != CLI_PROFILE_FIELDS[self.profile]:
            raise ParityValidationError("invalid CLI profile field inventory")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {
            "profile": self.profile.value,
            "command": self.command.value,
            "status": self.status.value,
            "exit_class": int(self.exit_class),
            "field_digests": [
                {"field": key, "digest_sha256": digest}
                for key, digest in sorted(self.field_digests)
            ],
            "redacted": True,
        }


@dataclass(frozen=True, slots=True)
class HealthFinding:
    probe: DoctorProbe
    finding_class: HealthFindingClass
    state: DoctorProbeState

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            not isinstance(self.probe, DoctorProbe)
            or not isinstance(self.finding_class, HealthFindingClass)
            or not isinstance(self.state, DoctorProbeState)
            or self.state is DoctorProbeState.HEALTHY
        ):
            raise ParityValidationError("invalid health finding class")

    def _normalized(self) -> dict[str, str]:
        self._validate()
        return {
            "probe": self.probe.value,
            "finding_class": self.finding_class.value,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class HealthDoctorMetadata:
    outcome: HealthOutcome
    findings: tuple[HealthFinding, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not isinstance(self.outcome, HealthOutcome) or not isinstance(self.findings, tuple):
            raise ParityValidationError("invalid health or doctor metadata")
        if not all(type(finding) is HealthFinding for finding in self.findings):
            raise ParityValidationError("invalid health finding inventory")
        for finding in self.findings:
            finding._validate()
        finding_keys = {
            (finding.probe, finding.finding_class, finding.state) for finding in self.findings
        }
        if len(finding_keys) != len(self.findings):
            raise ParityValidationError("duplicate health finding")
        if (self.outcome is HealthOutcome.HEALTHY) is bool(self.findings):
            raise ParityValidationError("invalid health outcome and findings")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        findings = sorted(
            (finding._normalized() for finding in self.findings),
            key=lambda finding: (
                finding["probe"],
                finding["finding_class"],
                finding["state"],
            ),
        )
        return {"outcome": self.outcome.value, "findings": findings}


@dataclass(frozen=True, slots=True)
class ShadowObservationMetadata:
    extraction_class: ShadowExtractionClass
    routing_destination: RoutingDestination
    content_kind: ContentKind
    source_kind: SourceKind
    source_ref_digest_sha256: str
    content_origin: ContentOrigin
    owner_context: OwnerContext
    provider_class: ShadowProviderClass
    privacy_tier: PrivacyTier
    resource_class: ShadowResourceClass
    redaction_class: ShadowRedactionClass
    redaction_policy_version: int

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            not isinstance(self.extraction_class, ShadowExtractionClass)
            or not isinstance(self.routing_destination, RoutingDestination)
            or not isinstance(self.content_kind, ContentKind)
            or not isinstance(self.source_kind, SourceKind)
            or not isinstance(self.content_origin, ContentOrigin)
            or not isinstance(self.owner_context, OwnerContext)
            or not isinstance(self.provider_class, ShadowProviderClass)
            or not isinstance(self.privacy_tier, PrivacyTier)
            or not isinstance(self.resource_class, ShadowResourceClass)
            or not isinstance(self.redaction_class, ShadowRedactionClass)
        ):
            raise ParityValidationError("invalid shadow observation class")
        if self.content_origin is ContentOrigin.UNKNOWN:
            raise ParityValidationError("missing shadow provenance")
        _require_sha256(self.source_ref_digest_sha256, "shadow source reference digest")
        _require_positive_int(self.redaction_policy_version, "shadow redaction policy version")

    def _normalized(self) -> dict[str, object]:
        self._validate()
        return {
            "extraction_class": self.extraction_class.value,
            "routing_destination": self.routing_destination.value,
            "content_kind": self.content_kind.value,
            "source_kind": self.source_kind.value,
            "source_ref_digest_sha256": self.source_ref_digest_sha256,
            "content_origin": self.content_origin.value,
            "owner_context": self.owner_context.value,
            "provider_class": self.provider_class.value,
            "privacy_tier": self.privacy_tier.value,
            "resource_class": self.resource_class.value,
            "redaction_class": self.redaction_class.value,
            "redaction_policy_version": self.redaction_policy_version,
        }


type FacetMetadata = (
    RequestContentMetadata
    | RawFileSetMetadata
    | QueueRetryMetadata
    | FrontmatterProvenanceMetadata
    | RoutingMetadata
    | LedgerCitationMetadata
    | ReviewProposalsMetadata
    | CliJsonMetadata
    | HealthDoctorMetadata
    | ShadowObservationMetadata
)

_EXPECTED_FACET_METADATA_TYPES: tuple[tuple[ParityFacet, type[object]], ...] = (
    (ParityFacet.REQUEST_CONTENT, RequestContentMetadata),
    (ParityFacet.RAW_FILE_SET, RawFileSetMetadata),
    (ParityFacet.QUEUE_RETRY, QueueRetryMetadata),
    (ParityFacet.FRONTMATTER_PROVENANCE, FrontmatterProvenanceMetadata),
    (ParityFacet.ROUTING, RoutingMetadata),
    (ParityFacet.LEDGER_CITATIONS, LedgerCitationMetadata),
    (ParityFacet.REVIEW_PROPOSALS, ReviewProposalsMetadata),
    (ParityFacet.CLI_JSON, CliJsonMetadata),
    (ParityFacet.HEALTH_DOCTOR, HealthDoctorMetadata),
)
_SHADOW_EXPECTED_FACET_METADATA_TYPES: tuple[tuple[ParityFacet, type[object]], ...] = (
    (ParityFacet.SHADOW_OBSERVATION, ShadowObservationMetadata),
)
_ALL_EXPECTED_FACET_METADATA_TYPES = (
    _EXPECTED_FACET_METADATA_TYPES + _SHADOW_EXPECTED_FACET_METADATA_TYPES
)
_FACET_METADATA_TYPES = MappingProxyType(dict(_ALL_EXPECTED_FACET_METADATA_TYPES))

_ENUM_MAPPINGS = (
    ("queue_state", QUEUE_STATE_MAP),
    ("queue_error", QUEUE_ERROR_MAP),
    ("content_kind", CONTENT_KIND_MAP),
    ("privacy_tier", PRIVACY_TIER_MAP),
    ("source_type", SOURCE_TYPE_MAP),
    ("content_origin", CONTENT_ORIGIN_MAP),
    ("owner_context", OWNER_CONTEXT_MAP),
    ("review_state", REVIEW_STATE_MAP),
    ("review_actor", REVIEW_ACTOR_MAP),
    ("review_intent", REVIEW_INTENT_MAP),
    ("cli_exit_class", CLI_EXIT_CLASS_MAP),
    ("doctor_probe", DOCTOR_PROBE_MAP),
    ("doctor_state", DOCTOR_STATE_MAP),
    ("doctor_finding", DOCTOR_FINDING_MAP),
    ("health_outcome", HEALTH_OUTCOME_MAP),
)


def _schema_definition() -> dict[str, object]:
    return {
        "manifest_version": PARITY_HARNESS_VERSION,
        "facets": [facet.value for facet in P7_W0_FACETS],
        "facet_metadata": [
            {
                "facet": facet.value,
                "type": metadata_type.__name__,
                "fields": [
                    field.name
                    for field in fields(metadata_type)  # type: ignore[arg-type]
                ],
            }
            for facet, metadata_type in _EXPECTED_FACET_METADATA_TYPES
        ],
        "enum_mappings": {
            name: [
                [str(authoritative), str(normalized.value)]
                for authoritative, normalized in mapping.items()
            ]
            for name, mapping in _ENUM_MAPPINGS
        },
        "cli_output_keys": sorted(CLI_OUTPUT_KEY_MAP),
        "cli_profiles": {
            profile.value: sorted(profile_fields)
            for profile, profile_fields in CLI_PROFILE_FIELDS.items()
        },
        "authoritative_field_mappings": {
            name: [[field, binding] for field, binding in mapping.items()]
            for name, mapping in _AUTHORITATIVE_FIELD_MAPPINGS.items()
        },
    }


def _shadow_schema_definition() -> dict[str, object]:
    return {
        "manifest_version": P7_W1_SHADOW_VERSION,
        "facets": [facet.value for facet in P7_W1_SHADOW_FACETS],
        "facet_metadata": [
            {
                "facet": facet.value,
                "type": metadata_type.__name__,
                "fields": [
                    field.name
                    for field in fields(metadata_type)  # type: ignore[arg-type]
                ],
            }
            for facet, metadata_type in _SHADOW_EXPECTED_FACET_METADATA_TYPES
        ],
        "enum_values": {
            enum_type.__name__: [member.value for member in enum_type]
            for enum_type in (
                ShadowExtractionClass,
                ShadowProviderClass,
                ShadowResourceClass,
                ShadowRedactionClass,
                RoutingDestination,
                ContentKind,
                SourceKind,
                ContentOrigin,
                OwnerContext,
                PrivacyTier,
            )
        },
    }


PARITY_SCHEMA_DIGEST_SHA256 = sha256(
    json.dumps(
        _schema_definition(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()

P7_W1_SHADOW_SCHEMA_DIGEST_SHA256 = sha256(
    json.dumps(
        _shadow_schema_definition(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class ParityManifest:
    version: str
    facets: tuple[ParityFacet, ...]
    schema_digest_sha256: str

    def __post_init__(self) -> None:
        is_wave_zero = (
            self.version == PARITY_HARNESS_VERSION
            and self.facets == P7_W0_FACETS
            and self.schema_digest_sha256 == PARITY_SCHEMA_DIGEST_SHA256
        )
        is_shadow = (
            self.version == P7_W1_SHADOW_VERSION
            and self.facets == P7_W1_SHADOW_FACETS
            and self.schema_digest_sha256 == P7_W1_SHADOW_SCHEMA_DIGEST_SHA256
        )
        if not (is_wave_zero or is_shadow):
            raise ParityValidationError("invalid parity manifest")


PHASE7_FACET_MANIFEST = ParityManifest(
    version=PARITY_HARNESS_VERSION,
    facets=P7_W0_FACETS,
    schema_digest_sha256=PARITY_SCHEMA_DIGEST_SHA256,
)

P7_W1_SHADOW_MANIFEST = ParityManifest(
    version=P7_W1_SHADOW_VERSION,
    facets=P7_W1_SHADOW_FACETS,
    schema_digest_sha256=P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
)


def _manifest_for(version: str, schema_digest_sha256: str) -> ParityManifest:
    if (
        version == PHASE7_FACET_MANIFEST.version
        and schema_digest_sha256 == PHASE7_FACET_MANIFEST.schema_digest_sha256
    ):
        return PHASE7_FACET_MANIFEST
    if (
        version == P7_W1_SHADOW_MANIFEST.version
        and schema_digest_sha256 == P7_W1_SHADOW_MANIFEST.schema_digest_sha256
    ):
        return P7_W1_SHADOW_MANIFEST
    raise ParityValidationError("invalid parity manifest or schema")


@dataclass(frozen=True, slots=True)
class ArtifactAttestationEvidence:
    """Non-sensitive verifier evidence for an externally minted attestation."""

    verifier_id: str
    attestation_id: str
    attestation_digest_sha256: str
    artifact: BuiltArtifactIdentity
    manifest_version: str
    schema_digest_sha256: str
    scope: EvidenceScope
    evaluated_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_artifact_attestation_evidence(self)

    def to_dict(self) -> dict[str, object]:
        _validate_artifact_attestation_evidence(self)
        return {
            "verifier_id": self.verifier_id,
            "attestation_id": self.attestation_id,
            "attestation_digest_sha256": self.attestation_digest_sha256,
            "manifest_version": self.manifest_version,
            "schema_digest_sha256": self.schema_digest_sha256,
            "scope": self.scope.value,
            "evaluated_at": _timestamp(self.evaluated_at),
            "expires_at": _timestamp(self.expires_at),
        }


def _validate_artifact_attestation_evidence(
    value: object,
    *,
    expected_artifact: BuiltArtifactIdentity | None = None,
    expected_evaluated_at: datetime | None = None,
    expected_scope: EvidenceScope | None = None,
) -> ArtifactAttestationEvidence:
    if type(value) is not ArtifactAttestationEvidence:
        raise ParityValidationError("invalid artifact attestation evidence")
    _require_opaque_id(value.verifier_id, "artifact verifier ID")
    _require_opaque_id(value.attestation_id, "artifact attestation ID")
    _require_sha256(value.attestation_digest_sha256, "artifact attestation digest")
    artifact = _validated_artifact(value.artifact)
    _manifest_for(value.manifest_version, value.schema_digest_sha256)
    if type(value.scope) is not EvidenceScope or (
        expected_scope is not None and value.scope is not expected_scope
    ):
        raise ParityValidationError("invalid artifact attestation")
    evaluated = _utc(value.evaluated_at, "artifact attestation evaluation time")
    expires = _utc(value.expires_at, "artifact attestation expiry")
    if expires <= evaluated:
        raise ParityValidationError("invalid artifact attestation expiry")
    if expected_artifact is not None:
        expected = _validated_artifact(expected_artifact)
        if artifact != expected:
            raise ParityValidationError("invalid artifact attestation binding")
    if expected_evaluated_at is not None and evaluated != _utc(
        expected_evaluated_at, "comparison evaluation time"
    ):
        raise ParityValidationError("invalid artifact attestation evaluation time")
    return value


class ArtifactAttestationVerifier(Protocol):
    """Verifier-only capability implemented and provisioned outside this package."""

    def verify_artifact_attestation(
        self,
        artifact_attestation: object,
        *,
        evaluated_at: datetime,
    ) -> ArtifactAttestationEvidence: ...


@dataclass(frozen=True, slots=True)
class SyntheticFacetSnapshot:
    facet: ParityFacet
    artifact: BuiltArtifactIdentity
    metadata: FacetMetadata
    schema_digest_sha256: str = PARITY_SCHEMA_DIGEST_SHA256
    manifest_version: str = PARITY_HARNESS_VERSION

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _validate_schema_registry()
        if not isinstance(self.facet, ParityFacet):
            raise ParityValidationError("invalid parity facet")
        manifest = _manifest_for(self.manifest_version, self.schema_digest_sha256)
        if self.facet not in manifest.facets:
            raise ParityValidationError("invalid parity facet")
        _validated_artifact(self.artifact)
        expected_type = _FACET_METADATA_TYPES[self.facet]
        if type(self.metadata) is not expected_type:
            raise ParityValidationError("invalid facet metadata type")
        _normalize_metadata(self.metadata)

    def _digest_sha256(self, *, attestation_digest_sha256: str) -> str:
        self._validate()
        _require_sha256(attestation_digest_sha256, "artifact attestation digest")
        return sha256(
            _canonical_json_bytes(
                {
                    "manifest_version": self.manifest_version,
                    "schema_digest_sha256": self.schema_digest_sha256,
                    "facet": self.facet.value,
                    "artifact": _artifact_dict(self.artifact),
                    "artifact_attestation_digest_sha256": attestation_digest_sha256,
                    "metadata": _normalize_metadata(self.metadata),
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SyntheticParityInput:
    side: ParitySide
    artifact: BuiltArtifactIdentity
    facets: tuple[SyntheticFacetSnapshot, ...]
    manifest_version: str = PARITY_HARNESS_VERSION
    schema_digest_sha256: str = PARITY_SCHEMA_DIGEST_SHA256

    def __post_init__(self) -> None:
        self._validate()

    @property
    def scope(self) -> EvidenceScope:
        return EvidenceScope.SYNTHETIC

    def _validate(self) -> None:
        _validate_schema_registry()
        if not isinstance(self.side, ParitySide):
            raise ParityValidationError("invalid parity side")
        manifest = _manifest_for(self.manifest_version, self.schema_digest_sha256)
        _validated_artifact(self.artifact)
        if not isinstance(self.facets, tuple) or not all(
            type(snapshot) is SyntheticFacetSnapshot for snapshot in self.facets
        ):
            raise ParityValidationError("invalid facet inventory")
        if tuple(snapshot.facet for snapshot in self.facets) != manifest.facets:
            raise ParityValidationError("invalid facet inventory")
        for snapshot in self.facets:
            snapshot._validate()
            if snapshot.artifact != self.artifact:
                raise ParityValidationError("invalid artifact binding")
            if snapshot.schema_digest_sha256 != self.schema_digest_sha256:
                raise ParityValidationError("invalid parity schema digest")
            if snapshot.manifest_version != self.manifest_version:
                raise ParityValidationError("invalid parity manifest or schema")


@dataclass(frozen=True, slots=True)
class LiveFacetSnapshot:
    """Normalized metadata produced by an externally attested live execution."""

    facet: ParityFacet
    artifact: BuiltArtifactIdentity
    metadata: FacetMetadata
    schema_digest_sha256: str = PARITY_SCHEMA_DIGEST_SHA256
    manifest_version: str = PARITY_HARNESS_VERSION

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _validate_schema_registry()
        if not isinstance(self.facet, ParityFacet):
            raise ParityValidationError("invalid parity facet")
        manifest = _manifest_for(self.manifest_version, self.schema_digest_sha256)
        if self.facet not in manifest.facets:
            raise ParityValidationError("invalid parity facet")
        _validated_artifact(self.artifact)
        expected_type = _FACET_METADATA_TYPES[self.facet]
        if type(self.metadata) is not expected_type:
            raise ParityValidationError("invalid facet metadata type")
        _normalize_metadata(self.metadata)

    def _digest_sha256(self, *, attestation_digest_sha256: str) -> str:
        self._validate()
        _require_sha256(attestation_digest_sha256, "artifact attestation digest")
        return sha256(
            _canonical_json_bytes(
                {
                    "manifest_version": self.manifest_version,
                    "schema_digest_sha256": self.schema_digest_sha256,
                    "facet": self.facet.value,
                    "artifact": _artifact_dict(self.artifact),
                    "artifact_attestation_digest_sha256": attestation_digest_sha256,
                    "metadata": _normalize_live_metadata(self.metadata),
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class LiveParityInput:
    """One complete live side, distinct from synthetic readiness evidence."""

    side: ParitySide
    artifact: BuiltArtifactIdentity
    facets: tuple[LiveFacetSnapshot, ...]
    manifest_version: str = PARITY_HARNESS_VERSION
    schema_digest_sha256: str = PARITY_SCHEMA_DIGEST_SHA256

    def __post_init__(self) -> None:
        self._validate()

    @property
    def scope(self) -> EvidenceScope:
        return EvidenceScope.LIVE

    def _validate(self) -> None:
        _validate_schema_registry()
        if not isinstance(self.side, ParitySide):
            raise ParityValidationError("invalid parity side")
        manifest = _manifest_for(self.manifest_version, self.schema_digest_sha256)
        _validated_artifact(self.artifact)
        if not isinstance(self.facets, tuple) or not all(
            type(snapshot) is LiveFacetSnapshot for snapshot in self.facets
        ):
            raise ParityValidationError("invalid live facet inventory")
        if tuple(snapshot.facet for snapshot in self.facets) != manifest.facets:
            raise ParityValidationError("invalid live facet inventory")
        for snapshot in self.facets:
            snapshot._validate()
            if snapshot.artifact != self.artifact:
                raise ParityValidationError("invalid artifact binding")
            if snapshot.schema_digest_sha256 != self.schema_digest_sha256:
                raise ParityValidationError("invalid parity schema digest")
            if snapshot.manifest_version != self.manifest_version:
                raise ParityValidationError("invalid parity manifest or schema")


@dataclass(frozen=True, slots=True)
class FacetComparison:
    facet: ParityFacet
    outcome: ComparisonOutcome
    unavailable: bool
    legacy_digest_sha256: str
    open_brain_digest_sha256: str
    artifact_attestation_digest_sha256: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _validate_schema_registry()
        if not isinstance(self.facet, ParityFacet) or not isinstance(
            self.outcome, ComparisonOutcome
        ):
            raise ParityValidationError("invalid facet comparison")
        if type(self.unavailable) is not bool:
            raise ParityValidationError("invalid facet availability")
        _require_sha256(self.legacy_digest_sha256, "legacy normalized digest")
        _require_sha256(self.open_brain_digest_sha256, "Open Brain normalized digest")
        _require_sha256(
            self.artifact_attestation_digest_sha256,
            "artifact attestation digest",
        )
        digests_match = self.legacy_digest_sha256 == self.open_brain_digest_sha256
        if self.unavailable and self.outcome is not ComparisonOutcome.BLOCKED_DIFFERENCE:
            raise ParityValidationError("unavailable facet cannot match")
        if not self.unavailable and (
            (self.outcome is ComparisonOutcome.MATCH) is not digests_match
        ):
            raise ParityValidationError("invalid facet comparison")

    def to_dict(self) -> dict[str, str | bool]:
        self._validate()
        return {
            "facet": self.facet.value,
            "outcome": self.outcome.value,
            "unavailable": self.unavailable,
            "legacy_digest_sha256": self.legacy_digest_sha256,
            "open_brain_digest_sha256": self.open_brain_digest_sha256,
            "artifact_attestation_digest_sha256": (self.artifact_attestation_digest_sha256),
        }


class SyntheticParityResult:
    """A revalidated redacted result that can only claim synthetic evidence."""

    __slots__ = (
        "manifest_version",
        "schema_digest_sha256",
        "artifact",
        "scope",
        "evaluated_at",
        "facets",
        "comparison_digest_sha256",
        "_artifact_evidence",
        "_artifact_attestation",
        "_artifact_verifier",
    )

    manifest_version: str
    schema_digest_sha256: str
    artifact: BuiltArtifactIdentity
    scope: EvidenceScope
    evaluated_at: datetime
    facets: tuple[FacetComparison, ...]
    comparison_digest_sha256: str
    _artifact_evidence: ArtifactAttestationEvidence
    _artifact_attestation: object
    _artifact_verifier: ArtifactAttestationVerifier

    def __init__(self) -> None:
        raise TypeError("synthetic parity results are harness-created")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("synthetic parity results are immutable")

    @property
    def artifact_attestation(self) -> ArtifactAttestationEvidence:
        self._validate()
        return self._artifact_evidence

    @property
    def resolved(self) -> bool:
        self._validate()
        return all(comparison.outcome is ComparisonOutcome.MATCH for comparison in self.facets)

    def for_facet(self, facet: ParityFacet) -> FacetComparison:
        self._validate()
        if not isinstance(facet, ParityFacet):
            raise ParityValidationError("invalid parity facet")
        for comparison in self.facets:
            if comparison.facet is facet:
                return comparison
        raise ParityValidationError("facet not in comparison manifest")

    def to_dict(self) -> dict[str, object]:
        self._validate()
        payload = _result_payload(
            artifact=self.artifact,
            artifact_evidence=self._artifact_evidence,
            evaluated_at=self.evaluated_at,
            facets=self.facets,
            scope=self.scope,
        )
        return {
            **payload,
            "comparison_digest_sha256": self.comparison_digest_sha256,
        }

    def _validate(self) -> None:
        _validate_schema_registry()
        manifest = _manifest_for(self.manifest_version, self.schema_digest_sha256)
        if self.scope is not EvidenceScope.SYNTHETIC:
            raise ParityValidationError("invalid comparison result")
        _validated_artifact(self.artifact)
        evaluated = _utc(self.evaluated_at, "comparison evaluation time")
        reverified = _verify_artifact_attestation(
            self._artifact_verifier,
            self._artifact_attestation,
            artifact=self.artifact,
            evaluated_at=evaluated,
            expected_scope=EvidenceScope.SYNTHETIC,
        )
        if reverified != self._artifact_evidence:
            raise ParityValidationError("invalid artifact attestation revalidation")
        if (
            self._artifact_evidence.manifest_version != self.manifest_version
            or self._artifact_evidence.schema_digest_sha256 != self.schema_digest_sha256
        ):
            raise ParityValidationError("invalid comparison result")
        if not isinstance(self.facets, tuple) or not all(
            type(comparison) is FacetComparison for comparison in self.facets
        ):
            raise ParityValidationError("invalid comparison result")
        if tuple(comparison.facet for comparison in self.facets) != manifest.facets:
            raise ParityValidationError("invalid comparison result")
        for comparison in self.facets:
            comparison._validate()
            if (
                comparison.artifact_attestation_digest_sha256
                != self._artifact_evidence.attestation_digest_sha256
            ):
                raise ParityValidationError("invalid comparison result")
        _require_sha256(self.comparison_digest_sha256, "comparison digest")
        expected_digest = sha256(
            _canonical_json_bytes(
                _result_payload(
                    artifact=self.artifact,
                    artifact_evidence=self._artifact_evidence,
                    evaluated_at=evaluated,
                    facets=self.facets,
                    scope=self.scope,
                )
            )
        ).hexdigest()
        if self.comparison_digest_sha256 != expected_digest:
            raise ParityValidationError("invalid comparison result")


class LiveParityResult:
    """A revalidated result that can only claim externally attested live evidence."""

    __slots__ = (
        "manifest_version",
        "schema_digest_sha256",
        "artifact",
        "scope",
        "evaluated_at",
        "facets",
        "comparison_digest_sha256",
        "_artifact_evidence",
        "_artifact_attestation",
        "_artifact_verifier",
    )

    manifest_version: str
    schema_digest_sha256: str
    artifact: BuiltArtifactIdentity
    scope: EvidenceScope
    evaluated_at: datetime
    facets: tuple[FacetComparison, ...]
    comparison_digest_sha256: str
    _artifact_evidence: ArtifactAttestationEvidence
    _artifact_attestation: object
    _artifact_verifier: ArtifactAttestationVerifier

    def __init__(self) -> None:
        raise TypeError("live parity results are harness-created")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("live parity results are immutable")

    @property
    def artifact_attestation(self) -> ArtifactAttestationEvidence:
        self._validate()
        return self._artifact_evidence

    @property
    def resolved(self) -> bool:
        self._validate()
        return all(comparison.outcome is ComparisonOutcome.MATCH for comparison in self.facets)

    def for_facet(self, facet: ParityFacet) -> FacetComparison:
        self._validate()
        if not isinstance(facet, ParityFacet):
            raise ParityValidationError("invalid parity facet")
        for comparison in self.facets:
            if comparison.facet is facet:
                return comparison
        raise ParityValidationError("facet not in comparison manifest")

    def to_dict(self) -> dict[str, object]:
        self._validate()
        payload = _result_payload(
            artifact=self.artifact,
            artifact_evidence=self._artifact_evidence,
            evaluated_at=self.evaluated_at,
            facets=self.facets,
            scope=self.scope,
        )
        return {
            **payload,
            "comparison_digest_sha256": self.comparison_digest_sha256,
        }

    def _validate(self) -> None:
        _validate_schema_registry()
        manifest = _manifest_for(self.manifest_version, self.schema_digest_sha256)
        if self.scope is not EvidenceScope.LIVE:
            raise ParityValidationError("invalid live comparison result")
        _validated_artifact(self.artifact)
        evaluated = _utc(self.evaluated_at, "comparison evaluation time")
        reverified = _verify_artifact_attestation(
            self._artifact_verifier,
            self._artifact_attestation,
            artifact=self.artifact,
            evaluated_at=evaluated,
            expected_scope=EvidenceScope.LIVE,
        )
        if reverified != self._artifact_evidence:
            raise ParityValidationError("invalid artifact attestation revalidation")
        if (
            self._artifact_evidence.manifest_version != self.manifest_version
            or self._artifact_evidence.schema_digest_sha256 != self.schema_digest_sha256
        ):
            raise ParityValidationError("invalid live comparison result")
        if not isinstance(self.facets, tuple) or not all(
            type(comparison) is FacetComparison for comparison in self.facets
        ):
            raise ParityValidationError("invalid live comparison result")
        if tuple(comparison.facet for comparison in self.facets) != manifest.facets:
            raise ParityValidationError("invalid live comparison result")
        for comparison in self.facets:
            comparison._validate()
            if (
                comparison.artifact_attestation_digest_sha256
                != self._artifact_evidence.attestation_digest_sha256
            ):
                raise ParityValidationError("invalid live comparison result")
        _require_sha256(self.comparison_digest_sha256, "comparison digest")
        expected_digest = sha256(
            _canonical_json_bytes(
                _result_payload(
                    artifact=self.artifact,
                    artifact_evidence=self._artifact_evidence,
                    evaluated_at=evaluated,
                    facets=self.facets,
                    scope=self.scope,
                )
            )
        ).hexdigest()
        if self.comparison_digest_sha256 != expected_digest:
            raise ParityValidationError("invalid live comparison result")


def _metadata_unavailable(metadata: FacetMetadata) -> bool:
    if type(metadata) is RequestContentMetadata:
        return metadata.request_status is RequestStatus.UNAVAILABLE
    if type(metadata) is RoutingMetadata:
        return metadata.destination is RoutingDestination.UNAVAILABLE
    if type(metadata) is CliJsonMetadata:
        return metadata.status is CliStatus.UNAVAILABLE
    if type(metadata) is HealthDoctorMetadata:
        return metadata.outcome is HealthOutcome.UNAVAILABLE
    if type(metadata) is ShadowObservationMetadata:
        return (
            metadata.extraction_class is not ShadowExtractionClass.COMPLETE
            or metadata.resource_class is not ShadowResourceClass.WITHIN_LIMIT
            or metadata.redaction_class
            in {ShadowRedactionClass.FAILED, ShadowRedactionClass.RAW_RESIDUE}
        )
    return False


def compare_synthetic_parity(
    legacy: SyntheticParityInput,
    open_brain: SyntheticParityInput,
    *,
    evaluated_at: datetime,
    artifact_attestation: object,
    artifact_verifier: ArtifactAttestationVerifier,
) -> SyntheticParityResult:
    """Compare complete synthetic sides; every difference remains owner-gated."""
    _validate_schema_registry()
    if type(legacy) is not SyntheticParityInput or type(open_brain) is not SyntheticParityInput:
        raise ParityValidationError("invalid synthetic parity input")
    legacy._validate()
    open_brain._validate()
    evaluated = _utc(evaluated_at, "comparison evaluation time")
    if legacy.side is not ParitySide.LEGACY or open_brain.side is not ParitySide.OPEN_BRAIN:
        raise ParityValidationError("invalid comparison sides")
    if legacy.artifact != open_brain.artifact:
        raise ParityValidationError("invalid artifact binding")
    if (
        legacy.manifest_version != open_brain.manifest_version
        or legacy.schema_digest_sha256 != open_brain.schema_digest_sha256
    ):
        raise ParityValidationError("invalid parity manifest or schema")

    artifact_evidence = _verify_artifact_attestation(
        artifact_verifier,
        artifact_attestation,
        artifact=legacy.artifact,
        evaluated_at=evaluated,
        expected_scope=EvidenceScope.SYNTHETIC,
    )
    if (
        artifact_evidence.manifest_version != legacy.manifest_version
        or artifact_evidence.schema_digest_sha256 != legacy.schema_digest_sha256
    ):
        raise ParityValidationError("invalid artifact attestation binding")
    attestation_digest = artifact_evidence.attestation_digest_sha256
    digests = tuple(
        (
            legacy_snapshot.facet,
            legacy_snapshot._digest_sha256(attestation_digest_sha256=attestation_digest),
            open_brain_snapshot._digest_sha256(attestation_digest_sha256=attestation_digest),
            _metadata_unavailable(legacy_snapshot.metadata)
            or _metadata_unavailable(open_brain_snapshot.metadata),
        )
        for legacy_snapshot, open_brain_snapshot in zip(
            legacy.facets,
            open_brain.facets,
            strict=True,
        )
    )
    comparisons = tuple(
        FacetComparison(
            facet=facet,
            outcome=(
                ComparisonOutcome.MATCH
                if legacy_digest == open_brain_digest and not unavailable
                else ComparisonOutcome.BLOCKED_DIFFERENCE
            ),
            unavailable=unavailable,
            legacy_digest_sha256=legacy_digest,
            open_brain_digest_sha256=open_brain_digest,
            artifact_attestation_digest_sha256=attestation_digest,
        )
        for facet, legacy_digest, open_brain_digest, unavailable in digests
    )
    payload = _result_payload(
        artifact=legacy.artifact,
        artifact_evidence=artifact_evidence,
        evaluated_at=evaluated,
        facets=comparisons,
        scope=EvidenceScope.SYNTHETIC,
    )
    result = object.__new__(SyntheticParityResult)
    object.__setattr__(result, "manifest_version", legacy.manifest_version)
    object.__setattr__(
        result,
        "schema_digest_sha256",
        legacy.schema_digest_sha256,
    )
    object.__setattr__(result, "artifact", legacy.artifact)
    object.__setattr__(result, "scope", EvidenceScope.SYNTHETIC)
    object.__setattr__(result, "evaluated_at", evaluated)
    object.__setattr__(result, "facets", comparisons)
    object.__setattr__(
        result,
        "comparison_digest_sha256",
        sha256(_canonical_json_bytes(payload)).hexdigest(),
    )
    object.__setattr__(result, "_artifact_evidence", artifact_evidence)
    object.__setattr__(result, "_artifact_attestation", artifact_attestation)
    object.__setattr__(result, "_artifact_verifier", artifact_verifier)
    result._validate()
    return result


def compare_live_parity(
    legacy: LiveParityInput,
    open_brain: LiveParityInput,
    *,
    evaluated_at: datetime,
    artifact_attestation: object,
    artifact_verifier: ArtifactAttestationVerifier,
) -> LiveParityResult:
    """Compare complete live sides using only live-scoped external evidence."""
    _validate_schema_registry()
    if type(legacy) is not LiveParityInput or type(open_brain) is not LiveParityInput:
        raise ParityValidationError("invalid live parity input")
    legacy._validate()
    open_brain._validate()
    evaluated = _utc(evaluated_at, "comparison evaluation time")
    if legacy.side is not ParitySide.LEGACY or open_brain.side is not ParitySide.OPEN_BRAIN:
        raise ParityValidationError("invalid comparison sides")
    if legacy.artifact != open_brain.artifact:
        raise ParityValidationError("invalid artifact binding")
    if (
        legacy.manifest_version != open_brain.manifest_version
        or legacy.schema_digest_sha256 != open_brain.schema_digest_sha256
    ):
        raise ParityValidationError("invalid parity manifest or schema")

    artifact_evidence = _verify_artifact_attestation(
        artifact_verifier,
        artifact_attestation,
        artifact=legacy.artifact,
        evaluated_at=evaluated,
        expected_scope=EvidenceScope.LIVE,
    )
    if (
        artifact_evidence.manifest_version != legacy.manifest_version
        or artifact_evidence.schema_digest_sha256 != legacy.schema_digest_sha256
    ):
        raise ParityValidationError("invalid artifact attestation binding")
    attestation_digest = artifact_evidence.attestation_digest_sha256
    digests = tuple(
        (
            legacy_snapshot.facet,
            legacy_snapshot._digest_sha256(attestation_digest_sha256=attestation_digest),
            open_brain_snapshot._digest_sha256(attestation_digest_sha256=attestation_digest),
            _metadata_unavailable(legacy_snapshot.metadata)
            or _metadata_unavailable(open_brain_snapshot.metadata),
        )
        for legacy_snapshot, open_brain_snapshot in zip(
            legacy.facets,
            open_brain.facets,
            strict=True,
        )
    )
    comparisons = tuple(
        FacetComparison(
            facet=facet,
            outcome=(
                ComparisonOutcome.MATCH
                if legacy_digest == open_brain_digest and not unavailable
                else ComparisonOutcome.BLOCKED_DIFFERENCE
            ),
            unavailable=unavailable,
            legacy_digest_sha256=legacy_digest,
            open_brain_digest_sha256=open_brain_digest,
            artifact_attestation_digest_sha256=attestation_digest,
        )
        for facet, legacy_digest, open_brain_digest, unavailable in digests
    )
    payload = _result_payload(
        artifact=legacy.artifact,
        artifact_evidence=artifact_evidence,
        evaluated_at=evaluated,
        facets=comparisons,
        scope=EvidenceScope.LIVE,
    )
    result = object.__new__(LiveParityResult)
    object.__setattr__(result, "manifest_version", legacy.manifest_version)
    object.__setattr__(result, "schema_digest_sha256", legacy.schema_digest_sha256)
    object.__setattr__(result, "artifact", legacy.artifact)
    object.__setattr__(result, "scope", EvidenceScope.LIVE)
    object.__setattr__(result, "evaluated_at", evaluated)
    object.__setattr__(result, "facets", comparisons)
    object.__setattr__(
        result,
        "comparison_digest_sha256",
        sha256(_canonical_json_bytes(payload)).hexdigest(),
    )
    object.__setattr__(result, "_artifact_evidence", artifact_evidence)
    object.__setattr__(result, "_artifact_attestation", artifact_attestation)
    object.__setattr__(result, "_artifact_verifier", artifact_verifier)
    result._validate()
    return result


def _verify_artifact_attestation(
    verifier: ArtifactAttestationVerifier,
    artifact_attestation: object,
    *,
    artifact: BuiltArtifactIdentity,
    evaluated_at: datetime,
    expected_scope: EvidenceScope,
) -> ArtifactAttestationEvidence:
    _validated_artifact(artifact)
    try:
        verify = verifier.verify_artifact_attestation
    except AttributeError:
        raise ParityValidationError("invalid artifact attestation verifier") from None
    if not callable(verify):
        raise ParityValidationError("invalid artifact attestation verifier")
    try:
        evidence = verify(artifact_attestation, evaluated_at=evaluated_at)
    except ParityValidationError:
        raise
    except Exception:
        raise ParityValidationError("artifact attestation verification failed") from None
    if type(evidence) is not ArtifactAttestationEvidence:
        raise ParityValidationError("invalid artifact attestation verifier result")
    _validate_artifact_attestation_evidence(
        evidence,
        expected_artifact=artifact,
        expected_evaluated_at=evaluated_at,
        expected_scope=expected_scope,
    )
    return evidence


def _result_payload(
    *,
    artifact: BuiltArtifactIdentity,
    artifact_evidence: ArtifactAttestationEvidence,
    evaluated_at: datetime,
    facets: tuple[FacetComparison, ...],
    scope: EvidenceScope,
) -> dict[str, object]:
    return {
        "artifact": _artifact_dict(artifact),
        "artifact_attestation": artifact_evidence.to_dict(),
        "evaluated_at": _timestamp(evaluated_at),
        "facets": [comparison.to_dict() for comparison in facets],
        "manifest_version": artifact_evidence.manifest_version,
        "redacted": True,
        "schema_digest_sha256": artifact_evidence.schema_digest_sha256,
        "scope": scope.value,
    }


def _validate_schema_registry() -> None:
    if not isinstance(_FACET_METADATA_TYPES, MappingProxyType):
        raise ParityValidationError("invalid parity schema registry")
    if tuple(_FACET_METADATA_TYPES.items()) != _ALL_EXPECTED_FACET_METADATA_TYPES:
        raise ParityValidationError("invalid parity schema registry")
    for _name, mapping in _ENUM_MAPPINGS:
        if not isinstance(mapping, MappingProxyType):
            raise ParityValidationError("invalid parity enum mapping")
    if not isinstance(_AUTHORITATIVE_FIELD_MAPPINGS, MappingProxyType):
        raise ParityValidationError("invalid authoritative field mapping registry")
    if not all(
        isinstance(mapping, MappingProxyType) for mapping in _AUTHORITATIVE_FIELD_MAPPINGS.values()
    ):
        raise ParityValidationError("invalid authoritative field mapping")
    current_digest = sha256(_canonical_json_bytes(_schema_definition())).hexdigest()
    if current_digest != PARITY_SCHEMA_DIGEST_SHA256:
        raise ParityValidationError("invalid parity schema digest")
    shadow_digest = sha256(_canonical_json_bytes(_shadow_schema_definition())).hexdigest()
    if shadow_digest != P7_W1_SHADOW_SCHEMA_DIGEST_SHA256:
        raise ParityValidationError("invalid shadow parity schema digest")
    if (
        PHASE7_FACET_MANIFEST.version != PARITY_HARNESS_VERSION
        or PHASE7_FACET_MANIFEST.facets != P7_W0_FACETS
        or PHASE7_FACET_MANIFEST.schema_digest_sha256 != PARITY_SCHEMA_DIGEST_SHA256
    ):
        raise ParityValidationError("invalid parity manifest")
    if (
        P7_W1_SHADOW_MANIFEST.version != P7_W1_SHADOW_VERSION
        or P7_W1_SHADOW_MANIFEST.facets != P7_W1_SHADOW_FACETS
        or P7_W1_SHADOW_MANIFEST.schema_digest_sha256 != P7_W1_SHADOW_SCHEMA_DIGEST_SHA256
    ):
        raise ParityValidationError("invalid shadow parity manifest")


def _normalize_metadata(metadata: FacetMetadata) -> dict[str, object]:
    metadata_type = type(metadata)
    if metadata_type is RequestContentMetadata:
        return metadata._normalized()
    if metadata_type is RawFileSetMetadata:
        return metadata._normalized()
    if metadata_type is QueueRetryMetadata:
        return metadata._normalized()
    if metadata_type is FrontmatterProvenanceMetadata:
        return metadata._normalized()
    if metadata_type is RoutingMetadata:
        return metadata._normalized()
    if metadata_type is LedgerCitationMetadata:
        return metadata._normalized()
    if metadata_type is ReviewProposalsMetadata:
        return metadata._normalized()
    if metadata_type is CliJsonMetadata:
        return metadata._normalized()
    if metadata_type is HealthDoctorMetadata:
        return metadata._normalized()
    if metadata_type is ShadowObservationMetadata:
        return metadata._normalized()
    raise ParityValidationError("invalid facet metadata type")


def _normalize_live_metadata(metadata: FacetMetadata) -> dict[str, object]:
    """Normalize cross-implementation live semantics without schema-name coupling."""
    if type(metadata) is CliJsonMetadata:
        normalized = metadata._normalized()
        return {
            "command": normalized["command"],
            "status": normalized["status"],
            "exit_class": normalized["exit_class"],
            "redacted": normalized["redacted"],
        }
    return _normalize_metadata(metadata)


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ParityValidationError(f"invalid {field} SHA-256")
    return value


def _require_sha256s(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ParityValidationError(f"invalid {field} SHA-256 inventory")
    for value in values:
        _require_sha256(value, field)
    if len(set(values)) != len(values):
        raise ParityValidationError(f"duplicate {field} SHA-256")
    return values


def _require_opaque_id(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ParityValidationError(f"invalid opaque {field}")
    match = _OPAQUE_ID.fullmatch(value)
    if match is None or match.group("prefix") in _DENIED_ID_PREFIXES:
        raise ParityValidationError(f"invalid opaque {field}")
    return value


def _require_opaque_ids(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ParityValidationError(f"invalid opaque {field} inventory")
    for value in values:
        _require_opaque_id(value, field)
    if len(set(values)) != len(values):
        raise ParityValidationError(f"duplicate opaque {field}")
    return values


def _require_positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ParityValidationError(f"invalid {field}")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ParityValidationError(f"invalid {field}")
    return value


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ParityValidationError(f"invalid {field}")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ParityValidationError("invalid normalized metadata") from None
