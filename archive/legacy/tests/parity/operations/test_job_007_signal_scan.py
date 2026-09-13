from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
    effect = ScheduledEffect.APPEND_ONLY_SIGNALS
    local_only = True
    dry_run = False

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


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class FailOnceSignalScan:
    def __init__(self) -> None:
        self.invocations: list[WriterJobInvocation] = []

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        self.invocations.append(invocation)
        if len(self.invocations) == 1:
            raise RuntimeError("synthetic signal scan failure")
        return PreparedEffect(
            effect=ScheduledEffect.APPEND_ONLY_SIGNALS,
            records=(
                EffectRecord("signal_fixture_1", "a" * 64),
                EffectRecord("signal_fixture_2", "b" * 64),
            ),
        )


def test_job_007_retries_one_cutoff_without_crossing_into_curation(tmp_path: Path) -> None:
    journal = MemoryJournal()
    lease = RecordingLease()
    capability = MemoryEffectCapability(tmp_path)
    application = FailOnceSignalScan()
    cutoff = datetime(2026, 8, 13, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="synthetic signal scan failure"):
        run_writer_job(
            job_id="JOB-007",
            root=tmp_path,
            replay_key="signals-2026-08-13",
            cutoff=cutoff,
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    applied = run_writer_job(
        job_id="JOB-007",
        root=tmp_path,
        replay_key="signals-2026-08-13",
        cutoff=cutoff,
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )
    replay = run_writer_job(
        job_id="JOB-007",
        root=tmp_path,
        replay_key="signals-2026-08-13",
        cutoff=cutoff,
        journal=journal,
        application=application,
        effect_capability=capability,
        lease=lease,
    )

    assert get_writer_job_spec("JOB-007").command == get_job("JOB-007").command
    assert applied.disposition is JobRunDisposition.APPLIED
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert len(application.invocations) == 2
    assert application.invocations[-1].cutoff == cutoff
    assert application.invocations[-1].effect is ScheduledEffect.APPEND_ONLY_SIGNALS
    assert lease.scopes == [LockScope.INGRESS, LockScope.INGRESS, LockScope.INGRESS]
