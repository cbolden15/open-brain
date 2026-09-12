from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.doctor import DoctorCommandAdapter, show_doctor
from open_brain_legacy.cli.main import main
from open_brain_legacy.operations.doctor import DoctorRole, ProbeName, ProbeReading, run_doctor
from open_brain_legacy.operations.models import DeploymentTarget


def _healthy_probes() -> dict[ProbeName, Callable[[float], ProbeReading]]:
    return {
        ProbeName.CONFIGURATION: lambda timeout: ProbeReading.healthy(),
        ProbeName.QUEUE_AGE: lambda timeout: ProbeReading.healthy(age_seconds=30, count=2),
        ProbeName.SCHEMA: lambda timeout: ProbeReading.healthy(),
        ProbeName.WRITER_OWNERSHIP: lambda timeout: ProbeReading.healthy(
            count=1, target=DeploymentTarget.CANONICAL_WRITER
        ),
        ProbeName.LOCK_STATE: lambda timeout: ProbeReading.healthy(count=0),
        ProbeName.BACKUP_EVIDENCE: lambda timeout: ProbeReading.healthy(
            age_seconds=120, count=1
        ),
        ProbeName.STALE_REFERENCES: lambda timeout: ProbeReading.healthy(count=0),
        ProbeName.OPTIONAL_PROVIDER: lambda timeout: ProbeReading.healthy(count=1),
    }


def test_doctor_preserves_strict_exit_findings_and_cutover_evidence() -> None:
    healthy = run_doctor(
        role=DoctorRole.WRITER,
        probes=_healthy_probes(),
        timeout_seconds=1.0,
        strict=True,
    )
    unhealthy_probes = _healthy_probes()
    unhealthy_probes[ProbeName.CONFIGURATION] = lambda timeout: ProbeReading.unhealthy(count=1)
    unhealthy = run_doctor(
        role=DoctorRole.WRITER,
        probes=unhealthy_probes,
        timeout_seconds=1.0,
        strict=True,
    )

    healthy_result = show_doctor(result=healthy)
    unhealthy_result = show_doctor(result=unhealthy)

    assert healthy_result.exit_code == 0
    assert healthy_result.envelope["cutover_ready"] is True
    assert unhealthy_result.exit_code == 1
    assert unhealthy_result.envelope["status"] == "unhealthy"
    assert unhealthy_result.envelope["cutover_ready"] is False
    assert unhealthy_result.envelope["findings"] == unhealthy.to_dict()["findings"]
    assert '"path"' not in unhealthy_result.to_json()
    assert '"content"' not in unhealthy_result.to_json()


def test_plain_doctor_adapter_omits_reserved_readiness_claim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = DoctorCommandAdapter(probes=_healthy_probes())

    exit_code = main(
        ("doctor", "--json", "--role=writer"),
        command_adapters=CommandAdapterRegistry({"doctor": adapter}),
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code is ExitCode.SUCCESS
    assert output["command"] == "doctor"
    assert output["status"] == "healthy"
    assert output["role"] == "writer"
    assert output["strict"] is True
    assert "cutover_ready" not in output


def test_plain_doctor_adapter_rejects_pre_alpha_cutover_and_other_shapes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = DoctorCommandAdapter(probes=_healthy_probes())
    registry = CommandAdapterRegistry({"doctor": adapter})

    cutover_exit = main(
        ("doctor", "--cutover", "--json"),
        command_adapters=registry,
    )
    cutover_output = json.loads(capsys.readouterr().out)
    invalid_exit = main(
        ("doctor", "--json", "--role", "writer"),
        command_adapters=registry,
    )
    invalid_output = json.loads(capsys.readouterr().out)

    assert cutover_exit is ExitCode.USAGE
    assert cutover_output["status"] == "invalid"
    assert invalid_exit is ExitCode.USAGE
    assert invalid_output["status"] == "invalid"
