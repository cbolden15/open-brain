from __future__ import annotations

from datetime import UTC, datetime

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.ledger import slim
from open_brain_legacy.ledger.slim import LedgerSourceView, PreparedSlim, SlimResult


class _RecordingSlimService:
    def __init__(self) -> None:
        self.calls = 0

    def prepare(self, *, source_view: object, row_identity: object, now: datetime) -> SlimResult:
        self.calls += 1
        return SlimResult(prepared=None, error=None)


def test_slim_dry_run_does_not_invoke_archive_writes() -> None:
    service = _RecordingSlimService()

    result = slim(
        service=service,
        source_view=object(),
        row_identity=object(),
        now=datetime(2026, 8, 14, tzinfo=UTC),
        dry_run=True,
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "command": "ledger.slim",
        "dry_run": True,
        "status": "dry_run",
    }
    assert service.calls == 0


def test_slim_delegates_replay_and_returns_metadata_only() -> None:
    created_at = datetime(2026, 8, 1, tzinfo=UTC)
    successor = LedgerSourceView.create(
        source_id="synthetic-source",
        created_at=created_at,
        content=b"SYNTHETIC_PRIVATE_CANARY",
        transcript=None,
    )
    prepared = PreparedSlim(
        source_id="synthetic-source",
        original_version_id="source_view_original",
        archive_digest_sha256="a" * 64,
        successor=successor,
        successor_digest_sha256="b" * 64,
    )

    class _ReplaySlimService:
        def prepare(
            self, *, source_view: object, row_identity: object, now: datetime
        ) -> SlimResult:
            return SlimResult(prepared=prepared, error=None)

    service = _ReplaySlimService()
    source_view = object()
    row_identity = object()
    now = datetime(2026, 8, 14, tzinfo=UTC)

    first = slim(
        service=service,
        source_view=source_view,
        row_identity=row_identity,
        now=now,
        dry_run=False,
    )
    second = slim(
        service=service,
        source_view=source_view,
        row_identity=row_identity,
        now=now,
        dry_run=False,
    )

    assert first.exit_code is ExitCode.SUCCESS
    assert first.envelope == {"command": "ledger.slim", "status": "slimmed"}
    assert second.envelope == first.envelope
    assert "SYNTHETIC_PRIVATE_CANARY" not in first.to_json()
    assert "synthetic-source" not in first.to_json()
