"""Fail-closed, metadata-only production release evidence contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import NoReturn


class EvidenceValidationError(ValueError):
    """Release evidence is incomplete, contradictory, or unbound."""


class CapabilityDisposition(StrEnum):
    """Closed final disposition for one predecessor capability."""

    OPEN_BRAIN_LIVE = "open-brain-live"


class Predecessor(StrEnum):
    """Opaque identifiers for the two replacement predecessors."""

    ALPHA = "predecessor-alpha"
    BETA = "predecessor-beta"


class ArtifactKind(StrEnum):
    """Closed artifact identities required for a public release."""

    SOURCE = "source"
    SDIST = "sdist"
    WHEEL = "wheel"

    @property
    def extension(self) -> str:
        return {
            ArtifactKind.SOURCE: "json",
            ArtifactKind.SDIST: "tar.gz",
            ArtifactKind.WHEEL: "whl",
        }[self]


class ProductionState(StrEnum):
    """Only direct, successful evidence may satisfy a production requirement."""

    PASSED_DIRECT = "passed-direct"


class RuntimeIdentity(StrEnum):
    """Required direct identities for a production capability handoff."""

    RECOVERY = "recovery"
    REHEARSAL = "rehearsal"
    SERVICE = "service"
    WRITER = "writer"
    HELPER = "helper"
    CONFIG = "config"


class DayCheckName(StrEnum):
    """Daily production checks required during stabilization."""

    HEALTH = "health"
    QUEUE = "queue"
    REVIEW = "review"
    LEDGER = "ledger"
    BACKUP = "backup"
    REDACTION = "redaction"
    NIGHTLY_CYCLE = "nightly-cycle"
    CAPTURE_INTEGRITY = "capture-integrity"


class StabilizationResetReason(StrEnum):
    """Events that invalidate all earlier stabilization days."""

    ROLLBACK = "rollback"
    INTEGRITY_FIX = "integrity-fix"


_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_OPAQUE_ID = re.compile(r"[a-z][a-z0-9-]{1,31}_[a-z0-9]{16,64}")
_FILENAME = re.compile(r"open-brain-[a-z0-9][a-z0-9.-]{0,127}")
_CAPABILITY_ALLOCATIONS = (
    ("CLI", 15),
    ("LED", 9),
    ("INT", 14),
    ("CAP", 11),
    ("JOB", 30),
    ("HOOK", 2),
    ("EXT", 2),
)
EXPECTED_CAPABILITY_IDS = tuple(
    f"{prefix}-{index:03d}"
    for prefix, count in _CAPABILITY_ALLOCATIONS
    for index in range(1, count + 1)
)


@dataclass(frozen=True, slots=True)
class CapabilityRow:
    """One final, non-deferred predecessor capability disposition."""

    id: str
    disposition: CapabilityDisposition
    implementation_digest_sha256: str
    focused_test_digest_sha256: str
    parity_evidence_digest_sha256: str
    production_binding_digest_sha256: str


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    """The closed 83-row release capability inventory."""

    version: int
    rows: tuple[CapabilityRow, ...]

    @property
    def digest_sha256(self) -> str:
        validate_capability_manifest(self)
        return _digest(
            {
                "version": self.version,
                "rows": [
                    {
                        "id": row.id,
                        "disposition": row.disposition.value,
                        "implementation_digest_sha256": row.implementation_digest_sha256,
                        "focused_test_digest_sha256": row.focused_test_digest_sha256,
                        "parity_evidence_digest_sha256": row.parity_evidence_digest_sha256,
                        "production_binding_digest_sha256": (
                            row.production_binding_digest_sha256
                        ),
                    }
                    for row in self.rows
                ],
            }
        )


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    """One source, sdist, or wheel artifact identity."""

    kind: ArtifactKind
    filename: str
    digest_sha256: str


@dataclass(frozen=True, slots=True)
class IndependentReviewEvidence:
    """Independent review bound to the release source commit."""

    review_id: str
    reviewed_commit_sha: str
    evidence_digest_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeReferenceEvidence:
    """Direct, redacted runtime-reference scan evidence for one predecessor."""

    scan_id: str
    evidence_digest_sha256: str
    state: ProductionState


@dataclass(frozen=True, slots=True)
class RehearsalEvidence:
    """Copy-only rehearsal and restore evidence bound to the wheel artifact."""

    rehearsal_id: str
    artifact_digest_sha256: str
    evidence_digest_sha256: str
    state: ProductionState


@dataclass(frozen=True, slots=True)
class PredecessorEvidence:
    """Replacement evidence for exactly one predecessor."""

    predecessor: Predecessor
    manifest_digest_sha256: str
    runtime_reference: RuntimeReferenceEvidence
    rehearsal: RehearsalEvidence


@dataclass(frozen=True, slots=True)
class ReplacementEvidence:
    """Replacement coverage that requires both predecessors."""

    predecessors: tuple[PredecessorEvidence, ...]


@dataclass(frozen=True, slots=True)
class RuntimeIdentityEvidence:
    """A direct identity check for one runtime role."""

    identity: RuntimeIdentity
    evidence_id: str
    artifact_digest_sha256: str
    evidence_digest_sha256: str
    state: ProductionState


@dataclass(frozen=True, slots=True)
class DayCheck:
    """A direct, day-scoped production health check."""

    check: DayCheckName
    state: ProductionState


@dataclass(frozen=True, slots=True)
class DailyEvidence:
    """One fully evidenced stabilization day."""

    observed_on: date
    checks: tuple[DayCheck, ...]
    evidence_digest_sha256: str


@dataclass(frozen=True, slots=True)
class StabilizationReset:
    """A rollback or integrity repair that restarts the stabilization clock."""

    occurred_on: date
    reason: StabilizationResetReason
    evidence_digest_sha256: str


@dataclass(frozen=True, slots=True)
class StabilizationEvidence:
    """Seven consecutive direct-evidence days after the latest reset."""

    days: tuple[DailyEvidence, ...]
    resets: tuple[StabilizationReset, ...] = ()


@dataclass(frozen=True, slots=True)
class ReproducibilityEvidence:
    """A second build whose source/sdist/wheel identities match the first build."""

    source_commit_sha: str
    rebuilt_artifacts: tuple[ArtifactEvidence, ...]
    evidence_digest_sha256: str


@dataclass(frozen=True, slots=True)
class ProductionEvidence:
    """All direct evidence required to close the release gate."""

    capability_manifest: CapabilityManifest
    manifest_digest_sha256: str
    source_commit_sha: str
    artifacts: tuple[ArtifactEvidence, ...]
    clean_install_artifact_digest_sha256: str
    independent_review: IndependentReviewEvidence
    replacement: ReplacementEvidence
    stabilization: StabilizationEvidence
    reproducibility: ReproducibilityEvidence | None = None
    runtime_identities: tuple[RuntimeIdentityEvidence, ...] = ()


REQUIRED_DAY_CHECKS = tuple(DayCheckName)


def validate_capability_manifest(value: CapabilityManifest) -> None:
    """Require all 83 rows live with four hash-bound acceptance identities."""
    _require_exact_type(value, CapabilityManifest, "invalid-capability-manifest")
    if value.version != 2 or not isinstance(value.rows, tuple):
        _fail("invalid-capability-manifest")
    if tuple(row.id for row in value.rows) != EXPECTED_CAPABILITY_IDS:
        _fail("capability-manifest-allocation-mismatch")
    if len(value.rows) != 83 or len({row.id for row in value.rows}) != 83:
        _fail("capability-manifest-not-unique")
    for row in value.rows:
        _require_exact_type(row, CapabilityRow, "invalid-capability-row")
        if not isinstance(row.id, str):
            _fail("invalid-capability-row")
        _require_enum(row.disposition, CapabilityDisposition, "invalid-capability-disposition")
        if row.disposition is not CapabilityDisposition.OPEN_BRAIN_LIVE:
            _fail("capability-not-open-brain-live")
        _require_digest(
            row.implementation_digest_sha256,
            "invalid-capability-implementation",
        )
        _require_digest(row.focused_test_digest_sha256, "invalid-capability-focused-test")
        _require_digest(
            row.parity_evidence_digest_sha256,
            "invalid-capability-parity-evidence",
        )
        _require_digest(
            row.production_binding_digest_sha256,
            "invalid-capability-production-binding",
        )


def validate_production_evidence(value: ProductionEvidence) -> None:
    """Validate all release evidence without performing any release action."""
    _require_exact_type(value, ProductionEvidence, "invalid-production-evidence")
    validate_capability_manifest(value.capability_manifest)
    _require_digest(value.manifest_digest_sha256, "invalid-manifest-digest")
    if value.manifest_digest_sha256 != value.capability_manifest.digest_sha256:
        _fail("production-evidence-manifest-mismatch")
    _require_commit(value.source_commit_sha, "invalid-source-commit")
    artifacts = _validate_artifacts(value.artifacts, value.source_commit_sha)
    wheel_digest = artifacts[ArtifactKind.WHEEL].digest_sha256
    if value.clean_install_artifact_digest_sha256 != wheel_digest:
        _fail("clean-install-wheel-mismatch")
    _require_digest(value.clean_install_artifact_digest_sha256, "invalid-clean-install-digest")
    _validate_independent_review(value.independent_review, value.source_commit_sha)
    _validate_replacement(value.replacement, value.manifest_digest_sha256, wheel_digest)
    _validate_reproducibility(value.reproducibility, value.source_commit_sha, value.artifacts)
    _validate_runtime_identities(value.runtime_identities, wheel_digest)
    _validate_stabilization(value.stabilization)


def validate_replacement_evidence(
    value: ReplacementEvidence,
    *,
    capability_manifest: CapabilityManifest,
    wheel_digest_sha256: str,
) -> None:
    """Validate both predecessor records against one manifest and wheel identity."""
    validate_capability_manifest(capability_manifest)
    manifest_digest_sha256 = capability_manifest.digest_sha256
    _require_digest(wheel_digest_sha256, "invalid-wheel-digest")
    _validate_replacement(value, manifest_digest_sha256, wheel_digest_sha256)


def validate_stabilization_evidence(value: StabilizationEvidence) -> None:
    """Validate a seven-day stabilization window without performing any checks."""
    _validate_stabilization(value)


def _validate_artifacts(
    artifacts: tuple[ArtifactEvidence, ...], source_commit_sha: str
) -> dict[ArtifactKind, ArtifactEvidence]:
    if not isinstance(artifacts, tuple) or len(artifacts) != len(ArtifactKind):
        _fail("artifact-identity-count-mismatch")
    by_kind: dict[ArtifactKind, ArtifactEvidence] = {}
    for artifact in artifacts:
        _require_exact_type(artifact, ArtifactEvidence, "invalid-artifact-identity")
        _require_enum(artifact.kind, ArtifactKind, "invalid-artifact-kind")
        if artifact.kind in by_kind:
            _fail("duplicate-artifact-kind")
        if not isinstance(artifact.filename, str) or "/" in artifact.filename:
            _fail("invalid-artifact-filename")
        filename_stem = artifact.filename.removesuffix(f".{artifact.kind.extension}")
        if _FILENAME.fullmatch(filename_stem) is None:
            _fail("invalid-artifact-filename")
        if not artifact.filename.endswith(f".{artifact.kind.extension}"):
            _fail("artifact-extension-mismatch")
        _require_digest(artifact.digest_sha256, "invalid-artifact-digest")
        by_kind[artifact.kind] = artifact
    if set(by_kind) != set(ArtifactKind):
        _fail("artifact-identity-allocation-mismatch")
    if source_commit_sha not in by_kind[ArtifactKind.SOURCE].filename:
        _fail("source-artifact-commit-mismatch")
    return by_kind


def _validate_independent_review(
    value: IndependentReviewEvidence, source_commit_sha: str
) -> None:
    _require_exact_type(value, IndependentReviewEvidence, "invalid-independent-review")
    _require_opaque_id(value.review_id, "invalid-independent-review-id")
    _require_commit(value.reviewed_commit_sha, "invalid-independent-review-commit")
    _require_digest(value.evidence_digest_sha256, "invalid-independent-review-digest")
    if value.reviewed_commit_sha != source_commit_sha:
        _fail("independent-review-commit-mismatch")


def _validate_replacement(
    value: ReplacementEvidence, manifest_digest: str, wheel_digest: str
) -> None:
    _require_exact_type(value, ReplacementEvidence, "invalid-replacement-evidence")
    if not isinstance(value.predecessors, tuple) or len(value.predecessors) != len(Predecessor):
        _fail("predecessor-evidence-count-mismatch")
    seen: set[Predecessor] = set()
    for evidence in value.predecessors:
        _require_exact_type(evidence, PredecessorEvidence, "invalid-predecessor-evidence")
        _require_enum(evidence.predecessor, Predecessor, "invalid-predecessor")
        if evidence.predecessor in seen:
            _fail("duplicate-predecessor-evidence")
        seen.add(evidence.predecessor)
        _require_digest(evidence.manifest_digest_sha256, "invalid-predecessor-manifest-digest")
        if evidence.manifest_digest_sha256 != manifest_digest:
            _fail("predecessor-manifest-mismatch")
        _validate_runtime_reference(evidence.runtime_reference)
        _validate_rehearsal(evidence.rehearsal, wheel_digest)
    if seen != set(Predecessor):
        _fail("predecessor-evidence-allocation-mismatch")


def _validate_runtime_reference(value: RuntimeReferenceEvidence) -> None:
    _require_exact_type(value, RuntimeReferenceEvidence, "invalid-runtime-reference-evidence")
    _require_opaque_id(value.scan_id, "invalid-runtime-reference-id")
    _require_digest(value.evidence_digest_sha256, "invalid-runtime-reference-digest")
    _require_passed_direct(value.state, "runtime-reference-not-direct")


def _validate_rehearsal(value: RehearsalEvidence, wheel_digest: str) -> None:
    _require_exact_type(value, RehearsalEvidence, "invalid-rehearsal-evidence")
    _require_opaque_id(value.rehearsal_id, "invalid-rehearsal-id")
    _require_digest(value.artifact_digest_sha256, "invalid-rehearsal-artifact-digest")
    _require_digest(value.evidence_digest_sha256, "invalid-rehearsal-digest")
    _require_passed_direct(value.state, "rehearsal-not-direct")
    if value.artifact_digest_sha256 != wheel_digest:
        _fail("rehearsal-wheel-mismatch")


def _validate_reproducibility(
    value: ReproducibilityEvidence | None,
    source_commit_sha: str,
    artifacts: tuple[ArtifactEvidence, ...],
) -> None:
    if value is None:
        _fail("missing-reproducibility-evidence")
    _require_exact_type(value, ReproducibilityEvidence, "invalid-reproducibility-evidence")
    _require_commit(value.source_commit_sha, "invalid-reproducibility-commit")
    _require_digest(value.evidence_digest_sha256, "invalid-reproducibility-digest")
    if value.source_commit_sha != source_commit_sha or value.rebuilt_artifacts != artifacts:
        _fail("reproducibility-artifact-mismatch")
    _validate_artifacts(value.rebuilt_artifacts, source_commit_sha)


def _validate_runtime_identities(
    values: tuple[RuntimeIdentityEvidence, ...], wheel_digest: str
) -> None:
    if not isinstance(values, tuple) or len(values) != len(RuntimeIdentity):
        _fail("runtime-identity-count-mismatch")
    seen: set[RuntimeIdentity] = set()
    for value in values:
        _require_exact_type(value, RuntimeIdentityEvidence, "invalid-runtime-identity")
        _require_enum(value.identity, RuntimeIdentity, "invalid-runtime-identity-kind")
        if value.identity in seen:
            _fail("duplicate-runtime-identity")
        seen.add(value.identity)
        _require_opaque_id(value.evidence_id, "invalid-runtime-identity-id")
        _require_digest(value.artifact_digest_sha256, "invalid-runtime-identity-artifact")
        _require_digest(value.evidence_digest_sha256, "invalid-runtime-identity-digest")
        _require_passed_direct(value.state, "runtime-identity-not-direct")
        if value.artifact_digest_sha256 != wheel_digest:
            _fail("runtime-identity-wheel-mismatch")
    if seen != set(RuntimeIdentity):
        _fail("runtime-identity-allocation-mismatch")


def _validate_stabilization(value: StabilizationEvidence) -> None:
    _require_exact_type(value, StabilizationEvidence, "invalid-stabilization-evidence")
    if not isinstance(value.days, tuple) or len(value.days) != 7:
        _fail("stabilization-day-count-mismatch")
    if not isinstance(value.resets, tuple):
        _fail("invalid-stabilization-resets")
    latest_reset: date | None = None
    for reset in value.resets:
        _require_exact_type(reset, StabilizationReset, "invalid-stabilization-reset")
        if type(reset.occurred_on) is not date:
            _fail("invalid-stabilization-reset-date")
        _require_enum(reset.reason, StabilizationResetReason, "invalid-stabilization-reset-reason")
        _require_digest(reset.evidence_digest_sha256, "invalid-stabilization-reset-digest")
        latest_reset = max(latest_reset, reset.occurred_on) if latest_reset else reset.occurred_on
    previous: date | None = None
    for index, day in enumerate(value.days):
        _require_exact_type(day, DailyEvidence, "invalid-stabilization-day")
        if type(day.observed_on) is not date:
            _fail("invalid-stabilization-day-date")
        if previous is not None and day.observed_on != previous + timedelta(days=1):
            _fail("stabilization-days-not-consecutive")
        if latest_reset is not None and day.observed_on <= latest_reset:
            _fail("stabilization-clock-not-reset")
        _require_digest(day.evidence_digest_sha256, "invalid-stabilization-day-digest")
        _validate_day_checks(day.checks, index)
        previous = day.observed_on


def _validate_day_checks(values: tuple[DayCheck, ...], day_index: int) -> None:
    if not isinstance(values, tuple) or len(values) != len(DayCheckName):
        _fail("stabilization-check-count-mismatch")
    checks: set[DayCheckName] = set()
    for value in values:
        _require_exact_type(value, DayCheck, "invalid-stabilization-check")
        _require_enum(value.check, DayCheckName, "invalid-stabilization-check-name")
        if value.check in checks:
            _fail("duplicate-stabilization-check")
        checks.add(value.check)
        _require_passed_direct(value.state, "stabilization-check-not-direct")
    if checks != set(REQUIRED_DAY_CHECKS):
        _fail("stabilization-check-allocation-mismatch")
    if day_index == 0 and checks != set(REQUIRED_DAY_CHECKS):
        _fail("day-zero-checks-incomplete")


def _require_passed_direct(value: object, code: str) -> None:
    _require_enum(value, ProductionState, code)
    if value is not ProductionState.PASSED_DIRECT:
        _fail(code)


def _require_enum(value: object, enum_type: type[StrEnum], code: str) -> None:
    if type(value) is not enum_type:
        _fail(code)


def _require_exact_type(value: object, expected: type[object], code: str) -> None:
    if type(value) is not expected:
        _fail(code)


def _require_digest(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(code)


def _require_commit(value: object, code: str) -> None:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        _fail(code)


def _require_opaque_id(value: object, code: str) -> None:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        _fail(code)


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def _fail(code: str) -> NoReturn:
    raise EvidenceValidationError(code)
