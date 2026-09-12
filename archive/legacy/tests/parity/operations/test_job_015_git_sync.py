from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
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
    WriterJobError,
    WriterJobInvocation,
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


class GitEffectCapability(EffectCapability):
    effect = ScheduledEffect.LOCAL_GIT_SYNC
    dry_run = False

    def __init__(self, root: Path, *, local_only: bool = True) -> None:
        self.root = root
        self.local_only = local_only
        self.apply_calls = 0
        self.receipts: dict[tuple[str, str], EffectReceipt] = {}
        self.effects: dict[str, PreparedEffect] = {}

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        return self.receipts.get((job_id, replay_key))

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        receipt = EffectReceipt.from_command(command)
        self.receipts[(command.job_id, command.replay_key)] = receipt
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self.apply_calls += 1
        if not self.local_only:
            (self.root / "remote-operation-attempted").write_text("synthetic")
        self.effects[receipt.effect_digest_sha256] = command.prepared

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        return self.effects.get(receipt.effect_digest_sha256)


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class PersonalGitSync:
    def __init__(self, *, attempts_effect: bool = False) -> None:
        self.attempts_effect = attempts_effect
        self.invocations: list[WriterJobInvocation] = []

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        self.invocations.append(invocation)
        records = (
            (EffectRecord("git_mutation", "a" * 64),) if self.attempts_effect else ()
        )
        return PreparedEffect(effect=ScheduledEffect.LOCAL_GIT_SYNC, records=records)


def test_job_015_locks_and_noops_for_local_only_personal_state(tmp_path: Path) -> None:
    journal = MemoryJournal()
    lease = RecordingLease()
    capability = GitEffectCapability(tmp_path)
    application = PersonalGitSync()

    first = run_writer_job(
        job_id="JOB-015",
        root=tmp_path,
        replay_key="git-sync-personal-fixture",
        personal_local_only=True,
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )
    replay = run_writer_job(
        job_id="JOB-015",
        root=tmp_path,
        replay_key="git-sync-personal-fixture",
        personal_local_only=True,
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )

    assert get_job("JOB-015").command == ("open-brain", "ops", "git-sync", "--json")
    assert first.disposition is JobRunDisposition.NOOP
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert application.invocations[0].personal_local_only is True
    assert application.invocations[0].local_only is True
    assert capability.apply_calls == 1
    assert lease.scopes == [LockScope.SHARED_WRITER, LockScope.SHARED_WRITER]


def test_job_015_rejects_nonlocal_capability_before_io(tmp_path: Path) -> None:
    capability = GitEffectCapability(tmp_path, local_only=False)
    application = PersonalGitSync()

    with pytest.raises(WriterJobError, match="local-only"):
        run_writer_job(
            job_id="JOB-015",
            root=tmp_path,
            replay_key="git-sync-remote-attempt",
            personal_local_only=True,
            journal=MemoryJournal(),
            application=application,
            effect_capability=capability,
            lease=RecordingLease(),
        )

    assert application.invocations == []
    assert capability.apply_calls == 0
    assert not (tmp_path / "remote-operation-attempted").exists()


def test_job_015_permits_local_only_personal_commit_effect(tmp_path: Path) -> None:
    capability = GitEffectCapability(tmp_path)

    result = run_writer_job(
        job_id="JOB-015",
        root=tmp_path,
        replay_key="git-sync-personal-commit",
        personal_local_only=True,
        journal=MemoryJournal(),
        application=PersonalGitSync(attempts_effect=True),
        effect_capability=capability,
        lease=RecordingLease(),
    )

    assert result.disposition is JobRunDisposition.APPLIED
    assert capability.apply_calls == 1
    assert not (tmp_path / "remote-operation-attempted").exists()
