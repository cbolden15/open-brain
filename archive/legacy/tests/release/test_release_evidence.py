from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from open_brain_legacy.release.evidence import (
    EXPECTED_CAPABILITY_IDS,
    REQUIRED_DAY_CHECKS,
    ArtifactEvidence,
    ArtifactKind,
    CapabilityDisposition,
    CapabilityManifest,
    CapabilityRow,
    DailyEvidence,
    DayCheck,
    EvidenceValidationError,
    IndependentReviewEvidence,
    Predecessor,
    PredecessorEvidence,
    ProductionEvidence,
    ProductionState,
    RehearsalEvidence,
    ReplacementEvidence,
    ReproducibilityEvidence,
    RuntimeIdentity,
    RuntimeIdentityEvidence,
    RuntimeReferenceEvidence,
    StabilizationEvidence,
    validate_production_evidence,
)
from open_brain_legacy.release.installation import (
    InstallationPlan,
    InstallationPlanError,
    InstallationPlatform,
    generic_installation_plan,
    validate_installation_plan,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_COMMIT = "e" * 40
_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _artifact(kind: ArtifactKind, digest: str) -> ArtifactEvidence:
    stem = f"open-brain-{_COMMIT}" if kind is ArtifactKind.SOURCE else "open-brain-0.1.0"
    return ArtifactEvidence(
        kind=kind,
        filename=f"{stem}.{kind.extension}",
        digest_sha256=digest,
    )


def _capability_manifest() -> CapabilityManifest:
    return CapabilityManifest(
        version=2,
        rows=tuple(
            CapabilityRow(
                id=row_id,
                disposition=CapabilityDisposition.OPEN_BRAIN_LIVE,
                implementation_digest_sha256=_A,
                focused_test_digest_sha256=_B,
                parity_evidence_digest_sha256=_C,
                production_binding_digest_sha256=_D,
            )
            for row_id in EXPECTED_CAPABILITY_IDS
        ),
    )


def _day(offset: int) -> DailyEvidence:
    return DailyEvidence(
        observed_on=date(2026, 8, 19) + timedelta(days=offset),
        checks=tuple(
            DayCheck(check=check, state=ProductionState.PASSED_DIRECT)
            for check in REQUIRED_DAY_CHECKS
        ),
        evidence_digest_sha256=_A,
    )


def _production_evidence() -> ProductionEvidence:
    manifest = _capability_manifest()
    artifacts = (
        _artifact(ArtifactKind.SOURCE, _D),
        _artifact(ArtifactKind.SDIST, _A),
        _artifact(ArtifactKind.WHEEL, _B),
    )
    return ProductionEvidence(
        capability_manifest=manifest,
        manifest_digest_sha256=manifest.digest_sha256,
        source_commit_sha=_COMMIT,
        artifacts=artifacts,
        clean_install_artifact_digest_sha256=_B,
        independent_review=IndependentReviewEvidence(
            review_id="review_0123456789abcdef",
            reviewed_commit_sha=_COMMIT,
            evidence_digest_sha256=_C,
        ),
        replacement=ReplacementEvidence(
            predecessors=tuple(
                PredecessorEvidence(
                    predecessor=predecessor,
                    manifest_digest_sha256=manifest.digest_sha256,
                    runtime_reference=RuntimeReferenceEvidence(
                        scan_id=f"scan-{predecessor.name.lower()}_0123456789abcdef",
                        evidence_digest_sha256=_D,
                        state=ProductionState.PASSED_DIRECT,
                    ),
                    rehearsal=RehearsalEvidence(
                        rehearsal_id=f"rehearsal-{predecessor.name.lower()}_0123456789abcdef",
                        artifact_digest_sha256=_B,
                        evidence_digest_sha256=_A,
                        state=ProductionState.PASSED_DIRECT,
                    ),
                )
                for predecessor in Predecessor
            ),
        ),
        stabilization=StabilizationEvidence(days=tuple(_day(offset) for offset in range(7))),
        reproducibility=ReproducibilityEvidence(
            source_commit_sha=_COMMIT,
            rebuilt_artifacts=artifacts,
            evidence_digest_sha256=_C,
        ),
        runtime_identities=tuple(
            RuntimeIdentityEvidence(
                identity=identity,
                evidence_id=f"identity-{identity.value}_0123456789abcdef",
                artifact_digest_sha256=_B,
                evidence_digest_sha256=_A,
                state=ProductionState.PASSED_DIRECT,
            )
            for identity in RuntimeIdentity
        ),
    )


def test_release_evidence_is_hash_bound_complete_and_never_installs() -> None:
    manifest = _capability_manifest()
    assert len(manifest.rows) == 83
    assert len({row.id for row in manifest.rows}) == 83
    assert {row.disposition.value for row in manifest.rows} == {"open-brain-live"}

    validate_production_evidence(_production_evidence())

    for platform in InstallationPlatform:
        job_id = "JOB-001" if platform is InstallationPlatform.LAUNCHD else "JOB-028"
        plan = generic_installation_plan(platform=platform, job_id=job_id)
        assert isinstance(plan, InstallationPlan)
        validate_installation_plan(plan)

    forged = replace(
        generic_installation_plan(platform=InstallationPlatform.LAUNCHD, job_id="JOB-001"),
        manifest_name="other.plist",
    )
    with pytest.raises(InstallationPlanError):
        validate_installation_plan(forged)

    evidence = _production_evidence()
    invalid = replace(evidence, manifest_digest_sha256=_D)
    with pytest.raises(EvidenceValidationError):
        validate_production_evidence(invalid)

    missing_binding = replace(
        manifest,
        rows=(replace(manifest.rows[0], production_binding_digest_sha256=""), *manifest.rows[1:]),
    )
    with pytest.raises(EvidenceValidationError, match="invalid-capability-production-binding"):
        validate_production_evidence(
            replace(evidence, capability_manifest=missing_binding)
        )
