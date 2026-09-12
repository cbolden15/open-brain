from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from open_brain_legacy.release.day_zero import (
    DAY_ZERO_CHECKS,
    EXPECTED_JOB_IDS,
    EXPECTED_WRITER_SURFACES,
    DayZeroBaseline,
    DayZeroCheck,
    DayZeroCheckName,
    DayZeroEvidenceError,
    JobBindingEvidence,
    JobBindingState,
    RecoveryPointEvidence,
    RecoveryRepository,
    WriterOwnershipEvidence,
    validate_day_zero_baseline,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_SOURCE = "1" * 40
_AGENT_CONFIG = "2" * 40
_NOW = datetime(2026, 8, 25, 18, 30, tzinfo=UTC)

_TERMINAL_MUTATIONS: tuple[
    tuple[Callable[[DayZeroBaseline], DayZeroBaseline], str], ...
] = (
    (lambda value: replace(value, installed_wheel_digest_sha256=_D), "installed-wheel-mismatch"),
    (lambda value: replace(value, predecessor_active_count=1), "predecessor-service-active"),
    (lambda value: replace(value, failed_service_count=1), "day-zero-service-inventory-unhealthy"),
    (lambda value: replace(value, duplicate_owner_count=1), "day-zero-service-inventory-unhealthy"),
    (lambda value: replace(value, missing_owner_count=1), "day-zero-service-inventory-unhealthy"),
    (lambda value: replace(value, undrained_queue_count=1), "day-zero-runtime-state-unhealthy"),
    (lambda value: replace(value, stale_lease_count=1), "day-zero-runtime-state-unhealthy"),
    (lambda value: replace(value, rollback_available=False), "day-zero-rollback-unavailable"),
    (lambda value: replace(value, rollback_activated=True), "day-zero-rollback-activated"),
)


def _baseline() -> DayZeroBaseline:
    return DayZeroBaseline(
        schema_version=1,
        source_commit_sha=_SOURCE,
        agent_config_commit_sha=_AGENT_CONFIG,
        source_artifact_digest_sha256=_A,
        sdist_digest_sha256=_B,
        wheel_digest_sha256=_C,
        installed_wheel_digest_sha256=_C,
        open_brain_config_digest_sha256=_D,
        agent_config_digest_sha256=_A,
        service_inventory_digest_sha256=_B,
        helper_digest_sha256=_C,
        job_bindings=tuple(
            JobBindingEvidence(
                job_id=job_id,
                state=(
                    JobBindingState.MANUAL_READY
                    if job_id in {"JOB-006", "JOB-009", "JOB-024"}
                    else JobBindingState.HEALTHY
                ),
                evidence_digest_sha256=_D,
            )
            for job_id in EXPECTED_JOB_IDS
        ),
        predecessor_active_count=0,
        predecessor_loaded_count=0,
        failed_service_count=0,
        duplicate_owner_count=0,
        missing_owner_count=0,
        undrained_queue_count=0,
        stale_lease_count=0,
        writer_generation=2,
        writers=tuple(
            WriterOwnershipEvidence(
                surface=surface,
                owner_count=1,
                evidence_digest_sha256=_A,
            )
            for surface in EXPECTED_WRITER_SURFACES
        ),
        recovery_points=tuple(
            RecoveryPointEvidence(
                repository=repository,
                snapshot_identity_sha256=_B if repository is RecoveryRepository.PRIMARY else _C,
                restore_receipt_digest_sha256=_D,
                independently_verified=True,
            )
            for repository in RecoveryRepository
        ),
        checks=tuple(
            DayZeroCheck(
                name=name,
                observed_at=_NOW,
                evidence_digest_sha256=_D,
            )
            for name in DAY_ZERO_CHECKS
        ),
        rollback_available=True,
        rollback_activated=False,
        stabilization_started_at=_NOW,
    )


def test_day_zero_binds_exact_direct_inventory_without_claiming_later_days() -> None:
    baseline = _baseline()

    validate_day_zero_baseline(baseline)

    assert len(baseline.job_bindings) == 30
    assert len(baseline.checks) == len(DayZeroCheckName)
    assert baseline.digest_sha256 == baseline.digest_sha256
    assert not hasattr(baseline, "days")


@pytest.mark.parametrize(
    ("mutate", "error"),
    _TERMINAL_MUTATIONS,
)
def test_day_zero_rejects_each_unhealthy_terminal_condition(
    mutate: Callable[[DayZeroBaseline], DayZeroBaseline], error: str
) -> None:
    with pytest.raises(DayZeroEvidenceError, match=error):
        validate_day_zero_baseline(mutate(_baseline()))


def test_day_zero_rejects_missing_job_writer_recovery_or_direct_check() -> None:
    baseline = _baseline()

    mutations: tuple[tuple[Callable[[DayZeroBaseline], DayZeroBaseline], str], ...] = (
        (
            lambda value: replace(value, job_bindings=value.job_bindings[:-1]),
            "day-zero-job-inventory-mismatch",
        ),
        (
            lambda value: replace(value, writers=value.writers[:-1]),
            "day-zero-writer-inventory-mismatch",
        ),
        (
            lambda value: replace(value, recovery_points=value.recovery_points[:-1]),
            "day-zero-recovery-inventory-mismatch",
        ),
        (
            lambda value: replace(value, checks=value.checks[:-1]),
            "day-zero-check-inventory-mismatch",
        ),
    )
    for mutate, error in mutations:
        with pytest.raises(DayZeroEvidenceError, match=error):
            validate_day_zero_baseline(mutate(baseline))


def test_day_zero_rejects_non_direct_or_post_start_evidence() -> None:
    baseline = _baseline()
    later = replace(baseline.checks[0], observed_at=_NOW.replace(minute=31))
    unverified = replace(baseline.recovery_points[0], independently_verified=False)

    with pytest.raises(DayZeroEvidenceError, match="day-zero-check-after-start"):
        validate_day_zero_baseline(
            replace(baseline, checks=(later, *baseline.checks[1:]))
        )
    with pytest.raises(DayZeroEvidenceError, match="day-zero-recovery-not-verified"):
        validate_day_zero_baseline(
            replace(baseline, recovery_points=(unverified, *baseline.recovery_points[1:]))
        )
