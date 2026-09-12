from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest

from open_brain_legacy.operations.doctor import (
    DoctorOutcome,
    DoctorRole,
    FindingClass,
    ProbeName,
    ProbeReading,
    ProbeState,
    run_doctor,
)
from open_brain_legacy.operations.models import DeploymentTarget
from open_brain_legacy.operations.status import (
    StatusMetric,
    StatusOutcome,
    StatusReading,
    StatusUnavailableClass,
    collect_status,
)

OBSERVED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _doctor_probes(
    overrides: dict[ProbeName, Callable[[float], ProbeReading]] | None = None,
) -> dict[ProbeName, Callable[[float], ProbeReading]]:
    probes: dict[ProbeName, Callable[[float], ProbeReading]] = {
        ProbeName.CONFIGURATION: lambda timeout: ProbeReading.healthy(observed_at=OBSERVED_AT),
        ProbeName.QUEUE_AGE: lambda timeout: ProbeReading.healthy(
            age_seconds=30, count=2, observed_at=OBSERVED_AT
        ),
        ProbeName.SCHEMA: lambda timeout: ProbeReading.healthy(observed_at=OBSERVED_AT),
        ProbeName.WRITER_OWNERSHIP: lambda timeout: ProbeReading.healthy(
            target=DeploymentTarget.CANONICAL_WRITER,
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
    probes.update(overrides or {})
    return probes


def _status_probes(
    overrides: dict[StatusMetric, Callable[[float], StatusReading]] | None = None,
) -> dict[StatusMetric, Callable[[float], StatusReading]]:
    values = {
        StatusMetric.CAPTURES_TODAY: 4,
        StatusMetric.OPEN_REVIEWS: 2,
        StatusMetric.INDEX_AGE: 90,
        StatusMetric.FAILED_JOBS: 0,
        StatusMetric.EVENT_BACKLOG: 3,
        StatusMetric.STALE_REVIEWS: 1,
        StatusMetric.BACKUP_AGE: 120,
        StatusMetric.RETRIEVAL_EVENTS: 7,
    }
    def available_probe(value: int) -> Callable[[float], StatusReading]:
        def probe(timeout_seconds: float) -> StatusReading:
            return StatusReading.available(value=value, observed_at=OBSERVED_AT)

        return probe

    probes = {metric: available_probe(value) for metric, value in values.items()}
    probes.update(overrides or {})
    return probes


def test_doctor_is_deterministic_metadata_only_and_passes_a_bounded_timeout() -> None:
    timeouts: list[float] = []

    def configuration(timeout_seconds: float) -> ProbeReading:
        timeouts.append(timeout_seconds)
        return ProbeReading.healthy(observed_at=OBSERVED_AT)

    probes = _doctor_probes({ProbeName.CONFIGURATION: configuration})
    first = run_doctor(
        role=DoctorRole.WRITER,
        probes=dict(reversed(tuple(probes.items()))),
        timeout_seconds=2.5,
        strict=True,
    )
    second = run_doctor(
        role=DoctorRole.WRITER,
        probes=probes,
        timeout_seconds=2.5,
        strict=True,
    )

    assert first.to_dict() == second.to_dict()
    assert first.outcome is DoctorOutcome.HEALTHY
    assert first.exit_code == 0
    assert first.cutover_ready is True
    assert timeouts == [2.5, 2.5]
    assert [check.probe for check in first.checks] == list(ProbeName)

    payload = first.to_dict()
    checks = cast(list[dict[str, object]], payload["checks"])
    assert checks[1] == {
        "probe": "queue-age",
        "state": "healthy",
        "finding_class": None,
        "count": 2,
        "age_seconds": 30,
        "observed_at": "2026-08-14T12:00:00Z",
        "target": None,
    }
    serialized = json.dumps(payload).lower()
    for forbidden_field in (
        '"error":',
        '"message":',
        '"path":',
        '"content":',
        '"url":',
        '"exception":',
    ):
        assert forbidden_field not in serialized


def test_doctor_maps_unhealthy_unavailable_timeout_and_failure_without_error_text() -> None:
    canary = "private path /synthetic/private and secret=fixture"

    def timed_out(timeout_seconds: float) -> ProbeReading:
        raise TimeoutError(canary)

    def failed(timeout_seconds: float) -> ProbeReading:
        raise RuntimeError(canary)

    result = run_doctor(
        role=DoctorRole.WRITER,
        probes=_doctor_probes(
            {
                ProbeName.CONFIGURATION: lambda timeout: ProbeReading.unavailable(),
                ProbeName.QUEUE_AGE: lambda timeout: ProbeReading.unhealthy(
                    age_seconds=7200, count=9, observed_at=OBSERVED_AT
                ),
                ProbeName.SCHEMA: failed,
                ProbeName.BACKUP_EVIDENCE: timed_out,
            }
        ),
        timeout_seconds=1.0,
        strict=True,
    )

    assert result.outcome is DoctorOutcome.UNHEALTHY
    assert result.exit_code == 1
    assert [finding.finding_class for finding in result.findings] == [
        FindingClass.CONFIGURATION_UNAVAILABLE,
        FindingClass.QUEUE_STALE,
        FindingClass.PROBE_FAILURE,
        FindingClass.PROBE_TIMEOUT,
    ]
    assert canary not in json.dumps(result.to_dict())


def test_doctor_missing_probe_and_invalid_timeout_fail_closed() -> None:
    strict = run_doctor(
        role=DoctorRole.WRITER,
        probes={},
        timeout_seconds=1.0,
        strict=True,
    )
    informational = run_doctor(
        role=DoctorRole.WRITER,
        probes={},
        timeout_seconds=1.0,
        strict=False,
    )

    assert strict.outcome is DoctorOutcome.UNAVAILABLE
    assert strict.exit_code == 78
    assert strict.cutover_ready is False
    assert informational.outcome is DoctorOutcome.UNAVAILABLE
    assert informational.exit_code == 0
    assert all(check.state is ProbeState.UNAVAILABLE for check in strict.checks)

    for timeout_seconds in (0, -1, 30.1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="probe timeout"):
            run_doctor(
                role=DoctorRole.PROBE,
                probes={},
                timeout_seconds=timeout_seconds,
                strict=True,
            )


def test_status_normalizes_counts_ages_and_explicit_unavailable_states() -> None:
    canary = "content from /synthetic/private"

    def failed(timeout_seconds: float) -> StatusReading:
        raise RuntimeError(canary)

    def timed_out(timeout_seconds: float) -> StatusReading:
        raise TimeoutError(canary)

    result = collect_status(
        probes=_status_probes(
            {
                StatusMetric.OPEN_REVIEWS: lambda timeout: StatusReading.unavailable(
                    StatusUnavailableClass.NOT_CONFIGURED
                ),
                StatusMetric.FAILED_JOBS: failed,
                StatusMetric.BACKUP_AGE: timed_out,
            }
        ),
        timeout_seconds=3.0,
        strict=True,
    )

    assert result.outcome is StatusOutcome.PARTIAL
    assert result.exit_code == 78
    assert [metric.metric for metric in result.metrics] == list(StatusMetric)
    assert result.to_dict()["metrics"] == [
        {
            "metric": "captures-today",
            "state": "available",
            "unit": "count",
            "value": 4,
            "observed_at": "2026-08-14T12:00:00Z",
            "unavailable_class": None,
        },
        {
            "metric": "open-reviews",
            "state": "unavailable",
            "unit": "count",
            "value": None,
            "observed_at": None,
            "unavailable_class": "not-configured",
        },
        {
            "metric": "index-age",
            "state": "available",
            "unit": "seconds",
            "value": 90,
            "observed_at": "2026-08-14T12:00:00Z",
            "unavailable_class": None,
        },
        {
            "metric": "failed-jobs",
            "state": "unavailable",
            "unit": "count",
            "value": None,
            "observed_at": None,
            "unavailable_class": "probe-failure",
        },
        {
            "metric": "event-backlog",
            "state": "available",
            "unit": "count",
            "value": 3,
            "observed_at": "2026-08-14T12:00:00Z",
            "unavailable_class": None,
        },
        {
            "metric": "stale-reviews",
            "state": "available",
            "unit": "count",
            "value": 1,
            "observed_at": "2026-08-14T12:00:00Z",
            "unavailable_class": None,
        },
        {
            "metric": "backup-age",
            "state": "unavailable",
            "unit": "seconds",
            "value": None,
            "observed_at": None,
            "unavailable_class": "probe-timeout",
        },
        {
            "metric": "retrieval-events",
            "state": "available",
            "unit": "count",
            "value": 7,
            "observed_at": "2026-08-14T12:00:00Z",
            "unavailable_class": None,
        },
    ]
    assert canary not in json.dumps(result.to_dict())


def test_doctor_and_status_application_seams_do_not_discover_live_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("application seam attempted live access")

    monkeypatch.setattr("builtins.open", unexpected_access)
    monkeypatch.setattr(os, "getenv", unexpected_access)

    doctor = run_doctor(
        role=DoctorRole.PROBE,
        probes=_doctor_probes(),
        timeout_seconds=1.0,
        strict=True,
    )
    status = collect_status(
        probes=_status_probes(),
        timeout_seconds=1.0,
        strict=True,
    )

    assert doctor.exit_code == 0
    assert status.exit_code == 0
