from __future__ import annotations

import plistlib
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.doctor import (
    DoctorRole,
    FindingClass,
    ProbeName,
    ProbeReading,
    run_doctor,
)
from open_brain_legacy.operations.models import (
    DeploymentTarget,
    HostRole,
    OutputPolicy,
    WriterScope,
)
from open_brain_legacy.operations.render import render_launchd

OBSERVED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _healthy_probes() -> dict[ProbeName, Callable[[float], ProbeReading]]:
    return {
        name: lambda timeout: ProbeReading.healthy(observed_at=OBSERVED_AT)
        for name in ProbeName
    } | {
        ProbeName.WRITER_OWNERSHIP: lambda timeout: ProbeReading.healthy(
            target=DeploymentTarget.CANONICAL_WRITER,
            observed_at=OBSERVED_AT,
        )
    }


def test_job_001_is_an_hourly_laptop_probe_only_contract() -> None:
    job = get_job("JOB-001")
    manifest = plistlib.loads(render_launchd(job).encode("utf-8"))

    assert job.command == ("open-brain", "doctor", "--json", "--role=probe")
    assert job.host_role is HostRole.PROBE
    assert job.writer_scope is WriterScope.NONE
    assert job.lock_scope is LockScope.NONE
    assert job.output_policy is OutputPolicy.METADATA_ONLY
    assert manifest["StartInterval"] == 3600
    assert manifest["RunAtLoad"] is False

    result = run_doctor(
        role=DoctorRole.PROBE,
        probes=_healthy_probes(),
        timeout_seconds=1.0,
        strict=True,
    )
    assert result.exit_code == 0
    assert result.cutover_ready is False


@pytest.mark.parametrize(
    ("probe", "expected_finding"),
    [
        (ProbeName.CONFIGURATION, FindingClass.CONFIGURATION_INVALID),
        (ProbeName.QUEUE_AGE, FindingClass.QUEUE_STALE),
        (ProbeName.SCHEMA, FindingClass.SCHEMA_MISMATCH),
        (ProbeName.WRITER_OWNERSHIP, FindingClass.WRITER_OWNERSHIP_CONFLICT),
        (ProbeName.LOCK_STATE, FindingClass.LOCK_UNHEALTHY),
        (ProbeName.BACKUP_EVIDENCE, FindingClass.BACKUP_EVIDENCE_MISSING),
        (ProbeName.STALE_REFERENCES, FindingClass.STALE_REFERENCE),
        (ProbeName.OPTIONAL_PROVIDER, FindingClass.OPTIONAL_PROVIDER_UNREADY),
    ],
)
def test_job_001_finding_class_is_stable_for_each_synthetic_probe(
    probe: ProbeName,
    expected_finding: FindingClass,
) -> None:
    probes = _healthy_probes()
    probes[probe] = lambda timeout: ProbeReading.unhealthy(
        count=1,
        observed_at=OBSERVED_AT,
    )

    result = run_doctor(
        role=DoctorRole.PROBE,
        probes=probes,
        timeout_seconds=1.0,
        strict=True,
    )

    assert result.exit_code == 1
    assert len(result.findings) == 1
    assert result.findings[0].finding_class is expected_finding
