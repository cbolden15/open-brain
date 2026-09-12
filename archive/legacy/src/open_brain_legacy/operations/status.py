from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .models import ExitClass, OperationsValidationError


class StatusMetric(StrEnum):
    CAPTURES_TODAY = "captures-today"
    OPEN_REVIEWS = "open-reviews"
    INDEX_AGE = "index-age"
    FAILED_JOBS = "failed-jobs"
    EVENT_BACKLOG = "event-backlog"
    STALE_REVIEWS = "stale-reviews"
    BACKUP_AGE = "backup-age"
    RETRIEVAL_EVENTS = "retrieval-events"


class StatusUnit(StrEnum):
    COUNT = "count"
    SECONDS = "seconds"


class StatusState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class StatusUnavailableClass(StrEnum):
    NOT_CONFIGURED = "not-configured"
    PROBE_TIMEOUT = "probe-timeout"
    PROBE_FAILURE = "probe-failure"


class StatusOutcome(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


MAX_STATUS_PROBE_TIMEOUT_SECONDS = 30.0
_METRIC_UNITS = {
    StatusMetric.CAPTURES_TODAY: StatusUnit.COUNT,
    StatusMetric.OPEN_REVIEWS: StatusUnit.COUNT,
    StatusMetric.INDEX_AGE: StatusUnit.SECONDS,
    StatusMetric.FAILED_JOBS: StatusUnit.COUNT,
    StatusMetric.EVENT_BACKLOG: StatusUnit.COUNT,
    StatusMetric.STALE_REVIEWS: StatusUnit.COUNT,
    StatusMetric.BACKUP_AGE: StatusUnit.SECONDS,
    StatusMetric.RETRIEVAL_EVENTS: StatusUnit.COUNT,
}


@dataclass(frozen=True, slots=True)
class StatusReading:
    state: StatusState
    value: int | None
    observed_at: datetime | None
    unavailable_class: StatusUnavailableClass | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, StatusState):
            raise OperationsValidationError("invalid status state")
        if self.state is StatusState.AVAILABLE:
            if (
                not isinstance(self.value, int)
                or isinstance(self.value, bool)
                or self.value < 0
                or self.unavailable_class is not None
            ):
                raise OperationsValidationError("invalid available status reading")
            if self.observed_at is not None:
                object.__setattr__(self, "observed_at", _utc(self.observed_at))
            return
        if (
            self.value is not None
            or self.observed_at is not None
            or not isinstance(self.unavailable_class, StatusUnavailableClass)
        ):
            raise OperationsValidationError("invalid unavailable status reading")

    @classmethod
    def available(
        cls,
        *,
        value: int,
        observed_at: datetime | None = None,
    ) -> StatusReading:
        return cls(
            state=StatusState.AVAILABLE,
            value=value,
            observed_at=observed_at,
            unavailable_class=None,
        )

    @classmethod
    def unavailable(cls, unavailable_class: StatusUnavailableClass) -> StatusReading:
        return cls(
            state=StatusState.UNAVAILABLE,
            value=None,
            observed_at=None,
            unavailable_class=unavailable_class,
        )


StatusProbe = Callable[[float], StatusReading]


@dataclass(frozen=True, slots=True)
class StatusMetricResult:
    metric: StatusMetric
    state: StatusState
    unit: StatusUnit
    value: int | None
    observed_at: datetime | None
    unavailable_class: StatusUnavailableClass | None

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric.value,
            "state": self.state.value,
            "unit": self.unit.value,
            "value": self.value,
            "observed_at": _timestamp(self.observed_at),
            "unavailable_class": (
                self.unavailable_class.value if self.unavailable_class is not None else None
            ),
        }


@dataclass(frozen=True, slots=True, init=False)
class StatusResult:
    schema_version: int
    strict: bool
    outcome: StatusOutcome
    exit_code: int
    metrics: tuple[StatusMetricResult, ...]

    def __init__(self) -> None:
        raise TypeError("StatusResult must be created by collect_status")

    @classmethod
    def _create(
        cls,
        *,
        strict: bool,
        outcome: StatusOutcome,
        exit_code: int,
        metrics: tuple[StatusMetricResult, ...],
    ) -> StatusResult:
        result = cls.__new__(cls)
        object.__setattr__(result, "schema_version", 1)
        object.__setattr__(result, "strict", strict)
        object.__setattr__(result, "outcome", outcome)
        object.__setattr__(result, "exit_code", exit_code)
        object.__setattr__(result, "metrics", metrics)
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strict": self.strict,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "metrics": [metric.to_dict() for metric in self.metrics],
        }


def collect_status(
    *,
    probes: Mapping[StatusMetric, StatusProbe],
    timeout_seconds: float,
    strict: bool,
) -> StatusResult:
    if not isinstance(strict, bool):
        raise OperationsValidationError("invalid strict flag")
    timeout = _probe_timeout(timeout_seconds)
    normalized_probes = _probes(probes)
    metrics = tuple(
        _collect_metric(metric, normalized_probes.get(metric), timeout)
        for metric in StatusMetric
    )
    outcome = (
        StatusOutcome.COMPLETE
        if all(metric.state is StatusState.AVAILABLE for metric in metrics)
        else StatusOutcome.PARTIAL
    )
    exit_code = (
        int(ExitClass.CONFIGURATION)
        if strict and outcome is StatusOutcome.PARTIAL
        else int(ExitClass.SUCCESS)
    )
    return StatusResult._create(
        strict=strict,
        outcome=outcome,
        exit_code=exit_code,
        metrics=metrics,
    )


def _collect_metric(
    metric: StatusMetric,
    collector: StatusProbe | None,
    timeout_seconds: float,
) -> StatusMetricResult:
    if collector is None:
        reading = StatusReading.unavailable(StatusUnavailableClass.NOT_CONFIGURED)
    else:
        try:
            reading = collector(timeout_seconds)
        except TimeoutError:
            reading = StatusReading.unavailable(StatusUnavailableClass.PROBE_TIMEOUT)
        except Exception:
            reading = StatusReading.unavailable(StatusUnavailableClass.PROBE_FAILURE)
        if not isinstance(reading, StatusReading):
            reading = StatusReading.unavailable(StatusUnavailableClass.PROBE_FAILURE)
    return StatusMetricResult(
        metric=metric,
        state=reading.state,
        unit=_METRIC_UNITS[metric],
        value=reading.value,
        observed_at=reading.observed_at,
        unavailable_class=reading.unavailable_class,
    )


def _probes(probes: Mapping[StatusMetric, StatusProbe]) -> dict[StatusMetric, StatusProbe]:
    if not isinstance(probes, Mapping):
        raise OperationsValidationError("invalid status probes")
    normalized: dict[StatusMetric, StatusProbe] = {}
    for metric, collector in probes.items():
        if not isinstance(metric, StatusMetric) or not callable(collector):
            raise OperationsValidationError("invalid status probes")
        normalized[metric] = collector
    return normalized


def _probe_timeout(value: float) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 < value <= MAX_STATUS_PROBE_TIMEOUT_SECONDS
    ):
        raise OperationsValidationError("invalid status probe timeout")
    return float(value)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OperationsValidationError("invalid status timestamp")
    return value.astimezone(UTC)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")

