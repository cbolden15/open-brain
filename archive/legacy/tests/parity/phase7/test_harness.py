from __future__ import annotations

import ast
import builtins
import inspect
import json
import socket
import subprocess
from collections.abc import Callable
from copy import copy
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import MappingProxyType

import pytest
from open_brain_engine.capture.models import CaptureWorkItem as AuthoritativeCaptureWorkItem
from open_brain_engine.capture.models import QueueErrorCode as AuthoritativeQueueErrorCode
from open_brain_engine.capture.models import QueueItemState as AuthoritativeQueueState
from open_brain_engine.core.models import CaptureEnvelope as AuthoritativeCaptureEnvelope
from open_brain_engine.core.models import CaptureWhyOrigin as AuthoritativeOwnerContext
from open_brain_engine.core.models import ContentKind as AuthoritativeContentKind
from open_brain_engine.core.models import ContentOrigin as AuthoritativeContentOrigin
from open_brain_engine.core.models import Intent as AuthoritativeIntent
from open_brain_engine.core.models import PrivacyDecision as AuthoritativePrivacyDecision
from open_brain_engine.core.models import PrivacyTier as AuthoritativePrivacyTier
from open_brain_engine.core.models import Provenance as AuthoritativeProvenance
from open_brain_engine.core.models import SourceType as AuthoritativeSourceType
from open_brain_engine.review.models import Actor as AuthoritativeReviewActor
from open_brain_engine.review.models import ActorKind as AuthoritativeActorKind
from open_brain_engine.review.models import ReviewProposal as AuthoritativeReviewProposal
from open_brain_engine.review.models import ReviewState as AuthoritativeReviewState

import open_brain_legacy.parity as parity_module
import open_brain_legacy.parity.harness as harness_module
from open_brain_legacy._compat.open_brain.cli._common import _PUBLIC_OUTPUT_SCHEMA_KEYS
from open_brain_legacy._compat.open_brain.cli._common import ExitCode as AuthoritativeExitCode
from open_brain_legacy.operations.doctor import DoctorCheck as AuthoritativeDoctorCheck
from open_brain_legacy.operations.doctor import DoctorOutcome as AuthoritativeDoctorOutcome
from open_brain_legacy.operations.doctor import DoctorResult as AuthoritativeDoctorResult
from open_brain_legacy.operations.doctor import FindingClass as AuthoritativeFindingClass
from open_brain_legacy.operations.doctor import (
    HistoricalDiagnosis as AuthoritativeHistoricalDiagnosis,
)
from open_brain_legacy.operations.doctor import ProbeName as AuthoritativeProbeName
from open_brain_legacy.operations.doctor import ProbeReading as AuthoritativeProbeReading
from open_brain_legacy.operations.doctor import ProbeState as AuthoritativeProbeState
from open_brain_legacy.parity import (
    P7_W0_FACETS,
    PARITY_HARNESS_VERSION,
    PARITY_SCHEMA_DIGEST_SHA256,
    PHASE7_FACET_MANIFEST,
    ArtifactAttestationEvidence,
    BuiltArtifactIdentity,
    CliCommand,
    CliExitClass,
    CliJsonMetadata,
    CliProfile,
    CliStatus,
    ComparisonOutcome,
    ContentKind,
    ContentOrigin,
    DoctorProbe,
    DoctorProbeState,
    EvidenceScope,
    FacetMetadata,
    FrontmatterProvenanceMetadata,
    HealthDoctorMetadata,
    HealthFinding,
    HealthFindingClass,
    HealthOutcome,
    LedgerCitationMetadata,
    OwnerContext,
    ParityFacet,
    ParitySide,
    ParityValidationError,
    PrivacyTier,
    QueueErrorClass,
    QueueRetryMetadata,
    QueueState,
    QueueTransition,
    RawFileSetMetadata,
    RequestContentMetadata,
    RequestStatus,
    ReviewActorKind,
    ReviewIntent,
    ReviewProposal,
    ReviewProposalsMetadata,
    ReviewProposalState,
    RoutingDestination,
    RoutingMetadata,
    SourceKind,
    SyntheticFacetSnapshot,
    SyntheticParityInput,
    SyntheticParityResult,
    compare_synthetic_parity,
)

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64
_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
_ARTIFACT = BuiltArtifactIdentity(version="0.1.0", digest_sha256=_DIGEST_A)
_ATTESTATION = object()


def _id(prefix: str, digest: str) -> str:
    return f"{prefix}_{digest}"


def _authoritative_field_names(contract: type[object]) -> tuple[str, ...]:
    return tuple(
        field.name
        for field in fields(contract)  # type: ignore[arg-type]
    )


def _assert_authoritative_field_map(
    contract: type[object],
    mapping: MappingProxyType[str, str],
) -> None:
    assert isinstance(mapping, MappingProxyType)
    assert tuple(mapping) == _authoritative_field_names(contract)
    assert all(
        binding.startswith(("normalized:", "digest:", "opaque:", "expanded:", "excluded:"))
        for binding in mapping.values()
    )


_AUTHORITATIVE_FIELD_CONTRACTS = (
    (AuthoritativeCaptureWorkItem, "QUEUE_WORK_ITEM_FIELD_MAP"),
    (AuthoritativePrivacyDecision, "PRIVACY_DECISION_FIELD_MAP"),
    (AuthoritativeProvenance, "PROVENANCE_FIELD_MAP"),
    (AuthoritativeCaptureEnvelope, "CAPTURE_ENVELOPE_FIELD_MAP"),
    (AuthoritativeReviewActor, "REVIEW_ACTOR_FIELD_MAP"),
    (AuthoritativeReviewProposal, "REVIEW_PROPOSAL_FIELD_MAP"),
    (AuthoritativeProbeReading, "DOCTOR_READING_FIELD_MAP"),
    (AuthoritativeDoctorCheck, "DOCTOR_CHECK_FIELD_MAP"),
    (AuthoritativeHistoricalDiagnosis, "DOCTOR_HISTORICAL_FIELD_MAP"),
    (AuthoritativeDoctorResult, "DOCTOR_RESULT_FIELD_MAP"),
)


class _ArtifactVerifier:
    def __init__(
        self,
        *,
        attestation: object = _ATTESTATION,
        artifact: BuiltArtifactIdentity = _ARTIFACT,
        manifest_version: str = PARITY_HARNESS_VERSION,
        schema_digest_sha256: str = PARITY_SCHEMA_DIGEST_SHA256,
        expires_at: datetime = _NOW + timedelta(hours=1),
    ) -> None:
        self._attestation = attestation
        self._artifact = artifact
        self._manifest_version = manifest_version
        self._schema_digest_sha256 = schema_digest_sha256
        self._expires_at = expires_at
        self.calls = 0

    def verify_artifact_attestation(
        self,
        artifact_attestation: object,
        *,
        evaluated_at: datetime,
    ) -> ArtifactAttestationEvidence:
        self.calls += 1
        if artifact_attestation is not self._attestation:
            raise ParityValidationError("invalid artifact attestation")
        return ArtifactAttestationEvidence(
            verifier_id=_id("verifier", _DIGEST_A),
            attestation_id=_id("attestation", _DIGEST_B),
            attestation_digest_sha256=_DIGEST_C,
            artifact=self._artifact,
            manifest_version=self._manifest_version,
            schema_digest_sha256=self._schema_digest_sha256,
            scope=EvidenceScope.SYNTHETIC,
            evaluated_at=evaluated_at,
            expires_at=self._expires_at,
        )


def _compare(
    legacy: SyntheticParityInput,
    open_brain: SyntheticParityInput,
    *,
    evaluated_at: datetime = _NOW,
    artifact_attestation: object = _ATTESTATION,
    artifact_verifier: object | None = None,
) -> SyntheticParityResult:
    verifier = _ArtifactVerifier() if artifact_verifier is None else artifact_verifier
    return compare_synthetic_parity(
        legacy,
        open_brain,
        evaluated_at=evaluated_at,
        artifact_attestation=artifact_attestation,
        artifact_verifier=verifier,  # type: ignore[arg-type]
    )


def _metadata() -> dict[ParityFacet, FacetMetadata]:
    return {
        ParityFacet.REQUEST_CONTENT: RequestContentMetadata(
            request_status=RequestStatus.COMPLETED,
            request_id=_id("request", _DIGEST_A),
            content_ids=(_id("content", _DIGEST_B), _id("content", _DIGEST_A)),
        ),
        ParityFacet.RAW_FILE_SET: RawFileSetMetadata(
            file_digests_sha256=(_DIGEST_B, _DIGEST_A),
        ),
        ParityFacet.QUEUE_RETRY: QueueRetryMetadata(
            transitions=(
                QueueTransition(QueueState.PENDING, QueueState.PROCESSING, 0, None),
                QueueTransition(QueueState.PROCESSING, QueueState.ACKNOWLEDGED, 0, None),
            ),
        ),
        ParityFacet.FRONTMATTER_PROVENANCE: FrontmatterProvenanceMetadata(
            schema_version=1,
            content_kind=ContentKind.ARTICLE,
            privacy_tier=PrivacyTier.WORK,
            source_kind=SourceKind.TEXT,
            source_ref_digest_sha256=_DIGEST_B,
            content_origin=ContentOrigin.OWNER_AUTHORED,
            owner_context=OwnerContext.OWNER_AUTHORED,
            redaction_policy_version=1,
        ),
        ParityFacet.ROUTING: RoutingMetadata(destination=RoutingDestination.WORK),
        ParityFacet.LEDGER_CITATIONS: LedgerCitationMetadata(
            ledger_item_ids=(_id("ledger", _DIGEST_B), _id("ledger", _DIGEST_A)),
            citation_ids=(_id("citation", _DIGEST_B), _id("citation", _DIGEST_A)),
        ),
        ParityFacet.REVIEW_PROPOSALS: ReviewProposalsMetadata(
            proposals=(
                ReviewProposal(
                    schema_version=1,
                    review_id=_id("review", _DIGEST_B),
                    capture_id=_id("capture", _DIGEST_B),
                    source_ref_digest_sha256=_DIGEST_B,
                    privacy_tier=PrivacyTier.WORK,
                    proposed_intent=ReviewIntent.ACTION_CANDIDATE,
                    proposal_reason_digest_sha256=_DIGEST_C,
                    capture_why_digest_sha256=_DIGEST_D,
                    state=ReviewProposalState.OPEN,
                    created_at=_NOW,
                    actor_kind=ReviewActorKind.OWNER,
                    actor_label_digest_sha256=_DIGEST_A,
                ),
                ReviewProposal(
                    schema_version=1,
                    review_id=_id("review", _DIGEST_A),
                    capture_id=_id("capture", _DIGEST_A),
                    source_ref_digest_sha256=_DIGEST_A,
                    privacy_tier=PrivacyTier.PERSONAL,
                    proposed_intent=ReviewIntent.IDEA,
                    proposal_reason_digest_sha256=_DIGEST_B,
                    capture_why_digest_sha256=_DIGEST_C,
                    state=ReviewProposalState.OPEN,
                    created_at=_NOW,
                    actor_kind=ReviewActorKind.SYSTEM,
                    actor_label_digest_sha256=_DIGEST_D,
                ),
            ),
        ),
        ParityFacet.CLI_JSON: CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.STATUS,
            status=CliStatus.COMPLETED,
            exit_class=CliExitClass.SUCCESS,
            field_digests=tuple(
                (field, _DIGEST_A)
                for field in ("command", "metrics", "schema_version", "status", "strict")
            ),
        ),
        ParityFacet.HEALTH_DOCTOR: HealthDoctorMetadata(
            outcome=HealthOutcome.HEALTHY,
            findings=(),
        ),
    }


def _input(
    side: ParitySide,
    *,
    metadata: dict[ParityFacet, FacetMetadata] | None = None,
    artifact: BuiltArtifactIdentity = _ARTIFACT,
) -> SyntheticParityInput:
    supplied = _metadata() if metadata is None else metadata
    return SyntheticParityInput(
        side=side,
        artifact=artifact,
        facets=tuple(
            SyntheticFacetSnapshot(facet=facet, artifact=artifact, metadata=supplied[facet])
            for facet in P7_W0_FACETS
        ),
    )


def _changed_metadata() -> dict[ParityFacet, FacetMetadata]:
    return {
        ParityFacet.REQUEST_CONTENT: RequestContentMetadata(
            request_status=RequestStatus.FAILED,
            request_id=_id("request", _DIGEST_A),
            content_ids=(_id("content", _DIGEST_B), _id("content", _DIGEST_A)),
        ),
        ParityFacet.RAW_FILE_SET: RawFileSetMetadata((_DIGEST_C,)),
        ParityFacet.QUEUE_RETRY: QueueRetryMetadata(
            (
                QueueTransition(
                    QueueState.PENDING,
                    QueueState.QUARANTINED,
                    2,
                    QueueErrorClass.RETRY_EXHAUSTED,
                ),
            )
        ),
        ParityFacet.FRONTMATTER_PROVENANCE: FrontmatterProvenanceMetadata(
            schema_version=1,
            content_kind=ContentKind.ARTICLE,
            privacy_tier=PrivacyTier.SECRET,
            source_kind=SourceKind.TEXT,
            source_ref_digest_sha256=_DIGEST_B,
            content_origin=ContentOrigin.MIXED,
            owner_context=OwnerContext.OWNER_AUTHORED,
            redaction_policy_version=1,
        ),
        ParityFacet.ROUTING: RoutingMetadata(RoutingDestination.REVIEW),
        ParityFacet.LEDGER_CITATIONS: LedgerCitationMetadata(
            ledger_item_ids=(_id("ledger", _DIGEST_C),),
            citation_ids=(_id("citation", _DIGEST_C),),
        ),
        ParityFacet.REVIEW_PROPOSALS: ReviewProposalsMetadata(
            (
                ReviewProposal(
                    schema_version=1,
                    review_id=_id("review", _DIGEST_C),
                    capture_id=_id("capture", _DIGEST_C),
                    source_ref_digest_sha256=_DIGEST_C,
                    privacy_tier=PrivacyTier.PUBLIC,
                    proposed_intent=ReviewIntent.IDEA,
                    proposal_reason_digest_sha256=_DIGEST_D,
                    capture_why_digest_sha256=_DIGEST_A,
                    state=ReviewProposalState.DEFERRED,
                    created_at=_NOW + timedelta(minutes=1),
                    actor_kind=ReviewActorKind.SYSTEM,
                    actor_label_digest_sha256=_DIGEST_B,
                ),
            )
        ),
        ParityFacet.CLI_JSON: CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.STATUS,
            status=CliStatus.FAILED,
            exit_class=CliExitClass.FAILURE,
            field_digests=tuple(
                (field, _DIGEST_C)
                for field in ("command", "metrics", "schema_version", "status", "strict")
            ),
        ),
        ParityFacet.HEALTH_DOCTOR: HealthDoctorMetadata(
            outcome=HealthOutcome.UNHEALTHY,
            findings=(
                HealthFinding(
                    probe=DoctorProbe.QUEUE_AGE,
                    finding_class=HealthFindingClass.QUEUE_STALE,
                    state=DoctorProbeState.UNHEALTHY,
                ),
            ),
        ),
    }


def test_phase7_manifest_is_fixed_versioned_and_complete() -> None:
    assert PARITY_HARNESS_VERSION == "phase7-wave0-v1"
    assert PARITY_SCHEMA_DIGEST_SHA256 == (
        "36248bb91e50ac4be90d5cd45faa3a7a60180ef38edcc5c6ff99090fe5a6174a"
    )
    assert PHASE7_FACET_MANIFEST.version == PARITY_HARNESS_VERSION
    assert PHASE7_FACET_MANIFEST.schema_digest_sha256 == PARITY_SCHEMA_DIGEST_SHA256
    assert PHASE7_FACET_MANIFEST.facets == P7_W0_FACETS
    assert tuple(facet.value for facet in PHASE7_FACET_MANIFEST.facets) == (
        "PAR7-001",
        "PAR7-002",
        "PAR7-003",
        "PAR7-004",
        "PAR7-005",
        "PAR7-006",
        "PAR7-007",
        "PAR7-008",
        "PAR7-009",
    )


def test_identical_explicit_sides_match_all_facets_and_render_deterministically() -> None:
    legacy = _input(ParitySide.LEGACY)
    open_brain = _input(ParitySide.OPEN_BRAIN)

    first = _compare(legacy, open_brain)
    second = _compare(legacy, open_brain)

    assert first.scope is EvidenceScope.SYNTHETIC
    assert first.resolved is True
    assert tuple(item.facet for item in first.facets) == P7_W0_FACETS
    assert {item.outcome for item in first.facets} == {ComparisonOutcome.MATCH}
    assert first.to_dict() == second.to_dict()
    rendered = json.dumps(first.to_dict(), allow_nan=False, sort_keys=True)
    assert first.to_dict() == {
        "artifact": {
            "distribution": "open-brain",
            "version": "0.1.0",
            "digest_sha256": _DIGEST_A,
        },
        "artifact_attestation": first.artifact_attestation.to_dict(),
        "comparison_digest_sha256": first.comparison_digest_sha256,
        "evaluated_at": "2026-08-14T12:00:00Z",
        "facets": [item.to_dict() for item in first.facets],
        "manifest_version": PARITY_HARNESS_VERSION,
        "redacted": True,
        "schema_digest_sha256": PARITY_SCHEMA_DIGEST_SHA256,
        "scope": "synthetic",
    }
    assert "cutover" not in rendered
    assert "production" not in rendered
    assert "private-capture-body" not in rendered


def test_set_like_metadata_normalizes_without_mutating_inputs() -> None:
    legacy_metadata = _metadata()
    open_brain_metadata = _metadata()
    request = open_brain_metadata[ParityFacet.REQUEST_CONTENT]
    raw_file_set = open_brain_metadata[ParityFacet.RAW_FILE_SET]
    ledger_citations = open_brain_metadata[ParityFacet.LEDGER_CITATIONS]
    review_proposals = open_brain_metadata[ParityFacet.REVIEW_PROPOSALS]
    cli_json = open_brain_metadata[ParityFacet.CLI_JSON]
    assert isinstance(request, RequestContentMetadata)
    assert isinstance(raw_file_set, RawFileSetMetadata)
    assert isinstance(ledger_citations, LedgerCitationMetadata)
    assert isinstance(review_proposals, ReviewProposalsMetadata)
    assert isinstance(cli_json, CliJsonMetadata)
    open_brain_metadata[ParityFacet.REQUEST_CONTENT] = replace(
        request,
        content_ids=tuple(reversed(request.content_ids)),
    )
    open_brain_metadata[ParityFacet.RAW_FILE_SET] = RawFileSetMetadata(
        tuple(reversed(raw_file_set.file_digests_sha256))
    )
    open_brain_metadata[ParityFacet.LEDGER_CITATIONS] = LedgerCitationMetadata(
        ledger_item_ids=tuple(reversed(ledger_citations.ledger_item_ids)),
        citation_ids=tuple(reversed(ledger_citations.citation_ids)),
    )
    open_brain_metadata[ParityFacet.REVIEW_PROPOSALS] = ReviewProposalsMetadata(
        tuple(reversed(review_proposals.proposals))
    )
    open_brain_metadata[ParityFacet.CLI_JSON] = replace(
        cli_json,
        field_digests=tuple(reversed(cli_json.field_digests)),
    )
    original = repr(open_brain_metadata)

    result = _compare(
        _input(ParitySide.LEGACY, metadata=legacy_metadata),
        _input(ParitySide.OPEN_BRAIN, metadata=open_brain_metadata),
    )

    assert result.resolved is True
    assert {item.outcome for item in result.facets} == {ComparisonOutcome.MATCH}
    assert repr(open_brain_metadata) == original


@pytest.mark.parametrize("facet", P7_W0_FACETS)
def test_each_unapproved_difference_is_blocked(facet: ParityFacet) -> None:
    changed = _metadata()
    changed[facet] = _changed_metadata()[facet]

    result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN, metadata=changed),
    )

    outcomes = {item.facet: item.outcome for item in result.facets}
    assert result.resolved is False
    assert outcomes[facet] is ComparisonOutcome.BLOCKED_DIFFERENCE
    assert sum(value is ComparisonOutcome.BLOCKED_DIFFERENCE for value in outcomes.values()) == 1


@pytest.mark.parametrize(
    ("facet", "metadata"),
    (
        (
            ParityFacet.REQUEST_CONTENT,
            RequestContentMetadata(
                request_status=RequestStatus.UNAVAILABLE,
                request_id=_id("request", _DIGEST_A),
                content_ids=(_id("content", _DIGEST_A),),
            ),
        ),
        (
            ParityFacet.ROUTING,
            RoutingMetadata(destination=RoutingDestination.UNAVAILABLE),
        ),
    ),
)
def test_equal_unavailable_facets_remain_blocked(
    facet: ParityFacet,
    metadata: FacetMetadata,
) -> None:
    values = _metadata()
    values[facet] = metadata

    result = _compare(
        _input(ParitySide.LEGACY, metadata=values),
        _input(ParitySide.OPEN_BRAIN, metadata=values),
    )
    comparison = result.for_facet(facet)

    assert comparison.legacy_digest_sha256 == comparison.open_brain_digest_sha256
    assert comparison.unavailable is True
    assert comparison.outcome is ComparisonOutcome.BLOCKED_DIFFERENCE
    assert result.resolved is False


def test_owner_approved_differences_have_no_public_resolution_path() -> None:
    changed = _metadata()
    changed[ParityFacet.ROUTING] = _changed_metadata()[ParityFacet.ROUTING]
    legacy = _input(ParitySide.LEGACY)
    open_brain = _input(ParitySide.OPEN_BRAIN, metadata=changed)
    result = _compare(legacy, open_brain)

    assert result.resolved is False
    assert result.for_facet(ParityFacet.ROUTING).outcome is ComparisonOutcome.BLOCKED_DIFFERENCE
    with pytest.raises(TypeError, match="decision_receipts"):
        compare_synthetic_parity(
            legacy,
            open_brain,
            evaluated_at=_NOW,
            artifact_attestation=_ATTESTATION,
            artifact_verifier=_ArtifactVerifier(),
            decision_receipts=(),  # type: ignore[call-arg]
        )


def test_artifact_evidence_is_reverified_at_consumption_and_serialization() -> None:
    verifier = _ArtifactVerifier()
    result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN),
        artifact_verifier=verifier,
    )

    assert verifier.calls == 2
    assert result.resolved is True
    assert verifier.calls == 3
    result.for_facet(ParityFacet.ROUTING)
    assert verifier.calls == 4
    result.to_dict()
    assert verifier.calls == 5

    object.__setattr__(result, "comparison_digest_sha256", _DIGEST_D)
    with pytest.raises(ParityValidationError, match="comparison result"):
        result.to_dict()


def test_incomplete_duplicate_reordered_and_mismatched_inventories_fail_closed() -> None:
    valid = _input(ParitySide.LEGACY)
    reordered = (valid.facets[1], valid.facets[0], *valid.facets[2:])
    invalid_inventories = (
        valid.facets[:-1],
        (*valid.facets, valid.facets[-1]),
        reordered,
    )
    for facets in invalid_inventories:
        with pytest.raises(ParityValidationError, match="facet inventory"):
            SyntheticParityInput(
                side=ParitySide.LEGACY,
                artifact=_ARTIFACT,
                facets=facets,
            )

    other_artifact = BuiltArtifactIdentity(version="0.1.1", digest_sha256=_DIGEST_D)
    mismatched = (
        replace(valid.facets[0], artifact=other_artifact),
        *valid.facets[1:],
    )
    with pytest.raises(ParityValidationError, match="artifact binding"):
        SyntheticParityInput(
            side=ParitySide.LEGACY,
            artifact=_ARTIFACT,
            facets=mismatched,
        )
    with pytest.raises(ParityValidationError, match="artifact binding"):
        _compare(
            valid,
            _input(ParitySide.OPEN_BRAIN, artifact=other_artifact),
        )


def test_raw_paths_urls_secrets_topology_nan_and_unknown_metadata_fail_without_echo() -> None:
    canaries = (
        "/" + "/".join(("Users", "example", "private.md")),
        "https://example.invalid/private",
        "token_deadbeefdeadbeef",
        "host_deadbeefdeadbeef",
        "raw private capture body",
    )
    for canary in canaries:
        with pytest.raises(ParityValidationError) as caught:
            RequestContentMetadata(
                request_status=RequestStatus.COMPLETED,
                request_id=canary,
                content_ids=(_id("content", _DIGEST_A),),
            )
        assert canary not in str(caught.value)

    with pytest.raises(ParityValidationError, match="SHA-256"):
        RawFileSetMetadata(("raw private capture body",))
    with pytest.raises(ParityValidationError, match="exit code"):
        CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.STATUS,
            status=CliStatus.COMPLETED,
            exit_class=float("nan"),  # type: ignore[arg-type]
            field_digests=tuple(
                (field, _DIGEST_A)
                for field in ("command", "metrics", "schema_version", "status", "strict")
            ),
        )
    with pytest.raises(ParityValidationError, match="metadata type"):
        SyntheticFacetSnapshot(
            facet=ParityFacet.CLI_JSON,
            artifact=_ARTIFACT,
            metadata={"status": "completed", "body": "raw"},  # type: ignore[arg-type]
        )


def test_asserted_expired_and_mismatched_artifacts_fail_closed() -> None:
    legacy = _input(ParitySide.LEGACY)
    open_brain = _input(ParitySide.OPEN_BRAIN)

    with pytest.raises(ParityValidationError, match="artifact attestation"):
        compare_synthetic_parity(
            legacy,
            open_brain,
            evaluated_at=_NOW,
            artifact_attestation=object(),
            artifact_verifier=_ArtifactVerifier(),
        )
    with pytest.raises(ParityValidationError, match="artifact attestation"):
        _compare(
            legacy,
            open_brain,
            artifact_verifier=_ArtifactVerifier(expires_at=_NOW),
        )
    other_artifact = BuiltArtifactIdentity(version="0.1.1", digest_sha256=_DIGEST_D)
    with pytest.raises(ParityValidationError, match="artifact attestation"):
        _compare(
            legacy,
            open_brain,
            artifact_verifier=_ArtifactVerifier(artifact=other_artifact),
        )


def test_comparison_has_no_ambient_io_or_legacy_import_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ambient operation attempted")

    monkeypatch.setattr(builtins, "open", unexpected)
    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(socket, "create_connection", unexpected)

    result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN),
    )

    assert result.resolved is True
    tree = ast.parse(inspect.getsource(harness_module))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported_roots <= {
        "__future__",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "re",
        "types",
        "typing",
    }


def test_public_api_has_no_decision_minting_or_direct_result_factory() -> None:
    assert not hasattr(parity_module, "DecisionReceiptIssuer")
    assert not hasattr(parity_module, "DecisionReceipt")
    assert not hasattr(parity_module, "ApprovalKind")
    assert not hasattr(SyntheticParityResult, "_create")
    assert tuple(ComparisonOutcome) == (
        ComparisonOutcome.MATCH,
        ComparisonOutcome.BLOCKED_DIFFERENCE,
    )
    with pytest.raises(TypeError, match="harness-created"):
        SyntheticParityResult()


def test_asserted_artifact_requires_an_external_attestation_verifier() -> None:
    parameters = inspect.signature(compare_synthetic_parity).parameters

    assert "artifact_attestation" in parameters
    assert "artifact_verifier" in parameters
    assert "decision_receipts" not in parameters
    assert "receipt_issuer" not in parameters

    result = _compare(_input(ParitySide.LEGACY), _input(ParitySide.OPEN_BRAIN))
    evidence = result.artifact_attestation
    assert evidence.verifier_id == _id("verifier", _DIGEST_A)
    assert evidence.attestation_id == _id("attestation", _DIGEST_B)
    assert evidence.attestation_digest_sha256 == _DIGEST_C
    assert evidence.manifest_version == PARITY_HARNESS_VERSION
    assert evidence.schema_digest_sha256 == PARITY_SCHEMA_DIGEST_SHA256
    assert evidence.evaluated_at == _NOW
    assert evidence.expires_at == _NOW + timedelta(hours=1)
    assert {facet.artifact_attestation_digest_sha256 for facet in result.facets} == {_DIGEST_C}


def test_built_artifact_identity_is_runtime_final_even_with_post_init_override() -> None:
    path_canary = "/synthetic/private/path"
    body_canary = "raw-synthetic-body"

    with pytest.raises(TypeError) as caught:

        class ForgedConstruction(BuiltArtifactIdentity):
            def __post_init__(self) -> None:
                return

        ForgedConstruction(version=path_canary, digest_sha256=body_canary)

    assert path_canary not in str(caught.value)
    assert body_canary not in str(caught.value)


def test_forged_artifact_values_fail_every_boundary_without_canary_leak() -> None:
    path_canary = "/synthetic/private/path"
    body_canary = "raw-synthetic-body"

    forged = object.__new__(BuiltArtifactIdentity)
    object.__setattr__(forged, "version", path_canary)
    object.__setattr__(forged, "digest_sha256", body_canary)
    boundary_calls = (
        lambda: BuiltArtifactIdentity.to_dict(forged),
        lambda: ArtifactAttestationEvidence(
            verifier_id=_id("verifier", _DIGEST_A),
            attestation_id=_id("attestation", _DIGEST_B),
            attestation_digest_sha256=_DIGEST_C,
            artifact=forged,
            manifest_version=PARITY_HARNESS_VERSION,
            schema_digest_sha256=PARITY_SCHEMA_DIGEST_SHA256,
            scope=EvidenceScope.SYNTHETIC,
            evaluated_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        ),
        lambda: SyntheticFacetSnapshot(
            facet=ParityFacet.ROUTING,
            artifact=forged,
            metadata=RoutingMetadata(RoutingDestination.WORK),
        ),
        lambda: _input(ParitySide.LEGACY, artifact=forged),
    )
    for boundary_call in boundary_calls:
        with pytest.raises(ParityValidationError) as boundary_error:
            boundary_call()
        assert path_canary not in str(boundary_error.value)
        assert body_canary not in str(boundary_error.value)

    valid_result = _compare(_input(ParitySide.LEGACY), _input(ParitySide.OPEN_BRAIN))
    object.__setattr__(valid_result, "artifact", forged)
    for result_boundary in (
        lambda: valid_result.resolved,
        valid_result.to_dict,
        lambda: valid_result.artifact_attestation,
    ):
        with pytest.raises(ParityValidationError) as result_error:
            result_boundary()
        assert path_canary not in str(result_error.value)
        assert body_canary not in str(result_error.value)


def test_facet_schema_registry_cannot_be_mutated() -> None:
    registry = harness_module._FACET_METADATA_TYPES
    original = registry[ParityFacet.ROUTING]
    try:
        with pytest.raises(TypeError):
            registry[ParityFacet.ROUTING] = RequestContentMetadata  # type: ignore[index]
    finally:
        if isinstance(registry, dict):
            registry[ParityFacet.ROUTING] = original

    class DerivedRoutingMetadata(RoutingMetadata):
        pass

    with pytest.raises(ParityValidationError, match="metadata type"):
        SyntheticFacetSnapshot(
            facet=ParityFacet.ROUTING,
            artifact=_ARTIFACT,
            metadata=DerivedRoutingMetadata(RoutingDestination.WORK),
        )


def test_cli_exit_inventory_rejects_exit_200() -> None:
    with pytest.raises(ParityValidationError, match="exit"):
        CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.STATUS,
            status=CliStatus.FAILED,
            exit_class=200,  # type: ignore[arg-type]
            field_digests=tuple(
                (field, _DIGEST_A)
                for field in ("command", "metrics", "schema_version", "status", "strict")
            ),
        )


def test_every_authoritative_enum_mapping_is_total_immutable_and_one_to_one() -> None:
    inventories = (
        ("QUEUE_STATE_MAP", "QueueState", tuple(AuthoritativeQueueState)),
        ("QUEUE_ERROR_MAP", "QueueErrorClass", tuple(AuthoritativeQueueErrorCode)),
        ("CONTENT_KIND_MAP", "ContentKind", tuple(AuthoritativeContentKind)),
        ("PRIVACY_TIER_MAP", "PrivacyTier", tuple(AuthoritativePrivacyTier)),
        ("SOURCE_TYPE_MAP", "SourceKind", tuple(AuthoritativeSourceType)),
        ("CONTENT_ORIGIN_MAP", "ContentOrigin", tuple(AuthoritativeContentOrigin)),
        ("OWNER_CONTEXT_MAP", "OwnerContext", tuple(AuthoritativeOwnerContext)),
        ("REVIEW_STATE_MAP", "ReviewProposalState", tuple(AuthoritativeReviewState)),
        ("REVIEW_ACTOR_MAP", "ReviewActorKind", tuple(AuthoritativeActorKind)),
        (
            "REVIEW_INTENT_MAP",
            "ReviewIntent",
            (AuthoritativeIntent.IDEA, AuthoritativeIntent.ACTION_CANDIDATE),
        ),
        ("CLI_EXIT_CLASS_MAP", "CliExitClass", tuple(AuthoritativeExitCode)),
        ("DOCTOR_PROBE_MAP", "DoctorProbe", tuple(AuthoritativeProbeName)),
        ("DOCTOR_STATE_MAP", "DoctorProbeState", tuple(AuthoritativeProbeState)),
        ("DOCTOR_FINDING_MAP", "HealthFindingClass", tuple(AuthoritativeFindingClass)),
        ("HEALTH_OUTCOME_MAP", "HealthOutcome", tuple(AuthoritativeDoctorOutcome)),
    )

    for mapping_name, parity_enum_name, authoritative_members in inventories:
        mapping = getattr(parity_module, mapping_name)
        parity_enum = getattr(parity_module, parity_enum_name)
        assert isinstance(mapping, MappingProxyType)
        assert tuple(mapping) == authoritative_members
        assert len(mapping) == len(authoritative_members)
        assert len(set(mapping.values())) == len(mapping)
        assert set(mapping.values()) == set(parity_enum)


def test_cli_closed_key_inventory_matches_authoritative_contract_exactly() -> None:
    assert isinstance(parity_module.CLI_OUTPUT_KEY_MAP, MappingProxyType)
    assert {key: key for key in _PUBLIC_OUTPUT_SCHEMA_KEYS} == parity_module.CLI_OUTPUT_KEY_MAP

    with pytest.raises(ParityValidationError, match="CLI output field"):
        CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.STATUS,
            status=CliStatus.COMPLETED,
            exit_class=CliExitClass.SUCCESS,
            field_digests=(("unknown_growth", _DIGEST_A),),
        )


def test_cli_profile_rejects_missing_cross_profile_and_wrong_command_fields() -> None:
    status_fields = ("command", "metrics", "schema_version", "status", "strict")
    with pytest.raises(ParityValidationError, match="profile field inventory"):
        CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.STATUS,
            status=CliStatus.COMPLETED,
            exit_class=CliExitClass.SUCCESS,
            field_digests=tuple((field, _DIGEST_A) for field in status_fields[:-1]),
        )
    with pytest.raises(ParityValidationError, match="CLI output field"):
        CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.STATUS,
            status=CliStatus.COMPLETED,
            exit_class=CliExitClass.SUCCESS,
            field_digests=tuple(
                ("run_count" if field == "metrics" else field, _DIGEST_A)
                for field in status_fields
            ),
        )
    with pytest.raises(ParityValidationError, match="profile command"):
        CliJsonMetadata(
            profile=CliProfile.OPEN_BRAIN_STATUS,
            command=CliCommand.CRON,
            status=CliStatus.COMPLETED,
            exit_class=CliExitClass.SUCCESS,
            field_digests=tuple((field, _DIGEST_A) for field in status_fields),
        )


def test_authoritative_field_maps_are_total_immutable_and_explicit() -> None:
    for contract, mapping_name in _AUTHORITATIVE_FIELD_CONTRACTS:
        mapping = getattr(parity_module, mapping_name)
        _assert_authoritative_field_map(contract, mapping)

    cli_mapping = parity_module.CLI_FIELD_MAP
    assert isinstance(cli_mapping, MappingProxyType)
    assert set(cli_mapping) == _PUBLIC_OUTPUT_SCHEMA_KEYS
    assert all(
        binding.startswith(("normalized:", "digest:", "opaque:", "excluded:"))
        for binding in cli_mapping.values()
    )

    assert parity_module.QUEUE_WORK_ITEM_FIELD_MAP["attempt_count"] == (
        "normalized:QueueTransition.attempt_count"
    )
    assert parity_module.PROVENANCE_FIELD_MAP["source_ref"] == (
        "digest:FrontmatterProvenanceMetadata.source_ref_digest_sha256"
    )
    assert parity_module.REVIEW_PROPOSAL_FIELD_MAP["review_id"] == (
        "opaque:ReviewProposal.review_id"
    )
    assert parity_module.REVIEW_PROPOSAL_FIELD_MAP["proposal_reason"] == (
        "digest:ReviewProposal.proposal_reason_digest_sha256"
    )
    assert parity_module.REVIEW_ACTOR_FIELD_MAP["label"] == (
        "digest:ReviewProposal.actor_label_digest_sha256"
    )
    assert parity_module.CLI_FIELD_MAP["excerpt"] == (
        "digest:CliJsonMetadata.field_digests.excerpt"
    )
    assert parity_module.DOCTOR_CHECK_FIELD_MAP["probe"] == ("normalized:HealthFinding.probe")

    schema_definition = harness_module._schema_definition()
    schema_mappings = schema_definition["authoritative_field_mappings"]
    assert isinstance(schema_mappings, dict)
    assert set(schema_mappings) == {
        mapping_name for _, mapping_name in _AUTHORITATIVE_FIELD_CONTRACTS
    } | {"CLI_FIELD_MAP"}
    assert (
        sha256(harness_module._canonical_json_bytes(schema_definition)).hexdigest()
        == PARITY_SCHEMA_DIGEST_SHA256
    )


@pytest.mark.parametrize(("contract", "mapping_name"), _AUTHORITATIVE_FIELD_CONTRACTS)
def test_authoritative_dataclass_field_growth_fails_until_explicitly_mapped(
    contract: type[object],
    mapping_name: str,
) -> None:
    authoritative_fields = contract.__dataclass_fields__  # type: ignore[attr-defined]
    original = dict(authoritative_fields)
    future = copy(next(iter(original.values())))
    future.name = "future_authoritative_field"
    try:
        authoritative_fields[future.name] = future
        with pytest.raises(AssertionError):
            _assert_authoritative_field_map(
                contract,
                getattr(parity_module, mapping_name),
            )
    finally:
        authoritative_fields.clear()
        authoritative_fields.update(original)


def test_authoritative_cli_field_growth_fails_until_explicitly_mapped() -> None:
    future_authoritative_keys = _PUBLIC_OUTPUT_SCHEMA_KEYS | {"future_authoritative_field"}

    with pytest.raises(AssertionError):
        assert set(parity_module.CLI_FIELD_MAP) == future_authoritative_keys


def test_normalized_field_inventories_cover_every_reviewed_dimension() -> None:
    for contract, mapping_name in _AUTHORITATIVE_FIELD_CONTRACTS:
        _assert_authoritative_field_map(
            contract,
            getattr(parity_module, mapping_name),
        )
    assert set(parity_module.CLI_FIELD_MAP) == _PUBLIC_OUTPUT_SCHEMA_KEYS

    assert tuple(field.name for field in fields(parity_module.QueueTransition)) == (
        "from_state",
        "to_state",
        "attempt_count",
        "last_error_code",
    )
    assert tuple(field.name for field in fields(parity_module.FrontmatterProvenanceMetadata)) == (
        "schema_version",
        "content_kind",
        "privacy_tier",
        "source_kind",
        "source_ref_digest_sha256",
        "content_origin",
        "owner_context",
        "redaction_policy_version",
    )
    assert tuple(field.name for field in fields(parity_module.ReviewProposal)) == (
        "schema_version",
        "review_id",
        "capture_id",
        "source_ref_digest_sha256",
        "privacy_tier",
        "proposed_intent",
        "proposal_reason_digest_sha256",
        "capture_why_digest_sha256",
        "state",
        "created_at",
        "actor_kind",
        "actor_label_digest_sha256",
    )
    assert tuple(field.name for field in fields(parity_module.CliJsonMetadata)) == (
        "profile",
        "command",
        "status",
        "exit_class",
        "field_digests",
    )
    assert tuple(field.name for field in fields(parity_module.HealthFinding)) == (
        "probe",
        "finding_class",
        "state",
    )


@pytest.mark.parametrize(
    "change",
    (
        lambda transition: replace(transition, attempt_count=1),
        lambda transition: replace(
            transition,
            last_error_code=QueueErrorClass.RETRYABLE_FAILURE,
        ),
    ),
)
def test_queue_retry_dimensions_change_the_normalized_digest(
    change: Callable[[QueueTransition], QueueTransition],
) -> None:
    metadata = _metadata()
    queue = metadata[ParityFacet.QUEUE_RETRY]
    assert isinstance(queue, QueueRetryMetadata)
    changed_transition = change(queue.transitions[0])
    metadata[ParityFacet.QUEUE_RETRY] = QueueRetryMetadata(
        (changed_transition, *queue.transitions[1:])
    )

    result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN, metadata=metadata),
    )

    assert result.for_facet(ParityFacet.QUEUE_RETRY).outcome is ComparisonOutcome.BLOCKED_DIFFERENCE


@pytest.mark.parametrize(
    "change",
    (
        lambda provenance: replace(provenance, privacy_tier=PrivacyTier.PERSONAL),
        lambda provenance: replace(provenance, source_ref_digest_sha256=_DIGEST_C),
        lambda provenance: replace(
            provenance,
            content_origin=ContentOrigin.THIRD_PARTY,
        ),
        lambda provenance: replace(
            provenance,
            owner_context=OwnerContext.AUTOMATION_ABSENT,
        ),
    ),
)
def test_provenance_and_privacy_dimensions_change_the_normalized_digest(
    change: Callable[[FrontmatterProvenanceMetadata], FrontmatterProvenanceMetadata],
) -> None:
    metadata = _metadata()
    provenance = metadata[ParityFacet.FRONTMATTER_PROVENANCE]
    assert isinstance(provenance, FrontmatterProvenanceMetadata)
    metadata[ParityFacet.FRONTMATTER_PROVENANCE] = change(provenance)

    result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN, metadata=metadata),
    )

    assert (
        result.for_facet(ParityFacet.FRONTMATTER_PROVENANCE).outcome
        is ComparisonOutcome.BLOCKED_DIFFERENCE
    )


@pytest.mark.parametrize(
    "change",
    (
        lambda proposal: replace(proposal, capture_id=_id("capture", _DIGEST_C)),
        lambda proposal: replace(proposal, source_ref_digest_sha256=_DIGEST_C),
        lambda proposal: replace(proposal, privacy_tier=PrivacyTier.PUBLIC),
        lambda proposal: replace(proposal, proposed_intent=ReviewIntent.IDEA),
        lambda proposal: replace(proposal, proposal_reason_digest_sha256=_DIGEST_A),
        lambda proposal: replace(proposal, capture_why_digest_sha256=_DIGEST_A),
        lambda proposal: replace(proposal, state=ReviewProposalState.DEFERRED),
        lambda proposal: replace(
            proposal,
            created_at=_NOW + timedelta(minutes=1),
        ),
        lambda proposal: replace(proposal, actor_kind=ReviewActorKind.SYSTEM),
        lambda proposal: replace(proposal, actor_label_digest_sha256=_DIGEST_B),
    ),
)
def test_review_binding_privacy_intent_source_and_actor_dimensions_are_compared(
    change: Callable[[ReviewProposal], ReviewProposal],
) -> None:
    metadata = _metadata()
    reviews = metadata[ParityFacet.REVIEW_PROPOSALS]
    assert isinstance(reviews, ReviewProposalsMetadata)
    first = reviews.proposals[0]
    metadata[ParityFacet.REVIEW_PROPOSALS] = ReviewProposalsMetadata(
        (change(first), *reviews.proposals[1:])
    )

    result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN, metadata=metadata),
    )

    assert (
        result.for_facet(ParityFacet.REVIEW_PROPOSALS).outcome
        is ComparisonOutcome.BLOCKED_DIFFERENCE
    )


def test_cli_field_digest_and_doctor_probe_identity_are_compared() -> None:
    metadata = _metadata()
    cli = metadata[ParityFacet.CLI_JSON]
    assert isinstance(cli, CliJsonMetadata)
    metadata[ParityFacet.CLI_JSON] = replace(
        cli,
        field_digests=tuple(
            (field, _DIGEST_C if field == "metrics" else _DIGEST_A)
            for field in ("command", "metrics", "schema_version", "status", "strict")
        ),
    )
    cli_result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN, metadata=metadata),
    )
    assert (
        cli_result.for_facet(ParityFacet.CLI_JSON).outcome is ComparisonOutcome.BLOCKED_DIFFERENCE
    )

    repeated = HealthDoctorMetadata(
        outcome=HealthOutcome.UNAVAILABLE,
        findings=(
            HealthFinding(
                probe=DoctorProbe.CONFIGURATION,
                finding_class=HealthFindingClass.PROBE_FAILURE,
                state=DoctorProbeState.UNAVAILABLE,
            ),
            HealthFinding(
                probe=DoctorProbe.SCHEMA,
                finding_class=HealthFindingClass.PROBE_FAILURE,
                state=DoctorProbeState.UNAVAILABLE,
            ),
        ),
    )
    metadata = _metadata()
    metadata[ParityFacet.HEALTH_DOCTOR] = repeated
    doctor_result = _compare(
        _input(ParitySide.LEGACY),
        _input(ParitySide.OPEN_BRAIN, metadata=metadata),
    )
    assert (
        doctor_result.for_facet(ParityFacet.HEALTH_DOCTOR).outcome
        is ComparisonOutcome.BLOCKED_DIFFERENCE
    )
