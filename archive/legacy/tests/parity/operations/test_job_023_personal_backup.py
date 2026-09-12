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


def test_job_023_personal_profile_handles_empty_registry_without_crossing_tiers(
    tmp_path: Path,
) -> None:
    empty_registry = b'{"people":[]}'
    source = FakeBackupSource(
        (
            BackupSourceObject(
                PurePosixPath("personal/journal/2026-08-14.md"),
                b"synthetic-personal-entry",
                BackupTier.PERSONAL,
            ),
            BackupSourceObject(
                PurePosixPath("personal/registry/people.json"),
                empty_registry,
                BackupTier.PERSONAL,
            ),
            BackupSourceObject(
                PurePosixPath("personal/cache/relationships.bin"),
                b"synthetic-cache",
                BackupTier.PERSONAL,
            ),
            BackupSourceObject(
                PurePosixPath("personal/secrets/calendar.env"),
                b"synthetic-secret",
                BackupTier.PERSONAL,
            ),
            BackupSourceObject(
                PurePosixPath("work/pages/outside.md"), b"synthetic-work", BackupTier.WORK
            ),
        )
    )
    original = tuple((item.relative_path, item.payload, item.tier) for item in source.objects)
    store = MemoryBackupStore()
    application = get_backup_job("JOB-023")

    receipt = application.run(source=source, store=store, created_at=FIXED_TIME)

    assert application.profile.name == "personal"
    assert application.profile.included_tiers == frozenset({BackupTier.PERSONAL})
    assert application.profile.excluded_prefixes == (
        PurePosixPath("personal/cache"),
        PurePosixPath("personal/secrets"),
    )
    assert {entry.relative_path for entry in receipt.entries} == {
        PurePosixPath("personal/journal/2026-08-14.md"),
        PurePosixPath("personal/registry/people.json"),
    }
    manifest = json.loads(store.manifests[receipt.backup_id])
    assert manifest["retention"] == application.profile.retention.to_dict()
    assert {entry["tier"] for entry in manifest["entries"]} == {"personal"}

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    evidence = restore_backup(
        receipt,
        store=store,
        target=DisposableRestoreRoot.create(restore_root),
    )

    assert (restore_root / "personal/registry/people.json").read_bytes() == empty_registry
    assert not (restore_root / "personal/cache/relationships.bin").exists()
    assert not (restore_root / "personal/secrets/calendar.env").exists()
    assert not (restore_root / "work/pages/outside.md").exists()
    assert evidence.to_redacted_dict() == {
        "backup_id": receipt.backup_id,
        "checksums_verified": 3,
        "generation": None,
        "manifest_digest_sha256": receipt.manifest_digest_sha256,
        "object_count": 2,
        "profile": "personal",
        "restored": True,
    }
    redacted = str(evidence.to_redacted_dict())
    assert "synthetic-personal-entry" not in redacted
    assert "people" not in redacted
    assert (
        tuple((item.relative_path, item.payload, item.tier) for item in source.objects) == original
    )
