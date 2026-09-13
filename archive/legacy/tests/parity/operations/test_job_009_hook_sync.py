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
    ScheduledEffect,
    WriterJobInvocation,
    get_writer_job_spec,
    run_writer_job,
)


class MemoryJournal(ReplayJournal):
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], JobRunResult] = {}

    def completed(self, job_id: str, replay_key: str) -> JobRunResult | None:
        return self.runs.get((job_id, replay_key))

    def begin(self, job_id: str, replay_key: str, request_digest_sha256: str) -> None:
        return None

    def complete(self, result: JobRunResult) -> None:
        self.runs[(result.job_id, result.replay_key)] = result


class MemoryEffectCapability(EffectCapability):
    effect = ScheduledEffect.HOOK_SYNC_PLAN
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


class RecordingHookPlan:
    def __init__(self) -> None:
        self.invocations: list[WriterJobInvocation] = []

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        self.invocations.append(invocation)
        return PreparedEffect(
            effect=ScheduledEffect.HOOK_SYNC_PLAN,
            records=(
                EffectRecord("hook_backup", "a" * 64),
                EffectRecord("hook_replace", "b" * 64),
                EffectRecord("hook_prune", "c" * 64),
            ),
        )


def test_job_009_plans_backup_replace_prune_once_and_stays_manual(tmp_path: Path) -> None:
    journal = MemoryJournal()
    application = RecordingHookPlan()
    capability = MemoryEffectCapability(tmp_path)

    first = run_writer_job(
        job_id="JOB-009",
        root=tmp_path,
        replay_key="hooks-fixture-v1",
        journal=journal,
        application=application,
        effect_capability=capability,
    )
    replay = run_writer_job(
        job_id="JOB-009",
        root=tmp_path,
        replay_key="hooks-fixture-v1",
        journal=journal,
        application=application,
        effect_capability=capability,
    )

    spec = get_writer_job_spec("JOB-009")
    assert spec.command == get_job("JOB-009").command
    assert get_job("JOB-009").state is JobState.MANUAL
    assert spec.lock_scope is LockScope.NONE
    assert application.invocations[0].dry_run is True
    assert application.invocations[0].planned_actions == ("backup", "replace", "prune")
    assert first.disposition is JobRunDisposition.APPLIED
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert len(application.invocations) == 1
