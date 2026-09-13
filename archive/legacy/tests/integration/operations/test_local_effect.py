from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from open_brain_engine.engine import LockScope

import open_brain_legacy.operations.local_effect as local_effect
from open_brain_legacy.operations.local_effect import (
    EmptyBatchApplication,
    FilesystemEmptyEffectCapability,
    FilesystemPreparedEffectCapability,
)
from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.writer_jobs import (
    EffectParameter,
    EffectRecord,
    JobRunDisposition,
    PreparedEffect,
    WriterJobError,
    WriterJobInvocation,
    get_writer_job_spec,
    run_writer_job,
)
from tests.unit.storage._factories import FixedClock


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class PreparedApplication:
    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        assert invocation.job_id == "JOB-008"
        return PreparedEffect(
            effect=invocation.effect,
            records=(EffectRecord("lint_report", "a" * 64),),
            parameters=(EffectParameter("finding_count", "0"),),
        )


def test_empty_batch_is_durable_and_replays_without_claiming_an_effect(
    tmp_path: Path,
) -> None:
    spec = get_writer_job_spec("JOB-008")
    capability = FilesystemEmptyEffectCapability(root=tmp_path, spec=spec)
    application = EmptyBatchApplication(spec)

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        first = run_writer_job(
            job_id=spec.job_id,
            root=tmp_path,
            replay_key="job-008-empty-fixture",
            journal=journal,
            application=application,
            effect_capability=capability,
        )
        replay = run_writer_job(
            job_id=spec.job_id,
            root=tmp_path,
            replay_key="job-008-empty-fixture",
            journal=journal,
            application=application,
            effect_capability=capability,
        )

    assert first.disposition is JobRunDisposition.NOOP
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert first.effect_count == replay.effect_count == 0
    assert not tuple(tmp_path.rglob("*.md"))
    assert not tuple(tmp_path.rglob("*.txt"))


def test_empty_batch_rejects_corrupt_applied_pointer(tmp_path: Path) -> None:
    spec = get_writer_job_spec("JOB-010")
    capability = FilesystemEmptyEffectCapability(root=tmp_path, spec=spec)
    application = EmptyBatchApplication(spec)
    lease = RecordingLease()
    replay_key = "job-010-empty-corrupt"

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        run_writer_job(
            job_id=spec.job_id,
            root=tmp_path,
            replay_key=replay_key,
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )
        _receipt, pointer = local_effect._paths(spec.job_id, replay_key)
        (tmp_path / pointer).write_text('{"schema_version":1,"broken":true}')

        with pytest.raises(WriterJobError, match="pointer"):
            run_writer_job(
                job_id=spec.job_id,
                root=tmp_path,
                replay_key=replay_key,
                journal=journal,
                application=application,
                effect_capability=capability,
                lease=lease,
            )

    assert lease.scopes == [LockScope.SHARED_WRITER, LockScope.SHARED_WRITER]


def test_prepared_effect_persists_exact_non_empty_metadata_and_replays(
    tmp_path: Path,
) -> None:
    spec = get_writer_job_spec("JOB-008")
    capability = FilesystemPreparedEffectCapability(root=tmp_path, spec=spec)
    expected = PreparedEffect(
        effect=spec.effect,
        records=(EffectRecord("lint_report", "a" * 64),),
        parameters=(EffectParameter("finding_count", "0"),),
    )

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        first = run_writer_job(
            job_id=spec.job_id,
            root=tmp_path,
            replay_key="job-008-prepared-fixture",
            journal=journal,
            application=PreparedApplication(),
            effect_capability=capability,
        )
        replay = run_writer_job(
            job_id=spec.job_id,
            root=tmp_path,
            replay_key="job-008-prepared-fixture",
            journal=journal,
            application=PreparedApplication(),
            effect_capability=capability,
        )

    receipt = capability.recover(spec.job_id, "job-008-prepared-fixture")
    assert receipt is not None
    assert capability.read(receipt) == expected
    assert first.effect_count == 1
    assert replay.disposition is JobRunDisposition.REPLAYED
