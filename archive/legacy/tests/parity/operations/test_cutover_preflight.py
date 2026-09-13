import json
from collections.abc import Callable

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.main import main
from open_brain_legacy.operations.doctor import (
    DoctorOutcome,
    DoctorRole,
    ProbeName,
    ProbeReading,
    run_doctor,
)
from open_brain_legacy.operations.models import DeploymentTarget
from packages.legacy.tests.parity.cross_surface._preflight import (
    AUTHORITATIVE_ROW_CLASSIFICATIONS,
    PreflightEvidence,
    RowClassification,
    evaluate_cutover_preflight,
)


def test_cutover_preflight_refuses_synthetic_only_phase_five_evidence() -> None:
    evidence = PreflightEvidence(
        row_classifications=AUTHORITATIVE_ROW_CLASSIFICATIONS,
        p0_p2_findings=0,
        doctor_outcome=DoctorOutcome.HEALTHY,
        evidence_scope="synthetic",
    )

    result = evaluate_cutover_preflight(evidence)

    assert result.ready is False
    assert result.reasons == ("synthetic-evidence-only",)


def test_cutover_preflight_rejects_any_non_live_row() -> None:
    relabeled_rows = (
        *AUTHORITATIVE_ROW_CLASSIFICATIONS[:-1],
        RowClassification("JOB-030", "unimplemented"),  # type: ignore[arg-type]
    )
    evidence = PreflightEvidence(
        row_classifications=relabeled_rows,
        p0_p2_findings=0,
        doctor_outcome=DoctorOutcome.HEALTHY,
        evidence_scope="live",
    )

    result = evaluate_cutover_preflight(evidence)

    assert result.ready is False
    assert result.reasons == ("row-classifications-not-authoritative",)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"row_classifications": AUTHORITATIVE_ROW_CLASSIFICATIONS[:-1]},
            "row-classifications-not-authoritative",
        ),
        ({"p0_p2_findings": 1}, "p0-p2-findings-remain"),
        ({"doctor_outcome": DoctorOutcome.UNAVAILABLE}, "doctor-not-healthy"),
        ({"evidence_scope": "synthetic"}, "synthetic-evidence-only"),
    ],
)
def test_cutover_preflight_refuses_each_incomplete_evidence_class(
    changes: dict[str, object],
    reason: str,
) -> None:
    values: dict[str, object] = {
        "row_classifications": AUTHORITATIVE_ROW_CLASSIFICATIONS,
        "p0_p2_findings": 0,
        "doctor_outcome": DoctorOutcome.HEALTHY,
        "evidence_scope": "live",
    }
    values.update(changes)
    evidence = PreflightEvidence(**values)  # type: ignore[arg-type]

    result = evaluate_cutover_preflight(evidence)

    assert result.ready is False
    assert reason in result.reasons


def test_public_cutover_command_refuses_without_a_separately_injected_live_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["doctor", "--cutover", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "unavailable"
    assert "cutover_ready" not in output


def test_probe_role_cannot_become_cutover_ready_from_all_healthy_synthetic_probes() -> None:
    probes: dict[ProbeName, Callable[[float], ProbeReading]] = {
        probe: (lambda timeout: ProbeReading.healthy()) for probe in ProbeName
    }
    probes[ProbeName.WRITER_OWNERSHIP] = lambda timeout: ProbeReading.healthy(
        count=1,
        target=DeploymentTarget.CANONICAL_WRITER,
    )

    result = run_doctor(
        role=DoctorRole.PROBE,
        probes=probes,
        timeout_seconds=1.0,
        strict=True,
    )

    assert result.outcome is DoctorOutcome.HEALTHY
    assert result.exit_code == 0
    assert result.cutover_ready is False


def test_synthetic_writer_doctor_green_is_still_refused_by_cutover_preflight() -> None:
    probes: dict[ProbeName, Callable[[float], ProbeReading]] = {
        probe: (lambda timeout: ProbeReading.healthy()) for probe in ProbeName
    }
    probes[ProbeName.WRITER_OWNERSHIP] = lambda timeout: ProbeReading.healthy(
        count=1,
        target=DeploymentTarget.CANONICAL_WRITER,
    )
    doctor = run_doctor(
        role=DoctorRole.WRITER,
        probes=probes,
        timeout_seconds=1.0,
        strict=True,
    )

    preflight = evaluate_cutover_preflight(
        PreflightEvidence(
            row_classifications=AUTHORITATIVE_ROW_CLASSIFICATIONS,
            p0_p2_findings=0,
            doctor_outcome=doctor.outcome,
            evidence_scope="synthetic",
        )
    )

    assert doctor.cutover_ready is True
    assert preflight.ready is False
    assert preflight.reasons == ("synthetic-evidence-only",)
