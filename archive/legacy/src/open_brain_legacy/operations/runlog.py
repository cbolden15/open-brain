from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from .models import ExitClass, OperationsValidationError
from .scheduler import EXPECTED_JOB_IDS


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED_LOCKED = "skipped-locked"
    CONFIGURATION_FAILED = "configuration-failed"
    FAILED = "failed"


class RunErrorClass(StrEnum):
    LOCK_HELD = "lock-held"
    CONFIGURATION = "configuration"
    JOB_FAILURE = "job-failure"


MAX_RUN_METRICS = 8
_JOB_METRIC_ALLOWLISTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        **{job_id: frozenset() for job_id in EXPECTED_JOB_IDS},
        "JOB-012": frozenset({"items_considered", "items_written"}),
    }
)
_OUTCOME_ERROR_CLASSES: Mapping[RunOutcome, RunErrorClass | None] = MappingProxyType(
    {
        RunOutcome.SUCCEEDED: None,
        RunOutcome.SKIPPED_LOCKED: RunErrorClass.LOCK_HELD,
        RunOutcome.CONFIGURATION_FAILED: RunErrorClass.CONFIGURATION,
        RunOutcome.FAILED: RunErrorClass.JOB_FAILURE,
    }
)


def classify_exit_code(exit_code: int) -> RunOutcome:
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not 0 <= exit_code <= 255:
        raise OperationsValidationError("invalid exit code")
    if exit_code == ExitClass.SUCCESS:
        return RunOutcome.SUCCEEDED
    if exit_code == ExitClass.LOCK_HELD:
        return RunOutcome.SKIPPED_LOCKED
    if exit_code == ExitClass.CONFIGURATION:
        return RunOutcome.CONFIGURATION_FAILED
    return RunOutcome.FAILED


@dataclass(frozen=True, slots=True, init=False)
class RunMetadata:
    schema_version: Literal[1]
    job_id: str
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    exit_code: int
    outcome: RunOutcome
    error_class: RunErrorClass | None
    metrics: tuple[tuple[str, int | float], ...]

    def __init__(self) -> None:
        raise TypeError("RunMetadata must be created through its factory")

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        started_at: datetime,
        finished_at: datetime,
        exit_code: int,
        error_class: RunErrorClass | None,
        metrics: Mapping[str, int | float],
    ) -> RunMetadata:
        if job_id not in EXPECTED_JOB_IDS:
            raise OperationsValidationError("invalid run job id")
        start = _utc(started_at)
        finish = _utc(finished_at)
        if finish < start:
            raise OperationsValidationError("invalid run time range")
        outcome = classify_exit_code(exit_code)
        if error_class is not None and not isinstance(error_class, RunErrorClass):
            raise OperationsValidationError("invalid redacted error class")
        expected_error_class = _OUTCOME_ERROR_CLASSES[outcome]
        if error_class is not expected_error_class:
            raise OperationsValidationError("invalid error class for run outcome")
        normalized_metrics = _metrics(job_id, metrics)
        delta = finish - start
        duration_ms = delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
        metadata = cls.__new__(cls)
        object.__setattr__(metadata, "schema_version", 1)
        object.__setattr__(metadata, "job_id", job_id)
        object.__setattr__(metadata, "started_at", start)
        object.__setattr__(metadata, "finished_at", finish)
        object.__setattr__(metadata, "duration_ms", duration_ms)
        object.__setattr__(metadata, "exit_code", exit_code)
        object.__setattr__(metadata, "outcome", outcome)
        object.__setattr__(metadata, "error_class", error_class)
        object.__setattr__(metadata, "metrics", normalized_metrics)
        return metadata

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RunMetadata:
        """Restore one exact metadata-only record through the normal factory."""
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            "job_id",
            "started_at",
            "finished_at",
            "duration_ms",
            "exit_code",
            "outcome",
            "error_class",
            "metrics",
        }:
            raise OperationsValidationError("invalid run metadata")
        try:
            raw_error = value["error_class"]
            if raw_error is not None and not isinstance(raw_error, str):
                raise OperationsValidationError("invalid run metadata")
            error_class = None if raw_error is None else RunErrorClass(raw_error)
            raw_metrics = value["metrics"]
            if not isinstance(raw_metrics, Mapping):
                raise OperationsValidationError("invalid run metadata")
            job_id = value["job_id"]
            exit_code = value["exit_code"]
            if (
                not isinstance(job_id, str)
                or not isinstance(exit_code, int)
                or isinstance(exit_code, bool)
            ):
                raise OperationsValidationError("invalid run metadata")
            metrics: dict[str, int | float] = {}
            for name, metric in raw_metrics.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(metric, int | float)
                    or isinstance(metric, bool)
                ):
                    raise OperationsValidationError("invalid run metadata")
                metrics[name] = metric
            metadata = cls.create(
                job_id=job_id,
                started_at=_parse_timestamp(value["started_at"]),
                finished_at=_parse_timestamp(value["finished_at"]),
                exit_code=exit_code,
                error_class=error_class,
                metrics=metrics,
            )
            if (
                value["schema_version"] != 1
                or value["duration_ms"] != metadata.duration_ms
                or value["outcome"] != metadata.outcome.value
                or metadata.to_dict() != dict(value)
            ):
                raise OperationsValidationError("invalid run metadata")
        except (TypeError, ValueError):
            raise OperationsValidationError("invalid run metadata") from None
        return metadata

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "started_at": _timestamp(self.started_at),
            "finished_at": _timestamp(self.finished_at),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "outcome": self.outcome.value,
            "error_class": self.error_class.value if self.error_class is not None else None,
            "metrics": dict(self.metrics),
        }


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OperationsValidationError("run timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise OperationsValidationError("invalid run metadata")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise OperationsValidationError("invalid run metadata") from None
    if _timestamp(parsed) != value:
        raise OperationsValidationError("invalid run metadata")
    return parsed


def _metrics(
    job_id: str, values: Mapping[str, int | float]
) -> tuple[tuple[str, int | float], ...]:
    if not isinstance(values, Mapping):
        raise OperationsValidationError("invalid run metrics")
    if len(values) > MAX_RUN_METRICS:
        raise OperationsValidationError("too many run metrics")
    allowed_names = _JOB_METRIC_ALLOWLISTS[job_id]
    normalized: list[tuple[str, int | float]] = []
    for name, value in values.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise OperationsValidationError("invalid run metrics")
        if name not in allowed_names:
            raise OperationsValidationError("run metrics are not allowed for job")
        normalized.append((name, value))
    return tuple(sorted(normalized))
