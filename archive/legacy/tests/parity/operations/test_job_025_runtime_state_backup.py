from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
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


def test_job_025_runtime_state_profile_preserves_generation_and_restores_redacted(
    tmp_path: Path,
) -> None:
    cursor = b'{"cursor":"synthetic-private-cursor"}'
    source = FakeBackupSource(
        (
            BackupSourceObject(
                PurePosixPath("runtime/state/cursor.json"), cursor, BackupTier.RUNTIME_STATE
            ),
            BackupSourceObject(
                PurePosixPath("runtime/state/replay.json"),
                b'{"replay":"synthetic-private-replay"}',
                BackupTier.RUNTIME_STATE,
            ),
            BackupSourceObject(
                PurePosixPath("runtime/locks/writer.lock"),
                b"synthetic-lock",
                BackupTier.RUNTIME_STATE,
            ),
            BackupSourceObject(
                PurePosixPath("work/pages/outside.md"), b"synthetic-work", BackupTier.WORK
            ),
        )
    )
    original = tuple((item.relative_path, item.payload, item.tier) for item in source.objects)
    store = MemoryBackupStore()
    application = get_backup_job("JOB-025")

    with pytest.raises(BackupError, match="requires a generation"):
        application.run(source=source, store=store, created_at=FIXED_TIME)
    assert source.collect_calls == 0

    receipt = application.run(
        source=source,
        store=store,
        created_at=FIXED_TIME,
        generation="runtime-generation-0007",
    )

    assert source.collect_calls == 1
    assert application.profile.name == "runtime-state"
    assert application.profile.included_tiers == frozenset({BackupTier.RUNTIME_STATE})
    assert application.profile.excluded_prefixes == (PurePosixPath("runtime/locks"),)
    assert {entry.relative_path for entry in receipt.entries} == {
        PurePosixPath("runtime/state/cursor.json"),
        PurePosixPath("runtime/state/replay.json"),
    }
    manifest = json.loads(store.manifests[receipt.backup_id])
    assert manifest["generation"] == "runtime-generation-0007"
    assert manifest["retention"] == application.profile.retention.to_dict()
    assert {entry["tier"] for entry in manifest["entries"]} == {"runtime-state"}
    cursor_entry = next(
        entry for entry in manifest["entries"] if entry["relative_path"].endswith("cursor.json")
    )
    assert cursor_entry["digest_sha256"] == sha256(cursor).hexdigest()
    assert len(receipt.manifest_digest_sha256) == 64

    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    evidence = restore_backup(
        receipt,
        store=store,
        target=DisposableRestoreRoot.create(restore_root),
    )

    assert (restore_root / "runtime/state/cursor.json").read_bytes() == cursor
    assert not (restore_root / "runtime/locks/writer.lock").exists()
    assert not (restore_root / "work/pages/outside.md").exists()
    assert evidence.to_redacted_dict() == {
        "backup_id": receipt.backup_id,
        "checksums_verified": 3,
        "generation": "runtime-generation-0007",
        "manifest_digest_sha256": receipt.manifest_digest_sha256,
        "object_count": 2,
        "profile": "runtime-state",
        "restored": True,
    }
    redacted = str(evidence.to_redacted_dict())
    assert "synthetic-private-cursor" not in redacted
    assert "synthetic-private-replay" not in redacted
    assert (
        tuple((item.relative_path, item.payload, item.tier) for item in source.objects) == original
    )
