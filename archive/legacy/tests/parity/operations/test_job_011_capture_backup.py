from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from open_brain_legacy.operations.backup import (
    BackupObject,
    BackupSource,
    BackupSourceObject,
    BackupStore,
    BackupTier,
    get_backup_job,
)
from open_brain_legacy.operations.recovery import DisposableRestoreRoot, restore_backup

FIXED_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class FakeBackupSource(BackupSource):
    def __init__(self, objects: tuple[BackupSourceObject, ...]) -> None:
        self.objects = objects
        self.collect_calls = 0

    def collect(self) -> tuple[BackupSourceObject, ...]:
        self.collect_calls += 1
        return self.objects


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


def test_job_011_capture_profile_filters_and_restores_with_redacted_evidence(
    tmp_path: Path,
) -> None:
    source = FakeBackupSource(
        (
            BackupSourceObject(
                PurePosixPath("capture/inbox/event.json"), b"synthetic-capture", BackupTier.CAPTURE
            ),
            BackupSourceObject(
                PurePosixPath("capture/raw/payload.bin"), b"synthetic-raw", BackupTier.CAPTURE
            ),
            BackupSourceObject(
                PurePosixPath("capture/transient/retry.tmp"),
                b"synthetic-transient",
                BackupTier.CAPTURE,
            ),
            BackupSourceObject(
                PurePosixPath("capture/secrets/provider.env"),
                b"synthetic-secret",
                BackupTier.CAPTURE,
            ),
            BackupSourceObject(
                PurePosixPath("work/notes/outside.md"), b"synthetic-work", BackupTier.WORK
            ),
        )
    )
    original = tuple((item.relative_path, item.payload, item.tier) for item in source.objects)
    store = MemoryBackupStore()
    application = get_backup_job("JOB-011")

    receipt = application.run(source=source, store=store, created_at=FIXED_TIME)

    assert source.collect_calls == 1
    assert (
        tuple((item.relative_path, item.payload, item.tier) for item in source.objects) == original
    )
    assert application.profile.name == "capture"
    assert application.profile.included_tiers == frozenset({BackupTier.CAPTURE})
    assert application.profile.excluded_prefixes == (
        PurePosixPath("capture/secrets"),
        PurePosixPath("capture/transient"),
    )
    assert {entry.relative_path for entry in receipt.entries} == {
        PurePosixPath("capture/inbox/event.json"),
        PurePosixPath("capture/raw/payload.bin"),
    }
    manifest = json.loads(store.manifests[receipt.backup_id])
    assert manifest["retention"] == application.profile.retention.to_dict()

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    evidence = restore_backup(
        receipt,
        store=store,
        target=DisposableRestoreRoot.create(restore_root),
    )

    assert (restore_root / "capture/inbox/event.json").read_bytes() == b"synthetic-capture"
    assert (restore_root / "capture/raw/payload.bin").read_bytes() == b"synthetic-raw"
    assert not (restore_root / "capture/transient/retry.tmp").exists()
    assert not (restore_root / "capture/secrets/provider.env").exists()
    assert not (restore_root / "work/notes/outside.md").exists()
    assert evidence.to_redacted_dict() == {
        "backup_id": receipt.backup_id,
        "checksums_verified": 3,
        "generation": None,
        "manifest_digest_sha256": receipt.manifest_digest_sha256,
        "object_count": 2,
        "profile": "capture",
        "restored": True,
    }
    assert "synthetic-capture" not in str(evidence.to_redacted_dict())
    assert (
        tuple((item.relative_path, item.payload, item.tier) for item in source.objects) == original
    )
