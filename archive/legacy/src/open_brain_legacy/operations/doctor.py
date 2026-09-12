from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .models import DeploymentTarget, ExitClass, OperationsValidationError


class DoctorRole(StrEnum):
    PROBE = "probe"
    WRITER = "writer"


class ProbeName(StrEnum):
    CONFIGURATION = "configuration"
    QUEUE_AGE = "queue-age"
    SCHEMA = "schema"
    WRITER_OWNERSHIP = "writer-ownership"
    LOCK_STATE = "lock-state"
    BACKUP_EVIDENCE = "backup-evidence"
    STALE_REFERENCES = "stale-references"
    OPTIONAL_PROVIDER = "optional-provider"


class ProbeState(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "unavailable"


class FindingClass(StrEnum):
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


class DoctorOutcome(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNAVAILABLE = "unavailable"
    DIAGNOSIS_REQUIRED = "diagnosis-required"


MAX_PROBE_TIMEOUT_SECONDS = 30.0
_JOB_ID_PATTERN = re.compile(r"JOB-[0-9]{3}")
_UNHEALTHY_FINDINGS = {
    ProbeName.CONFIGURATION: FindingClass.CONFIGURATION_INVALID,
    ProbeName.QUEUE_AGE: FindingClass.QUEUE_STALE,
    ProbeName.SCHEMA: FindingClass.SCHEMA_MISMATCH,
    ProbeName.WRITER_OWNERSHIP: FindingClass.WRITER_OWNERSHIP_CONFLICT,
    ProbeName.LOCK_STATE: FindingClass.LOCK_UNHEALTHY,
    ProbeName.BACKUP_EVIDENCE: FindingClass.BACKUP_EVIDENCE_MISSING,
    ProbeName.STALE_REFERENCES: FindingClass.STALE_REFERENCE,
    ProbeName.OPTIONAL_PROVIDER: FindingClass.OPTIONAL_PROVIDER_UNREADY,
}
_UNAVAILABLE_FINDINGS = {
    ProbeName.CONFIGURATION: FindingClass.CONFIGURATION_UNAVAILABLE,
    ProbeName.QUEUE_AGE: FindingClass.QUEUE_AGE_UNAVAILABLE,
    ProbeName.SCHEMA: FindingClass.SCHEMA_UNAVAILABLE,
    ProbeName.WRITER_OWNERSHIP: FindingClass.WRITER_OWNERSHIP_UNAVAILABLE,
    ProbeName.LOCK_STATE: FindingClass.LOCK_STATE_UNAVAILABLE,
    ProbeName.BACKUP_EVIDENCE: FindingClass.BACKUP_EVIDENCE_UNAVAILABLE,
    ProbeName.STALE_REFERENCES: FindingClass.STALE_REFERENCES_UNAVAILABLE,
    ProbeName.OPTIONAL_PROVIDER: FindingClass.OPTIONAL_PROVIDER_UNAVAILABLE,
}


@dataclass(frozen=True, slots=True)
class ProbeReading:
    state: ProbeState
    count: int | None = None
    age_seconds: int | None = None
    observed_at: datetime | None = None
    target: DeploymentTarget | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ProbeState):
            raise OperationsValidationError("invalid probe state")
        _optional_nonnegative_integer(self.count, "probe count")
        _optional_nonnegative_integer(self.age_seconds, "probe age")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at, "probe timestamp"))
        if self.target is not None and not isinstance(self.target, DeploymentTarget):
            raise OperationsValidationError("invalid probe target")
        if self.state is ProbeState.UNAVAILABLE and any(
            value is not None
            for value in (self.count, self.age_seconds, self.observed_at, self.target)
        ):
            raise OperationsValidationError("unavailable probe cannot carry observations")

    @classmethod
    def healthy(
        cls,
        *,
        count: int | None = None,
        age_seconds: int | None = None,
        observed_at: datetime | None = None,
        target: DeploymentTarget | None = None,
    ) -> ProbeReading:
        return cls(
            state=ProbeState.HEALTHY,
            count=count,
            age_seconds=age_seconds,
            observed_at=observed_at,
            target=target,
        )

    @classmethod
    def unhealthy(
        cls,
        *,
        count: int | None = None,
        age_seconds: int | None = None,
        observed_at: datetime | None = None,
        target: DeploymentTarget | None = None,
    ) -> ProbeReading:
        return cls(
            state=ProbeState.UNHEALTHY,
            count=count,
            age_seconds=age_seconds,
            observed_at=observed_at,
            target=target,
        )

    @classmethod
    def unavailable(cls) -> ProbeReading:
        return cls(state=ProbeState.UNAVAILABLE)


DoctorProbe = Callable[[float], ProbeReading]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    probe: ProbeName
    state: ProbeState
    finding_class: FindingClass | None
    count: int | None
    age_seconds: int | None
    observed_at: datetime | None
    target: DeploymentTarget | None

    def to_dict(self) -> dict[str, object]:
        return {
            "probe": self.probe.value,
            "state": self.state.value,
            "finding_class": (
                self.finding_class.value if self.finding_class is not None else None
            ),
            "count": self.count,
            "age_seconds": self.age_seconds,
            "observed_at": _timestamp(self.observed_at),
            "target": self.target.value if self.target is not None else None,
        }


@dataclass(frozen=True, slots=True)
class HistoricalDiagnosis:
    job_id: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or _JOB_ID_PATTERN.fullmatch(self.job_id) is None:
            raise OperationsValidationError("invalid historical diagnosis job id")
        object.__setattr__(
            self,
            "observed_at",
            _utc(self.observed_at, "historical diagnosis timestamp"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "finding_class": FindingClass.HISTORICAL_NONZERO.value,
            "state": DoctorOutcome.DIAGNOSIS_REQUIRED.value,
            "observed_at": _timestamp(self.observed_at),
        }


@dataclass(frozen=True, slots=True, init=False)
class DoctorResult:
    schema_version: int
    role: DoctorRole
    strict: bool
    outcome: DoctorOutcome
    exit_code: int
    cutover_ready: bool
    checks: tuple[DoctorCheck, ...]
    findings: tuple[DoctorCheck, ...]
    historical_diagnoses: tuple[HistoricalDiagnosis, ...]

    def __init__(self) -> None:
        raise TypeError("DoctorResult must be created by run_doctor")

    @classmethod
    def _create(
        cls,
        *,
        role: DoctorRole,
        strict: bool,
        outcome: DoctorOutcome,
        exit_code: int,
        cutover_ready: bool,
        checks: tuple[DoctorCheck, ...],
        historical_diagnoses: tuple[HistoricalDiagnosis, ...],
    ) -> DoctorResult:
        result = cls.__new__(cls)
        object.__setattr__(result, "schema_version", 1)
        object.__setattr__(result, "role", role)
        object.__setattr__(result, "strict", strict)
        object.__setattr__(result, "outcome", outcome)
        object.__setattr__(result, "exit_code", exit_code)
        object.__setattr__(result, "cutover_ready", cutover_ready)
        object.__setattr__(result, "checks", checks)
        object.__setattr__(
            result,
            "findings",
            tuple(check for check in checks if check.finding_class is not None),
        )
        object.__setattr__(result, "historical_diagnoses", historical_diagnoses)
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "role": self.role.value,
            "strict": self.strict,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "cutover_ready": self.cutover_ready,
            "checks": [check.to_dict() for check in self.checks],
            "findings": [finding.to_dict() for finding in self.findings],
            "historical_diagnoses": [
                diagnosis.to_dict() for diagnosis in self.historical_diagnoses
            ],
        }


def run_doctor(
    *,
    role: DoctorRole,
    probes: Mapping[ProbeName, DoctorProbe],
    timeout_seconds: float,
    strict: bool,
    historical_diagnoses: tuple[HistoricalDiagnosis, ...] = (),
) -> DoctorResult:
    if not isinstance(role, DoctorRole):
        raise OperationsValidationError("invalid doctor role")
    if not isinstance(strict, bool):
        raise OperationsValidationError("invalid strict flag")
    timeout = _probe_timeout(timeout_seconds)
    normalized_probes = _probes(probes)
    diagnoses = _historical_diagnoses(historical_diagnoses)
    checks = tuple(
        _collect_probe(probe, normalized_probes.get(probe), timeout) for probe in ProbeName
    )
    outcome = _outcome(checks, diagnoses)
    exit_code = _exit_code(outcome, strict)
    cutover_ready = role is DoctorRole.WRITER and outcome is DoctorOutcome.HEALTHY
    return DoctorResult._create(
        role=role,
        strict=strict,
        outcome=outcome,
        exit_code=exit_code,
        cutover_ready=cutover_ready,
        checks=checks,
        historical_diagnoses=diagnoses,
    )


def _collect_probe(
    probe: ProbeName,
    collector: DoctorProbe | None,
    timeout_seconds: float,
) -> DoctorCheck:
    if collector is None:
        return _unavailable_check(probe, _UNAVAILABLE_FINDINGS[probe])
    try:
        reading = collector(timeout_seconds)
    except TimeoutError:
        return _unavailable_check(probe, FindingClass.PROBE_TIMEOUT)
    except Exception:
        return _unavailable_check(probe, FindingClass.PROBE_FAILURE)
    if not isinstance(reading, ProbeReading):
        return _unavailable_check(probe, FindingClass.PROBE_FAILURE)
    reading = _apply_ownership_contract(probe, reading)
    finding_class: FindingClass | None = None
    if reading.state is ProbeState.UNHEALTHY:
        finding_class = _UNHEALTHY_FINDINGS[probe]
    elif reading.state is ProbeState.UNAVAILABLE:
        finding_class = _UNAVAILABLE_FINDINGS[probe]
    return DoctorCheck(
        probe=probe,
        state=reading.state,
        finding_class=finding_class,
        count=reading.count,
        age_seconds=reading.age_seconds,
        observed_at=reading.observed_at,
        target=reading.target,
    )


def _apply_ownership_contract(probe: ProbeName, reading: ProbeReading) -> ProbeReading:
    if (
        probe is ProbeName.WRITER_OWNERSHIP
        and reading.state is ProbeState.HEALTHY
        and (
            reading.target is not DeploymentTarget.CANONICAL_WRITER
            or (reading.count is not None and reading.count != 1)
        )
    ):
        return ProbeReading.unhealthy(
            count=reading.count,
            age_seconds=reading.age_seconds,
            observed_at=reading.observed_at,
            target=reading.target,
        )
    return reading


def _unavailable_check(probe: ProbeName, finding_class: FindingClass) -> DoctorCheck:
    return DoctorCheck(
        probe=probe,
        state=ProbeState.UNAVAILABLE,
        finding_class=finding_class,
        count=None,
        age_seconds=None,
        observed_at=None,
        target=None,
    )


def _outcome(
    checks: tuple[DoctorCheck, ...],
    historical_diagnoses: tuple[HistoricalDiagnosis, ...],
) -> DoctorOutcome:
    if any(check.state is ProbeState.UNHEALTHY for check in checks):
        return DoctorOutcome.UNHEALTHY
    if any(check.state is ProbeState.UNAVAILABLE for check in checks):
        return DoctorOutcome.UNAVAILABLE
    if historical_diagnoses:
        return DoctorOutcome.DIAGNOSIS_REQUIRED
    return DoctorOutcome.HEALTHY


def _exit_code(outcome: DoctorOutcome, strict: bool) -> int:
    if outcome is DoctorOutcome.UNHEALTHY:
        return 1
    if strict and outcome is DoctorOutcome.UNAVAILABLE:
        return int(ExitClass.CONFIGURATION)
    if strict and outcome is DoctorOutcome.DIAGNOSIS_REQUIRED:
        return 1
    return int(ExitClass.SUCCESS)


def _probes(probes: Mapping[ProbeName, DoctorProbe]) -> dict[ProbeName, DoctorProbe]:
    if not isinstance(probes, Mapping):
        raise OperationsValidationError("invalid doctor probes")
    normalized: dict[ProbeName, DoctorProbe] = {}
    for name, collector in probes.items():
        if not isinstance(name, ProbeName) or not callable(collector):
            raise OperationsValidationError("invalid doctor probes")
        normalized[name] = collector
    return normalized


def _historical_diagnoses(
    diagnoses: tuple[HistoricalDiagnosis, ...],
) -> tuple[HistoricalDiagnosis, ...]:
    if (
        not isinstance(diagnoses, tuple)
        or any(not isinstance(diagnosis, HistoricalDiagnosis) for diagnosis in diagnoses)
        or len({diagnosis.job_id for diagnosis in diagnoses}) != len(diagnoses)
    ):
        raise OperationsValidationError("invalid historical diagnoses")
    return tuple(sorted(diagnoses, key=lambda diagnosis: diagnosis.job_id))


def _probe_timeout(value: float) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 < value <= MAX_PROBE_TIMEOUT_SECONDS
    ):
        raise OperationsValidationError("invalid probe timeout")
    return float(value)


def _optional_nonnegative_integer(value: int | None, label: str) -> None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value < 0
    ):
        raise OperationsValidationError(f"invalid {label}")


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OperationsValidationError(f"invalid {label}")
    return value.astimezone(UTC)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")

