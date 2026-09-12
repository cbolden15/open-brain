from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from open_brain_legacy.operations.backup import (
    BackupObject,
    BackupStore,
    SQLiteSnapshotSource,
    get_backup_job,
)
from open_brain_legacy.operations.recovery import DisposableRestoreRoot, restore_backup

FIXED_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class FakeSQLiteSnapshotSource(SQLiteSnapshotSource):
    def __init__(self, live_bytes: bytes, snapshot_bytes: bytes) -> None:
        self.live_bytes = live_bytes
        self.snapshot_bytes = snapshot_bytes
        self.snapshot_calls = 0

    def snapshot_via_api(self) -> bytes:
        self.snapshot_calls += 1
        return self.snapshot_bytes


class MemoryBackupStore(BackupStore):
    def __init__(self) -> None:
        self.manifests: dict[str, bytes] = {}
        self.objects: dict[tuple[str, PurePosixPath], bytes] = {}

    def stage_objects(
        self,
        *,
        backup_id: str,
        objects: tuple[BackupObject, ...],
    ) -> None:
        for item in objects:
            self.objects[(backup_id, item.relative_path)] = item.payload

    def publish_manifest(self, *, backup_id: str, manifest: bytes) -> None:
        self.manifests[backup_id] = manifest

    def read_manifest(self, *, backup_id: str) -> bytes:
        return self.manifests[backup_id]

    def read_object(self, *, backup_id: str, relative_path: PurePosixPath) -> bytes:
        return self.objects[(backup_id, relative_path)]


def test_job_004_uses_consistent_sqlite_api_and_verifies_disposable_restore(
    tmp_path: Path,
) -> None:
    live = b"synthetic-live-database-with-changing-sidecars"
    consistent = b"SQLite format 3\x00synthetic-consistent-snapshot"
    source = FakeSQLiteSnapshotSource(live, consistent)
    store = MemoryBackupStore()
    application = get_backup_job("JOB-004")

    receipt = application.run_sqlite(
        source=source,
        store=store,
        created_at=FIXED_TIME,
    )

    assert source.snapshot_calls == 1
    assert source.live_bytes == live
    assert receipt.object_count == 1
    assert len(receipt.manifest_digest_sha256) == 64
    assert store.objects[(receipt.backup_id, PurePosixPath("sqlite/snapshot.db"))] == consistent

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    evidence = restore_backup(
        receipt,
        store=store,
        target=DisposableRestoreRoot.create(restore_root),
    )

    assert (restore_root / "sqlite/snapshot.db").read_bytes() == consistent
    assert evidence.checksums_verified == 2
    assert evidence.to_redacted_dict() == {
        "backup_id": receipt.backup_id,
        "checksums_verified": 2,
        "generation": None,
        "manifest_digest_sha256": receipt.manifest_digest_sha256,
        "object_count": 1,
        "profile": "local-sqlite",
        "restored": True,
    }
    assert source.live_bytes == live
