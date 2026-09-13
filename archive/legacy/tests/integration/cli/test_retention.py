from __future__ import annotations

from dataclasses import dataclass, field

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.operations import run_retention
from open_brain_legacy.production.retention import RetentionReport


@dataclass
class RecordingRetentionService:
    calls: list[bool] = field(default_factory=list)

    def retain(self, *, dry_run: bool) -> RetentionReport:
        self.calls.append(dry_run)
        return RetentionReport(
            candidate_count=3,
            manifest_digest="a" * 64,
            protected_count=2,
            removed_count=0,
            replayed=False,
        )


def test_retention_defaults_to_dry_run_and_serializes_only_metadata() -> None:
    service = RecordingRetentionService()

    result = run_retention(service=service)

    assert service.calls == [True]
    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "candidate_count": 3,
        "command": "retention",
        "dry_run": True,
        "manifest_digest": "a" * 64,
        "protected_count": 2,
        "removed_count": 0,
        "replayed": False,
        "status": "planned",
    }


def test_retention_report_is_owned_by_the_production_service() -> None:
    assert RetentionReport.__module__ == "open_brain_legacy.production.retention"


def test_retention_redacts_service_failures() -> None:
    class FailingRetentionService:
        def retain(self, *, dry_run: bool) -> RetentionReport:
            raise RuntimeError("synthetic-content /synthetic/path")

    result = run_retention(service=FailingRetentionService())

    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope["error"] == {
        "code": "retention_operation_failed",
        "message": "operation unavailable; details redacted",
        "redacted": True,
    }
    assert "synthetic" not in result.to_json()
