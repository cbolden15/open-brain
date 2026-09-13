from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import ScheduledRouteSpec
from open_brain_legacy.cli.main import main
from open_brain_legacy.cli.scheduled import (
    ScheduledApplicationAdapters,
    ScheduledDispatchResult,
    UnavailableScheduledAdapters,
    dispatch_scheduled_route,
    scheduled_result_envelope,
)
from open_brain_legacy.operations.capture_jobs import CaptureJobApplication
from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import ExitClass, JobSpec
from open_brain_legacy.operations.writer_jobs import WriterJobSpec

WAVE4_ROUTES = (
    ("JOB-001", "optional", JobSpec),
    ("JOB-002", "optional", JobSpec),
    ("JOB-003", "optional", JobSpec),
    ("JOB-004", "optional", JobSpec),
    ("JOB-005", "capture", CaptureJobApplication),
    ("JOB-006", "writer", WriterJobSpec),
    ("JOB-007", "writer", WriterJobSpec),
    ("JOB-008", "writer", WriterJobSpec),
    ("JOB-009", "writer", WriterJobSpec),
    ("JOB-010", "writer", WriterJobSpec),
    ("JOB-011", "writer", WriterJobSpec),
    ("JOB-012", "writer", WriterJobSpec),
    ("JOB-013", "optional", JobSpec),
    ("JOB-014", "writer", WriterJobSpec),
    ("JOB-015", "writer", WriterJobSpec),
    ("JOB-016", "writer", WriterJobSpec),
    ("JOB-017", "optional", JobSpec),
    ("JOB-018", "optional", JobSpec),
    ("JOB-019", "optional", JobSpec),
    ("JOB-020", "optional", JobSpec),
    ("JOB-021", "optional", JobSpec),
    ("JOB-022", "writer", WriterJobSpec),
    ("JOB-023", "writer", WriterJobSpec),
    ("JOB-024", "optional", JobSpec),
    ("JOB-025", "writer", WriterJobSpec),
    ("JOB-026", "optional", JobSpec),
    ("JOB-027", "capture", CaptureJobApplication),
    ("JOB-028", "capture", CaptureJobApplication),
    ("JOB-029", "capture", CaptureJobApplication),
    ("JOB-030", "optional", JobSpec),
)

DISPATCHABLE_WAVE4_ROUTES = WAVE4_ROUTES


@dataclass
class RecordingScheduledAdapters:
    calls: list[tuple[str, object]] = field(default_factory=list)

    def dispatch_capture(
        self, application: CaptureJobApplication
    ) -> ScheduledDispatchResult:
        self.calls.append(("capture", application))
        return ScheduledDispatchResult.completed(application.job.id)

    def dispatch_writer(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        self.calls.append(("writer", application))
        return ScheduledDispatchResult.completed(application.job_id)

    def dispatch_optional(self, application: JobSpec) -> ScheduledDispatchResult:
        self.calls.append(("optional", application))
        return ScheduledDispatchResult.completed(application.id)


@pytest.mark.parametrize(
    ("result", "expected_exit", "error_code"),
    (
        (
            ScheduledDispatchResult.lock_held("JOB-011"),
            ExitClass.LOCK_HELD,
            "scheduled_application_lock_held",
        ),
        (
            ScheduledDispatchResult.configuration("JOB-011"),
            ExitClass.CONFIGURATION,
            "scheduled_application_configuration",
        ),
    ),
)
def test_scheduled_result_preserves_operational_exit_classes(
    result: ScheduledDispatchResult,
    expected_exit: ExitClass,
    error_code: str,
) -> None:
    assert result.exit_code == int(expected_exit)
    assert scheduled_result_envelope(result)["error"] == {
        "code": error_code,
        "message": "operation unavailable; details redacted",
        "redacted": True,
    }


@pytest.mark.parametrize(
    ("job_id", "adapter_name", "application_type"), DISPATCHABLE_WAVE4_ROUTES
)
def test_every_enabled_or_manual_wave4_catalog_argv_reaches_its_typed_application_adapter(
    job_id: str,
    adapter_name: str,
    application_type: type[object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = get_job(job_id)
    adapters = RecordingScheduledAdapters()

    exit_code = main(
        job.command[1:],
        scheduled_adapters=adapters,
        scheduled_job_id=job_id,
    )

    assert exit_code not in {ExitCode.USAGE, ExitCode.DEFERRED}
    assert exit_code is ExitCode.SUCCESS
    assert len(adapters.calls) == 1
    actual_adapter_name, application = adapters.calls[0]
    assert actual_adapter_name == adapter_name
    assert isinstance(application, application_type)
    if isinstance(application, CaptureJobApplication):
        command = application.argv
    else:
        assert isinstance(application, (WriterJobSpec, JobSpec))
        command = application.command
    assert command == job.command
    output = capsys.readouterr().out
    assert "invalid" not in output
    assert "deferred" not in output


@pytest.mark.parametrize("job_id", [route[0] for route in WAVE4_ROUTES])
def test_every_wave4_catalog_argv_uses_safe_unavailable_adapter_without_injection(
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ScheduledApplicationAdapters]] = []

    def recording_dispatch(
        route: ScheduledRouteSpec,
        adapters: ScheduledApplicationAdapters,
    ) -> ScheduledDispatchResult:
        calls.append((route.job_id, adapters))
        return dispatch_scheduled_route(route, adapters)

    monkeypatch.setattr('open_brain_legacy.cli.main.dispatch_scheduled_route', recording_dispatch)
    job = get_job(job_id)

    exit_code = main(job.command[1:], scheduled_job_id=job_id)

    assert exit_code is ExitCode.FAILURE
    assert exit_code not in {ExitCode.USAGE, ExitCode.DEFERRED}
    assert len(calls) == 1
    actual_job_id, adapters = calls[0]
    assert actual_job_id == job_id
    assert isinstance(adapters, UnavailableScheduledAdapters)
    assert tuple(tmp_path.iterdir()) == ()
    output = capsys.readouterr().out
    if "--json" in job.command:
        assert json.loads(output) == {
            "command": job_id,
            "error": {
                "code": "scheduled_application_unavailable",
                "message": "operation unavailable; details redacted",
                "redacted": True,
            },
            "status": "unavailable",
        }
    else:
        assert output == "scheduled application unavailable\n"
