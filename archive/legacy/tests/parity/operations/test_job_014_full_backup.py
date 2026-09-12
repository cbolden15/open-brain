from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from open_brain_legacy.operations.backup import (
    BackupError,
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

    def collect(self) -> tuple[BackupSourceObject, ...]:
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


class DroppingBackupStore(MemoryBackupStore):
    def stage_objects(
        self,
        *,
        backup_id: str,
        objects: tuple[BackupObject, ...],
    ) -> None:
        if objects:
            first = objects[0]
            self.objects[(backup_id, first.relative_path)] = first.payload


def test_job_014_does_not_publish_a_partial_backup() -> None:
    source = FakeBackupSource(
        (
            BackupSourceObject(
                PurePosixPath("work/pages/one.md"), b"one", BackupTier.WORK
            ),
            BackupSourceObject(
                PurePosixPath("work/pages/two.md"), b"two", BackupTier.WORK
            ),
        )
    )
    store = DroppingBackupStore()

    with pytest.raises(BackupError, match="store verification failed"):
        get_backup_job("JOB-014").run(
            source=source,
            store=store,
            created_at=FIXED_TIME,
        )

    assert store.manifests == {}


def test_job_014_full_profile_verifies_manifest_and_files_before_disposable_restore(
    tmp_path: Path,
) -> None:
    source = FakeBackupSource(
        (
            BackupSourceObject(
                PurePosixPath("capture/inbox/event.json"), b"synthetic-capture", BackupTier.CAPTURE
            ),
            BackupSourceObject(
                PurePosixPath("work/pages/decision.md"), b"synthetic-work", BackupTier.WORK
            ),
            BackupSourceObject(
                PurePosixPath("saved-content/articles/source.md"),
                b"synthetic-saved-content",
                BackupTier.SAVED_CONTENT,
            ),
            BackupSourceObject(
                PurePosixPath("personal/registry/people.json"), b"{}", BackupTier.PERSONAL
            ),
            BackupSourceObject(
                PurePosixPath("runtime/state/cursor.json"),
                b"synthetic-runtime",
                BackupTier.RUNTIME_STATE,
            ),
            BackupSourceObject(
                PurePosixPath("cache/search/index.bin"), b"synthetic-cache", BackupTier.WORK
            ),
            BackupSourceObject(
                PurePosixPath("secrets/provider.env"), b"synthetic-secret", BackupTier.PERSONAL
            ),
            BackupSourceObject(
                PurePosixPath("capture/secrets/provider.env"),
                b"synthetic-capture-secret",
                BackupTier.CAPTURE,
            ),
            BackupSourceObject(
                PurePosixPath("personal/secrets/calendar.env"),
                b"synthetic-personal-secret",
                BackupTier.PERSONAL,
            ),
            BackupSourceObject(
                PurePosixPath("tmp/incomplete.bin"), b"synthetic-temp", BackupTier.CAPTURE
            ),
            BackupSourceObject(
                PurePosixPath("runtime/locks/writer.lock"),
                b"synthetic-lock",
                BackupTier.RUNTIME_STATE,
            ),
            BackupSourceObject(
                PurePosixPath("sqlite/live/database.db-wal"),
                b"synthetic-sidecar",
                BackupTier.LOCAL_SQLITE,
            ),
        )
    )
    original = tuple((item.relative_path, item.payload, item.tier) for item in source.objects)
    store = MemoryBackupStore()
    application = get_backup_job("JOB-014")

    receipt = application.run(source=source, store=store, created_at=FIXED_TIME)

    assert application.profile.name == "full"
    assert application.profile.included_tiers == frozenset(
        {
            BackupTier.CAPTURE,
            BackupTier.PERSONAL,
            BackupTier.RUNTIME_STATE,
            BackupTier.SAVED_CONTENT,
            BackupTier.WORK,
        }
    )
    assert application.profile.excluded_prefixes == (
        PurePosixPath("cache"),
        PurePosixPath("capture/secrets"),
        PurePosixPath("capture/transient"),
        PurePosixPath("personal/cache"),
        PurePosixPath("personal/secrets"),
        PurePosixPath("runtime/locks"),
        PurePosixPath("secrets"),
        PurePosixPath("tmp"),
    )
    assert {entry.relative_path for entry in receipt.entries} == {
        PurePosixPath("capture/inbox/event.json"),
        PurePosixPath("personal/registry/people.json"),
        PurePosixPath("runtime/state/cursor.json"),
        PurePosixPath("saved-content/articles/source.md"),
        PurePosixPath("work/pages/decision.md"),
    }
    manifest = json.loads(store.manifests[receipt.backup_id])
    assert manifest["retention"] == application.profile.retention.to_dict()
    assert {entry["tier"] for entry in manifest["entries"]} == {
        "capture",
        "personal",
        "runtime-state",
        "saved-content",
        "work",
    }

    manifest_bytes = store.manifests[receipt.backup_id]
    store.manifests[receipt.backup_id] = manifest_bytes + b"\n"
    bad_manifest_root = tmp_path / "bad-manifest"
    bad_manifest_root.mkdir()
    with pytest.raises(BackupError, match="manifest verification failed"):
        restore_backup(
            receipt,
            store=store,
            target=DisposableRestoreRoot.create(bad_manifest_root),
        )
    assert not tuple(bad_manifest_root.iterdir())
    store.manifests[receipt.backup_id] = manifest_bytes

    object_key = (receipt.backup_id, PurePosixPath("work/pages/decision.md"))
    object_bytes = store.objects[object_key]
    store.objects[object_key] = object_bytes + b"tampered"
    bad_object_root = tmp_path / "bad-object"
    bad_object_root.mkdir()
    with pytest.raises(BackupError, match="object verification failed"):
        restore_backup(
            receipt,
            store=store,
            target=DisposableRestoreRoot.create(bad_object_root),
        )
    assert not tuple(bad_object_root.iterdir())
    store.objects[object_key] = object_bytes

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    evidence = restore_backup(
        receipt,
        store=store,
        target=DisposableRestoreRoot.create(restore_root),
    )

    assert evidence.to_redacted_dict() == {
        "backup_id": receipt.backup_id,
        "checksums_verified": 6,
        "generation": None,
        "manifest_digest_sha256": receipt.manifest_digest_sha256,
        "object_count": 5,
        "profile": "full",
        "restored": True,
    }
    assert "synthetic-work" not in str(evidence.to_redacted_dict())
    assert (
        tuple((item.relative_path, item.payload, item.tier) for item in source.objects) == original
    )
