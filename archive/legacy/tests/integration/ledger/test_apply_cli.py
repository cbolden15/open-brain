from __future__ import annotations

from typing import cast

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.ledger import apply
from open_brain_legacy.ledger.service import ApplyResult, PreparedLedgerApply
from open_brain_legacy.ledger.stage import LedgerStage


class _RecordingApplier:
    def __init__(self) -> None:
        self.calls = 0

    def apply(self, *, stage: LedgerStage, prepared: PreparedLedgerApply) -> ApplyResult:
        self.calls += 1
        return ApplyResult(status="applied")


def test_apply_dry_run_does_not_invoke_mutating_service() -> None:
    service = _RecordingApplier()

    result = apply(
        service=service,
        stage=cast(LedgerStage, object()),
        prepared=cast(PreparedLedgerApply, object()),
        dry_run=True,
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "command": "ledger.apply",
        "dry_run": True,
        "status": "dry_run",
    }
    assert service.calls == 0
