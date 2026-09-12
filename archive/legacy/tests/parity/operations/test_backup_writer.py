from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.engine import LockScope
from open_brain_engine.storage.filesystem import DurabilityError, atomic_write_new
from open_brain_engine.storage.writer_record import CanonicalWriterRecord

from open_brain_legacy.operations.backup import BackupError, BackupObject
from open_brain_legacy.operations.backup_writer import (
    BackupEffectCapability,
    BackupWriterApplication,
    FilesystemBackupSource,
    FilesystemBackupStore,
)
from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.writer_jobs import (
    EffectCommand,
    JobRunDisposition,
    JobRunResult,
    ReplayJournal,
    WriterJobError,
    WriterJobInvocation,
    get_writer_job_spec,
    run_writer_job,
)
from tests.unit.storage._factories import FixedClock

_CREATED_AT = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class FailFirstCompletionJournal(ReplayJournal):
    def __init__(self, journal: ReplayJournal) -> None:
        self._journal = journal
        self.fail_completion = True

    def completed(self, job_id: str, replay_key: str) -> JobRunResult | None:
        return self._journal.completed(job_id, replay_key)

    def begin(self, job_id: str, replay_key: str, request_digest_sha256: str) -> None:
        self._journal.begin(job_id, replay_key, request_digest_sha256)

    def complete(self, result: JobRunResult) -> None:
        if self.fail_completion:
            self.fail_completion = False
            raise RuntimeError("synthetic crash before journal completion")
        self._journal.complete(result)


class FailFirstPartialStore(FilesystemBackupStore):
    def __init__(self, *, root: Path) -> None:
        super().__init__(root=root)
        self.fail_once = True

    def stage_objects(
        self,
        *,
        backup_id: str,
        objects: tuple[BackupObject, ...],
    ) -> None:
        if self.fail_once:
            self.fail_once = False
            super().stage_objects(backup_id=backup_id, objects=objects[:1])
            raise BackupError("synthetic partial object crash")
        super().stage_objects(backup_id=backup_id, objects=objects)


def _parts(
    tmp_path: Path,
    job_id: str,
    *,
    store_factory: Callable[[Path], FilesystemBackupStore] | None = None,
) -> tuple[
    Path,
    BackupWriterApplication,
    BackupEffectCapability,
]:
    roots = {
        "work_root": tmp_path / "work",
        "personal_root": tmp_path / "personal",
        "capture_root": tmp_path / "capture",
        "saved_content_root": tmp_path / "saved-content",
        "state_root": tmp_path / "state",
    }
    backup_root = tmp_path / "backup"
    for root in (*roots.values(), backup_root):
        root.mkdir()
    (roots["work_root"] / "page.md").write_bytes(b"work")
    (roots["personal_root"] / "person.json").write_bytes(b"personal")
    (roots["capture_root"] / "event.json").write_bytes(b"capture")
    (roots["saved_content_root"] / "article.md").write_bytes(b"saved")
    (roots["state_root"] / "cursor.json").write_bytes(b"state")
    source = FilesystemBackupSource(**roots)
    store = (
        FilesystemBackupStore(root=backup_root)
        if store_factory is None
        else store_factory(backup_root)
    )
    writer_record = CanonicalWriterRecord.create(
        identity_id="synthetic-writer",
        generation=1,
        recorded_at=_CREATED_AT,
    )
    return (
        backup_root,
        BackupWriterApplication(job_id=job_id, created_at=_CREATED_AT),
        BackupEffectCapability(
            root=backup_root,
            source=source,
            store=store,
            writer_record=writer_record,
            writer_record_reader=lambda: writer_record,
        ),
    )


@pytest.mark.parametrize(
    ("job_id", "expected_paths"),
    (
        ("JOB-011", {"capture/event.json"}),
        (
            "JOB-014",
            {
                "capture/event.json",
                "personal/person.json",
                "runtime/cursor.json",
                "saved-content/article.md",
                "work/page.md",
            },
        ),
        ("JOB-023", {"personal/person.json"}),
        ("JOB-025", {"runtime/cursor.json"}),
    ),
)
def test_backup_writer_jobs_apply_once_and_replay(
    tmp_path: Path,
    job_id: str,
    expected_paths: set[str],
) -> None:
    backup_root, application, capability = _parts(tmp_path, job_id)
    lease = RecordingLease()
    with SqliteReplayJournal(root=backup_root, clock=FixedClock()) as journal:
        first = run_writer_job(
            job_id=job_id,
            root=backup_root,
            replay_key=f"{job_id.lower()}-2026-08-16",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )
        replay = run_writer_job(
            job_id=job_id,
            root=backup_root,
            replay_key=f"{job_id.lower()}-2026-08-16",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    receipt = capability.recover(job_id, f"{job_id.lower()}-2026-08-16")
    assert receipt is not None
    applied = capability.applied_pointer(receipt)
    manifest = json.loads(
        (backup_root / "backups" / applied.backup_id / "manifest.json").read_bytes()
    )
    assert first.disposition is JobRunDisposition.APPLIED
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert {entry["relative_path"] for entry in manifest["entries"]} == expected_paths
    assert lease.scopes == [LockScope.BACKUP_PROFILE, LockScope.BACKUP_PROFILE]


def test_backup_writer_recovers_after_crash_before_journal_completion(
    tmp_path: Path,
) -> None:
    backup_root, application, capability = _parts(tmp_path, "JOB-011")
    lease = RecordingLease()
    with SqliteReplayJournal(root=backup_root, clock=FixedClock()) as durable:
        journal = FailFirstCompletionJournal(durable)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            run_writer_job(
                job_id="JOB-011",
                root=backup_root,
                replay_key="capture-journal-crash",
                journal=journal,
                application=application,
                effect_capability=capability,
                lease=lease,
            )
        replay = run_writer_job(
            job_id="JOB-011",
            root=backup_root,
            replay_key="capture-journal-crash",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=lease,
        )

    assert replay.disposition is JobRunDisposition.REPLAYED


def test_backup_writer_recovery_uses_receipt_created_at_after_pointer_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_root, application, capability = _parts(tmp_path, "JOB-014")
    lease = RecordingLease()
    failed = False

    def fail_first_pointer_write(
        *, root: Path, relative: str | PurePosixPath, data: bytes
    ) -> object:
        nonlocal failed
        if str(relative).endswith(".applied.json") and not failed:
            failed = True
            raise DurabilityError("synthetic backup pointer crash")
        return atomic_write_new(root=root, relative=relative, data=data)

    monkeypatch.setattr(
        'open_brain_legacy.operations.backup_writer.atomic_write_new',
        fail_first_pointer_write,
    )
    with SqliteReplayJournal(root=backup_root, clock=FixedClock()) as journal:
        with pytest.raises(DurabilityError, match="synthetic backup pointer crash"):
            run_writer_job(
                job_id="JOB-014",
                root=backup_root,
                replay_key="full-pointer-crash",
                journal=journal,
                application=application,
                effect_capability=capability,
                lease=lease,
            )
        (tmp_path / "work" / "page.md").write_bytes(b"changed after reservation")
        replay = run_writer_job(
            job_id="JOB-014",
            root=backup_root,
            replay_key="full-pointer-crash",
            journal=journal,
            application=BackupWriterApplication(
                job_id="JOB-014",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            ),
            effect_capability=capability,
            lease=lease,
        )

    receipt = capability.recover("JOB-014", "full-pointer-crash")
    assert receipt is not None
    pointer = capability.applied_pointer(receipt)
    manifest = json.loads(
        (backup_root / "backups" / pointer.backup_id / "manifest.json").read_bytes()
    )
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert manifest["created_at"] == "2026-08-16T12:00:00Z"
    assert (
        backup_root / "backups" / pointer.backup_id / "objects" / "work" / "page.md"
    ).read_bytes() == b"work"


def test_backup_writer_recovers_partial_object_stage_from_atomic_source_snapshot(
    tmp_path: Path,
) -> None:
    backup_root, application, capability = _parts(
        tmp_path,
        "JOB-014",
        store_factory=lambda root: FailFirstPartialStore(root=root),
    )
    lease = RecordingLease()
    with SqliteReplayJournal(root=backup_root, clock=FixedClock()) as journal:
        with pytest.raises(BackupError, match="partial object crash"):
            run_writer_job(
                job_id="JOB-014",
                root=backup_root,
                replay_key="full-partial-stage-crash",
                journal=journal,
                application=application,
                effect_capability=capability,
                lease=lease,
            )
        (tmp_path / "work" / "page.md").write_bytes(b"changed after partial stage")
        replay = run_writer_job(
            job_id="JOB-014",
            root=backup_root,
            replay_key="full-partial-stage-crash",
            journal=journal,
            application=BackupWriterApplication(
                job_id="JOB-014",
                created_at=datetime(2026, 8, 16, 12, 5, tzinfo=UTC),
            ),
            effect_capability=capability,
            lease=lease,
        )

    receipt = capability.recover("JOB-014", "full-partial-stage-crash")
    assert receipt is not None
    pointer = capability.applied_pointer(receipt)
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert (
        backup_root / "backups" / pointer.backup_id / "objects" / "work" / "page.md"
    ).read_bytes() == b"work"
    assert len(tuple((backup_root / "backups").iterdir())) == 1
    assert len(tuple((backup_root / "reservations" / "JOB-014").glob("*.snapshot.json"))) == 1


def test_backup_writer_rejects_changed_canonical_writer_authority(
    tmp_path: Path,
) -> None:
    backup_root, application, capability = _parts(tmp_path, "JOB-011")
    changed = CanonicalWriterRecord.create(
        identity_id="different-writer",
        generation=2,
        recorded_at=datetime(2026, 8, 16, 12, 1, tzinfo=UTC),
    )
    capability._writer_record_reader = lambda: changed

    with (
        SqliteReplayJournal(root=backup_root, clock=FixedClock()) as journal,
        pytest.raises(WriterJobError, match="canonical writer authority changed"),
    ):
        run_writer_job(
            job_id="JOB-011",
            root=backup_root,
            replay_key="authority-changed",
            journal=journal,
            application=application,
            effect_capability=capability,
            lease=RecordingLease(),
        )


def test_backup_writer_rejects_durable_reservation_digest_conflict(
    tmp_path: Path,
) -> None:
    _backup_root, application, capability = _parts(tmp_path, "JOB-011")
    spec = get_writer_job_spec("JOB-011")
    invocation = WriterJobInvocation(
        job_id=spec.job_id,
        command=spec.command,
        replay_key="capture-reservation-conflict",
        effect=spec.effect,
        review_boundary=spec.review_boundary,
        local_only=spec.local_only,
        dry_run=spec.dry_run,
        apply_review_decisions=False,
        approved_records=(),
        approval_bindings=(),
        planned_actions=spec.planned_actions,
        personal_local_only=False,
        cutoff=None,
    )
    prepared = application.prepare(invocation)
    capability.reserve(EffectCommand("JOB-011", invocation.replay_key, "a" * 64, prepared))

    with pytest.raises(WriterJobError, match="reservation conflict"):
        capability.reserve(EffectCommand("JOB-011", invocation.replay_key, "b" * 64, prepared))
