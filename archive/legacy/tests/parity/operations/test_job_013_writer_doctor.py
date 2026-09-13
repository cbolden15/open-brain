from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.doctor import (
    DoctorOutcome,
    DoctorRole,
    FindingClass,
    HistoricalDiagnosis,
    ProbeName,
    ProbeReading,
    run_doctor,
)
from open_brain_legacy.operations.models import DeploymentTarget, HostRole, WriterScope

OBSERVED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _healthy_writer_probes() -> dict[ProbeName, Callable[[float], ProbeReading]]:
    return {
        ProbeName.CONFIGURATION: lambda timeout: ProbeReading.healthy(
            observed_at=OBSERVED_AT
        ),
        ProbeName.QUEUE_AGE: lambda timeout: ProbeReading.healthy(
            age_seconds=30, count=0, observed_at=OBSERVED_AT
        ),
        ProbeName.SCHEMA: lambda timeout: ProbeReading.healthy(observed_at=OBSERVED_AT),
        ProbeName.WRITER_OWNERSHIP: lambda timeout: ProbeReading.healthy(
            target=DeploymentTarget.CANONICAL_WRITER,
            count=1,
            observed_at=OBSERVED_AT,
        ),
        ProbeName.LOCK_STATE: lambda timeout: ProbeReading.healthy(
            count=0, observed_at=OBSERVED_AT
        ),
        ProbeName.BACKUP_EVIDENCE: lambda timeout: ProbeReading.healthy(
            age_seconds=120, count=1, observed_at=OBSERVED_AT
        ),
        ProbeName.STALE_REFERENCES: lambda timeout: ProbeReading.healthy(
            count=0, observed_at=OBSERVED_AT
        ),
        ProbeName.OPTIONAL_PROVIDER: lambda timeout: ProbeReading.healthy(
            count=1, observed_at=OBSERVED_AT
        ),
    }


def test_job_013_checks_the_canonical_writer_without_owning_writes() -> None:
    job = get_job("JOB-013")
    result = run_doctor(
        role=DoctorRole.WRITER,
        probes=_healthy_writer_probes(),
        timeout_seconds=1.0,
        strict=True,
    )

    assert job.command == ("open-brain", "doctor", "--json", "--role=writer")
    assert job.host_role is HostRole.PROBE
    assert job.writer_scope is WriterScope.NONE
    assert result.outcome is DoctorOutcome.HEALTHY
    assert result.exit_code == 0
    assert result.cutover_ready is True


def test_job_013_writer_cutover_findings_are_deterministic_and_strict() -> None:
    probes = _healthy_writer_probes()
    probes.update(
        {
            ProbeName.WRITER_OWNERSHIP: lambda timeout: ProbeReading.unhealthy(
                target=DeploymentTarget.EDGE_OPERATOR,
                count=2,
                observed_at=OBSERVED_AT,
            ),
            ProbeName.LOCK_STATE: lambda timeout: ProbeReading.unhealthy(
                count=1, age_seconds=600, observed_at=OBSERVED_AT
            ),
            ProbeName.BACKUP_EVIDENCE: lambda timeout: ProbeReading.unhealthy(
                count=0, observed_at=OBSERVED_AT
            ),
            ProbeName.STALE_REFERENCES: lambda timeout: ProbeReading.unhealthy(
                count=3, observed_at=OBSERVED_AT
            ),
        }
    )

    result = run_doctor(
        role=DoctorRole.WRITER,
        probes=dict(reversed(tuple(probes.items()))),
        timeout_seconds=1.0,
        strict=True,
    )

    assert result.outcome is DoctorOutcome.UNHEALTHY
    assert result.exit_code == 1
    assert result.cutover_ready is False
    assert [finding.finding_class for finding in result.findings] == [
        FindingClass.WRITER_OWNERSHIP_CONFLICT,
        FindingClass.LOCK_UNHEALTHY,
        FindingClass.BACKUP_EVIDENCE_MISSING,
        FindingClass.STALE_REFERENCE,
    ]


def test_job_013_historical_nonzero_is_a_diagnosis_not_a_health_finding() -> None:
    result = run_doctor(
        role=DoctorRole.WRITER,
        probes=_healthy_writer_probes(),
        timeout_seconds=1.0,
        strict=True,
        historical_diagnoses=(
            HistoricalDiagnosis(job_id="JOB-013", observed_at=OBSERVED_AT),
        ),
    )

    assert result.outcome is DoctorOutcome.DIAGNOSIS_REQUIRED
    assert result.exit_code == 1
    assert result.cutover_ready is False
    assert result.findings == ()
    assert result.to_dict()["historical_diagnoses"] == [
        {
            "job_id": "JOB-013",
            "finding_class": "historical-nonzero",
            "state": "diagnosis-required",
            "observed_at": "2026-08-14T12:00:00Z",
        }
    ]

