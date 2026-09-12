from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.main import main
from open_brain_legacy.cli.scheduled import ScheduledDispatchResult, ScheduledDispatchStatus
from open_brain_legacy.operations.capture_jobs import CaptureJobApplication
from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import JobSpec
from open_brain_legacy.operations.writer_jobs import WriterJobSpec


@dataclass(frozen=True, slots=True)
class _Result:
    exit_code: ExitCode
    envelope: dict[str, object]


@dataclass(slots=True)
class _RecordingFamilyAdapter:
    command: str = "capture"
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def dispatch(self, argv: tuple[str, ...]) -> _Result:
        self.calls.append(argv)
        return _Result(
            ExitCode.SUCCESS,
            {"command": self.command, "status": "completed"},
        )


@dataclass(slots=True)
class _RecordingScheduledAdapters:
    calls: list[str] = field(default_factory=list)

    def dispatch_capture(
        self, application: CaptureJobApplication
    ) -> ScheduledDispatchResult:
        self.calls.append(application.job.id)
        return ScheduledDispatchResult.completed(application.job.id)

    def dispatch_writer(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        self.calls.append(application.job_id)
        return ScheduledDispatchResult.completed(application.job_id)

    def dispatch_optional(self, application: JobSpec) -> ScheduledDispatchResult:
        self.calls.append(application.id)
        return ScheduledDispatchResult.completed(application.id)


@dataclass(slots=True)
class _FailingScheduledAdapters:
    mode: str
    calls: list[str] = field(default_factory=list)

    def _dispatch(self, job_id: str, argv: tuple[str, ...]) -> ScheduledDispatchResult:
        self.calls.append(job_id)
        if self.mode == "raises":
            raise RuntimeError(
                f"synthetic-secret-value at /synthetic/private/adapter.log argv={argv!r}"
            )
        return ScheduledDispatchResult.completed("JOB-999")

    def dispatch_capture(
        self, application: CaptureJobApplication
    ) -> ScheduledDispatchResult:
        return self._dispatch(application.job.id, application.argv)

    def dispatch_writer(self, application: WriterJobSpec) -> ScheduledDispatchResult:
        return self._dispatch(application.job_id, application.command)

    def dispatch_optional(self, application: JobSpec) -> ScheduledDispatchResult:
        return self._dispatch(application.id, application.command)


def test_exact_scheduled_argv_is_owned_by_its_typed_route_not_the_family_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    family = _RecordingFamilyAdapter()
    scheduled = _RecordingScheduledAdapters()
    job = get_job("JOB-005")

    exit_code = main(
        job.command[1:],
        command_adapters=CommandAdapterRegistry({"capture": family}),
        scheduled_adapters=scheduled,
    )

    assert exit_code is ExitCode.SUCCESS
    assert scheduled.calls == ["JOB-005"]
    assert family.calls == []
    assert json.loads(capsys.readouterr().out) == {
        "command": "JOB-005",
        "status": "completed",
    }


@pytest.mark.parametrize("mode", ["raises", "mismatched"])
@pytest.mark.parametrize(
    "job_id",
    ["JOB-005", "JOB-006", "JOB-007", "JOB-008", "JOB-009", "JOB-012", "JOB-015"],
)
def test_scheduled_adapter_failures_are_redacted_at_the_public_cli_boundary(
    job_id: str,
    mode: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = get_job(job_id)
    scheduled = _FailingScheduledAdapters(mode)

    exit_code = main(job.command[1:], scheduled_adapters=scheduled)

    output = capsys.readouterr().out
    assert exit_code is ExitCode.FAILURE
    assert scheduled.calls == [job_id]
    assert json.loads(output) == {
        "command": job_id,
        "error": {
            "code": "scheduled_application_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }
    assert "RuntimeError" not in output
    assert "synthetic-secret-value" not in output
    assert "/synthetic/private/adapter.log" not in output
    assert "JOB-999" not in output


@pytest.mark.parametrize(
    ("exit_code", "status"),
    [
        (ExitCode.SUCCESS, ScheduledDispatchStatus.FAILED),
        (ExitCode.SUCCESS, ScheduledDispatchStatus.UNAVAILABLE),
        (ExitCode.FAILURE, ScheduledDispatchStatus.COMPLETED),
    ],
)
def test_scheduled_result_status_and_exit_must_agree(
    exit_code: ExitCode,
    status: ScheduledDispatchStatus,
) -> None:
    with pytest.raises(ValueError, match="status and exit disagree"):
        ScheduledDispatchResult("JOB-005", exit_code, status)


@pytest.mark.parametrize("action", ["edit", "archive"])
def test_review_actions_are_owned_by_the_injected_review_adapter(
    action: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = _RecordingFamilyAdapter(command="review")

    exit_code = main(
        ["review", action, "review_synthetic", "--json"],
        command_adapters=CommandAdapterRegistry({"review": adapter}),
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.SUCCESS
    assert adapter.calls == [(action, "review_synthetic", "--json")]
    assert output["status"] == "completed"
    assert "owner_gated" not in output


def test_adapter_registry_is_immutable_and_rejects_unowned_families() -> None:
    adapter = _RecordingFamilyAdapter()
    source = {"capture": adapter}
    registry = CommandAdapterRegistry(source)
    source.clear()

    assert registry.get("capture") is adapter
    with pytest.raises(ValueError, match="unknown command family"):
        CommandAdapterRegistry({"close-day": adapter})
