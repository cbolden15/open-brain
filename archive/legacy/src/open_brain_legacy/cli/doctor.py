"""Metadata-only doctor serializer for public CLI callers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy.operations.doctor import (
    DoctorProbe,
    DoctorResult,
    DoctorRole,
    ProbeName,
    run_doctor,
)


@dataclass(frozen=True, slots=True)
class DoctorCliResult:
    """A deterministic diagnostic envelope with the typed service exit code."""

    exit_code: int
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize diagnostic metadata with stable key ordering."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class DoctorCommandAdapter:
    """Route the one doctor family to probe-backed diagnostics."""

    probes: Mapping[ProbeName, DoctorProbe] | None = None
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.probes is not None:
            object.__setattr__(self, "probes", MappingProxyType(dict(self.probes)))

    def dispatch(self, argv: tuple[str, ...]) -> DoctorCliResult:
        options = tuple(argument for argument in argv if argument != "--json")
        roles: dict[tuple[str, ...], DoctorRole] = {
            ("--role=writer",): DoctorRole.WRITER,
            ("--role=probe",): DoctorRole.PROBE,
        }
        role = roles.get(options)
        if role is None:
            return DoctorCliResult(
                ExitCode.USAGE,
                {
                    "command": "doctor",
                    "error": redacted_error("invalid_doctor_request"),
                    "status": "invalid",
                },
            )
        if self.probes is None:
            return DoctorCliResult(
                ExitCode.FAILURE,
                {
                    "command": "doctor",
                    "error": redacted_error("doctor_probes_unavailable"),
                    "status": "unavailable",
                },
            )
        try:
            doctor_result = run_doctor(
                role=role,
                probes=self.probes,
                timeout_seconds=self.timeout_seconds,
                strict=True,
            )
        except Exception:
            return DoctorCliResult(
                ExitCode.FAILURE,
                {
                    "command": "doctor",
                    "error": redacted_error("doctor_failed"),
                    "status": "failed",
                },
            )
        return DoctorCliResult(
            ExitCode.SUCCESS if doctor_result.exit_code == 0 else ExitCode.FAILURE,
            _public_doctor_envelope(doctor_result),
        )


def show_doctor(*, result: DoctorResult) -> DoctorCliResult:
    """Serialize allow-listed findings without probing or exposing exception details."""
    return DoctorCliResult(
        exit_code=result.exit_code,
        envelope={
            "checks": [check.to_dict() for check in result.checks],
            "command": "doctor",
            "cutover_ready": result.cutover_ready,
            "findings": [finding.to_dict() for finding in result.findings],
            "historical_diagnoses": [
                diagnosis.to_dict() for diagnosis in result.historical_diagnoses
            ],
            "role": result.role.value,
            "schema_version": result.schema_version,
            "status": result.outcome.value,
            "strict": result.strict,
        },
    )


def _public_doctor_envelope(result: DoctorResult) -> dict[str, object]:
    """Serialize diagnostics without the reserved cutover readiness claim."""
    return {
        "checks": [check.to_dict() for check in result.checks],
        "command": "doctor",
        "findings": [finding.to_dict() for finding in result.findings],
        "historical_diagnoses": [
            diagnosis.to_dict() for diagnosis in result.historical_diagnoses
        ],
        "role": result.role.value,
        "schema_version": result.schema_version,
        "status": result.outcome.value,
        "strict": result.strict,
    }
