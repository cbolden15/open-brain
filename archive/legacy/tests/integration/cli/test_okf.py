from __future__ import annotations

from dataclasses import dataclass, field

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.operations import OkfAction, OkfReport, run_okf


@dataclass
class RecordingOkfService:
    calls: list[OkfAction] = field(default_factory=list)

    def run(self, *, action: OkfAction) -> OkfReport:
        self.calls.append(action)
        return OkfReport(action=action, record_count=2, replayed=False, schema_version=1)


def test_okf_uses_the_injected_service_and_rejects_unknown_actions() -> None:
    service = RecordingOkfService()

    checked = run_okf(service=service, action="check")
    rejected = run_okf(service=service, action="delete")

    assert service.calls == [OkfAction.CHECK]
    assert checked.exit_code is ExitCode.SUCCESS
    assert checked.envelope == {
        "action": "check",
        "command": "okf",
        "record_count": 2,
        "replayed": False,
        "schema_version": 1,
        "status": "checked",
    }
    assert rejected.exit_code is ExitCode.FAILURE
    rejected_error = rejected.envelope["error"]
    assert isinstance(rejected_error, dict)
    assert rejected_error["code"] == "okf_unknown_action"


def test_okf_redacts_service_failures() -> None:
    class FailingOkfService:
        def run(self, *, action: OkfAction) -> OkfReport:
            raise RuntimeError("synthetic-content /synthetic/path")

    result = run_okf(service=FailingOkfService(), action=OkfAction.CHECK)

    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope["error"] == {
        "code": "okf_operation_failed",
        "message": "operation unavailable; details redacted",
        "redacted": True,
    }
    assert "synthetic" not in result.to_json()
