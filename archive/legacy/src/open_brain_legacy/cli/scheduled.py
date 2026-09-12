"""Typed, dependency-injected dispatch for scheduled public CLI routes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, TextIO

from open_brain_legacy._compat.open_brain.cli._common import redacted_error
from open_brain_legacy.cli._registry import ScheduledRouteSpec
from open_brain_legacy.operations.capture_jobs import CaptureJobApplication, get_capture_job
from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import ExitClass, JobSpec
from open_brain_legacy.operations.scheduled_results import (
    ScheduledDispatchResult,
    ScheduledDispatchStatus,
)
from open_brain_legacy.operations.writer_jobs import WriterJobSpec, get_writer_job_spec

__all__ = [
    "ScheduledApplicationAdapters",
    "ScheduledDispatchResult",
    "ScheduledDispatchStatus",
    "UnavailableScheduledAdapters",
    "dispatch_scheduled_route",
    "scheduled_result_envelope",
    "write_scheduled_result",
]


class ScheduledApplicationAdapters(Protocol):
    """Effect boundary supplied by the runtime composition layer."""

    def dispatch_capture(
        self, application: CaptureJobApplication
    ) -> ScheduledDispatchResult: ...

    def dispatch_writer(self, application: WriterJobSpec) -> ScheduledDispatchResult: ...

    def dispatch_optional(self, application: JobSpec) -> ScheduledDispatchResult: ...


@dataclass(frozen=True, slots=True)
class UnavailableScheduledAdapters:
    """Safe normal-console adapter that grants no application effect capability."""

    def dispatch_capture(
        self, application: CaptureJobApplication
    ) -> ScheduledDispatchResult:
        return ScheduledDispatchResult.unavailable(application.job.id)

    def dispatch_writer(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        return ScheduledDispatchResult.unavailable(application.job_id)

    def dispatch_optional(self, application: JobSpec) -> ScheduledDispatchResult:
        return ScheduledDispatchResult.unavailable(application.id)


def dispatch_scheduled_route(
    route: ScheduledRouteSpec,
    adapters: ScheduledApplicationAdapters,
) -> ScheduledDispatchResult:
    """Resolve one catalog route to the existing typed application adapter."""
    job = get_job(route.job_id)
    if route.adapter == "capture":
        result = adapters.dispatch_capture(get_capture_job(route.job_id))
    elif route.adapter == "writer":
        result = adapters.dispatch_writer(get_writer_job_spec(route.job_id))
    else:
        result = adapters.dispatch_optional(job)
    if not isinstance(result, ScheduledDispatchResult) or result.job_id != route.job_id:
        raise ValueError("scheduled adapter returned a mismatched result")
    return result


def write_scheduled_result(
    result: ScheduledDispatchResult,
    *,
    json_output: bool,
    stream: TextIO,
) -> None:
    """Write only closed, metadata-safe scheduled dispatch fields."""
    if json_output:
        stream.write(
            json.dumps(
                scheduled_result_envelope(result),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return
    stream.write(f"scheduled application {result.status.value}\n")


def scheduled_result_envelope(result: ScheduledDispatchResult) -> dict[str, object]:
    """Convert one owner record into the public CLI representation."""
    if not isinstance(result, ScheduledDispatchResult):
        raise ValueError("invalid scheduled dispatch result")
    envelope: dict[str, object] = {
        "command": result.job_id,
        "status": result.status.value,
    }
    if result.status is not ScheduledDispatchStatus.COMPLETED:
        if result.exit_code == ExitClass.LOCK_HELD:
            error_code = "scheduled_application_lock_held"
        elif result.exit_code == ExitClass.CONFIGURATION:
            error_code = "scheduled_application_configuration"
        elif result.status is ScheduledDispatchStatus.FAILED:
            error_code = "scheduled_application_failed"
        else:
            error_code = "scheduled_application_unavailable"
        envelope["error"] = redacted_error(error_code)
    return envelope
