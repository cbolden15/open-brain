from pathlib import Path

from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import JobState
from open_brain_legacy.operations.writer_jobs import (
    EffectCapability,
    EffectCommand,
    EffectReceipt,
    EffectRecord,
    JobRunDisposition,
    JobRunResult,
    PreparedEffect,
    ReplayJournal,
    ReviewBoundary,
    ScheduledEffect,
    WriterJobInvocation,
    get_writer_job_spec,
    run_writer_job,
)


class MemoryJournal(ReplayJournal):
    def __init__(self) -> None:
        self.completed_runs: dict[tuple[str, str], JobRunResult] = {}
        self.begun: list[tuple[str, str, str]] = []

    def completed(self, job_id: str, replay_key: str) -> JobRunResult | None:
        return self.completed_runs.get((job_id, replay_key))

    def begin(self, job_id: str, replay_key: str, request_digest_sha256: str) -> None:
        self.begun.append((job_id, replay_key, request_digest_sha256))

    def complete(self, result: JobRunResult) -> None:
        self.completed_runs[(result.job_id, result.replay_key)] = result


class MemoryEffectCapability(EffectCapability):
    effect = ScheduledEffect.OPERATOR_ARTIFACT
    local_only = True
    dry_run = True

    def __init__(self, root: Path) -> None:
        self.root = root
        self.receipts: dict[tuple[str, str], EffectReceipt] = {}
        self.effects: dict[str, PreparedEffect] = {}

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        return self.receipts.get((job_id, replay_key))

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        receipt = EffectReceipt.from_command(command)
        self.receipts[(command.job_id, command.replay_key)] = receipt
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self.effects[receipt.effect_digest_sha256] = command.prepared

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        return self.effects.get(receipt.effect_digest_sha256)


class RecordingPreparation:
    def __init__(self) -> None:
        self.invocations: list[WriterJobInvocation] = []

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        self.invocations.append(invocation)
        return PreparedEffect(
            effect=ScheduledEffect.OPERATOR_ARTIFACT,
            records=(EffectRecord("close_day_plan", "a" * 64),),
        )


def test_job_006_prepares_once_without_applying_review_decisions(tmp_path: Path) -> None:
    journal = MemoryJournal()
    application = RecordingPreparation()
    capability = MemoryEffectCapability(tmp_path)
    spec = get_writer_job_spec("JOB-006")

    first = run_writer_job(
        job_id="JOB-006",
        root=tmp_path,
        replay_key="close-day-2026-08-13",
        journal=journal,
        application=application,
        effect_capability=capability,
    )
    replay = run_writer_job(
        job_id="JOB-006",
        root=tmp_path,
        replay_key="close-day-2026-08-13",
        journal=journal,
        application=application,
        effect_capability=capability,
    )

    assert spec.command == get_job("JOB-006").command == (
        "open-brain",
        "close-day",
        "prepare",
        "--json",
        "--dry-run",
    )
    assert get_job("JOB-006").state is JobState.MANUAL
    assert spec.lock_scope is LockScope.NONE
    assert spec.review_boundary is ReviewBoundary.PREPARATION_ONLY
    assert first.disposition is JobRunDisposition.APPLIED
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert len(application.invocations) == 1
    assert application.invocations[0].apply_review_decisions is False
    assert application.invocations[0].local_only is True
