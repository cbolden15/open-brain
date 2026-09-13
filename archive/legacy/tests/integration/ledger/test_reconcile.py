from __future__ import annotations

from typing import cast

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.ledger import reconcile
from open_brain_legacy.ledger.reconcile import ReconcileDisposition, ReconcileResult
from open_brain_legacy.ledger.service import PreparedLedgerApply


class _RecordingReconciler:
    def __init__(self, result: ReconcileResult) -> None:
        self.calls = 0
        self._result = result

    def reconcile(self, *, prepared: PreparedLedgerApply) -> ReconcileResult:
        self.calls += 1
        return self._result


def test_reconcile_dry_run_does_not_invoke_mutating_service() -> None:
    service = _RecordingReconciler(
        ReconcileResult(ReconcileDisposition.ROLLED_FORWARD, "a" * 64)
    )

    result = reconcile(service=service, prepared=cast(PreparedLedgerApply, object()), dry_run=True)

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "command": "ledger.reconcile",
        "dry_run": True,
        "status": "dry_run",
    }
    assert service.calls == 0


def test_reconcile_reports_explicit_conflict_without_exposing_prepared_content() -> None:
    service = _RecordingReconciler(
        ReconcileResult(ReconcileDisposition.CONFLICT, "b" * 64)
    )

    result = reconcile(service=service, prepared=cast(PreparedLedgerApply, object()), dry_run=False)

    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope == {
        "command": "ledger.reconcile",
        "disposition": "conflict",
        "status": "conflict",
    }
    assert service.calls == 1
