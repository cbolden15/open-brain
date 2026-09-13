from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.operations import show_cron
from open_brain_legacy.operations.runlog import RunMetadata


class SpoofedCronAction:
    def __eq__(self, other: object) -> bool:
        return other == "report"


@dataclass
class RecordingCronReader:
    calls: list[int] = field(default_factory=list)

    def reports(self, *, window_seconds: int) -> tuple[RunMetadata, ...]:
        self.calls.append(window_seconds)
        return (
            RunMetadata.create(
                job_id="JOB-012",
                started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                finished_at=datetime(2026, 1, 2, 3, 4, 6, tzinfo=UTC),
                exit_code=0,
                error_class=None,
                metrics={"items_considered": 2},
            ),
        )


def test_cron_reports_a_bounded_window_and_rejects_unknown_actions() -> None:
    reader = RecordingCronReader()

    result = show_cron(reader=reader, window_seconds=3_600)
    unknown = show_cron(reader=reader, action="run")

    assert result.exit_code is ExitCode.SUCCESS
    assert reader.calls == [3_600]
    assert result.envelope["window_seconds"] == 3_600
    assert result.envelope["run_count"] == 1
    assert result.envelope["runs"] == [reader.reports(window_seconds=3_600)[0].to_dict()]
    assert unknown.exit_code is ExitCode.FAILURE
    unknown_error = unknown.envelope["error"]
    assert isinstance(unknown_error, dict)
    assert unknown_error["code"] == "cron_unknown_action"


def test_cron_rejects_non_string_actions_without_calling_the_reader() -> None:
    reader = RecordingCronReader()

    result = show_cron(reader=reader, action=SpoofedCronAction())  # type: ignore[arg-type]

    assert reader.calls == []
    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope == {
        "command": "cron",
        "error": {
            "code": "cron_unknown_action",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }


def test_cron_redacts_reader_failures() -> None:
    class FailingCronReader:
        def reports(self, *, window_seconds: int) -> tuple[RunMetadata, ...]:
            raise RuntimeError("synthetic-content /synthetic/path")

    result = show_cron(reader=FailingCronReader())

    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope["error"] == {
        "code": "cron_operation_failed",
        "message": "operation unavailable; details redacted",
        "redacted": True,
    }
    assert "synthetic" not in result.to_json()
