from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from open_brain_legacy.operations.backup import (
    BackupError,
    BackupObject,
    BackupTier,
    get_backup_job,
)
from open_brain_legacy.operations.backup_writer import (
    FilesystemBackupSource,
    FilesystemBackupStore,
    inspect_backup_evidence,
)


def _source(tmp_path: Path) -> FilesystemBackupSource:
    roots = {
        "work_root": tmp_path / "work",
        "personal_root": tmp_path / "personal",
        "capture_root": tmp_path / "capture",
        "saved_content_root": tmp_path / "saved-content",
        "state_root": tmp_path / "state",
    }
    for root in roots.values():
        root.mkdir()
    return FilesystemBackupSource(**roots)


def test_filesystem_backup_source_maps_all_five_namespaces(tmp_path: Path) -> None:
    source = _source(tmp_path)
    (tmp_path / "work" / "page.md").write_bytes(b"work")
    (tmp_path / "personal" / "person.json").write_bytes(b"personal")
    (tmp_path / "capture" / "event.json").write_bytes(b"capture")
    (tmp_path / "saved-content" / "article.md").write_bytes(b"saved")
    (tmp_path / "state" / "cursor.json").write_bytes(b"state")
    locks = tmp_path / "state" / ".open-brain-locks"
    locks.mkdir()
    (locks / "lease.shared-writer").write_bytes(b"lock")

    objects = source.collect()

    assert tuple((item.relative_path, item.tier) for item in objects) == (
        (PurePosixPath("capture/event.json"), BackupTier.CAPTURE),
        (PurePosixPath("personal/person.json"), BackupTier.PERSONAL),
        (PurePosixPath("runtime/cursor.json"), BackupTier.RUNTIME_STATE),
        (
            PurePosixPath("runtime/locks/lease.shared-writer"),
            BackupTier.RUNTIME_STATE,
        ),
        (PurePosixPath("saved-content/article.md"), BackupTier.SAVED_CONTENT),
        (PurePosixPath("work/page.md"), BackupTier.WORK),
    )


def test_filesystem_backup_source_rejects_symbolic_links(tmp_path: Path) -> None:
    source = _source(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (tmp_path / "work" / "linked.txt").symlink_to(outside)

    with pytest.raises(BackupError, match="symbolic links"):
        source.collect()


def test_runtime_source_uses_sqlite_backup_api_and_excludes_live_sidecars(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    database = tmp_path / "state" / "events.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE events (value TEXT NOT NULL)")
        connection.execute("INSERT INTO events(value) VALUES ('first')")
        connection.commit()

        objects = source.collect()
    finally:
        connection.close()

    runtime = next(
        item for item in objects if item.relative_path == PurePosixPath("runtime/events.sqlite3")
    )
    assert all(not str(item.relative_path).endswith(("-wal", "-shm")) for item in objects)
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(runtime.payload)
    verification = sqlite3.connect(restored)
    try:
        assert verification.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert verification.execute("SELECT value FROM events").fetchone() == ("first",)
    finally:
        verification.close()


def test_filesystem_backup_store_is_immutable_and_manifest_is_published_last(
    tmp_path: Path,
) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    store = FilesystemBackupStore(root=root)
    backup_id = "backup-" + "a" * 24
    item = BackupObject(PurePosixPath("work/page.md"), b"page", BackupTier.WORK)

    store.stage_objects(backup_id=backup_id, objects=(item,))
    assert store.read_object(backup_id=backup_id, relative_path=item.relative_path) == b"page"
    with pytest.raises(BackupError, match="manifest unavailable"):
        store.read_manifest(backup_id=backup_id)

    store.publish_manifest(backup_id=backup_id, manifest=b'{"synthetic":true}')
    store.publish_manifest(backup_id=backup_id, manifest=b'{"synthetic":true}')
    assert store.read_manifest(backup_id=backup_id) == b'{"synthetic":true}'

    with pytest.raises(BackupError, match="immutable backup conflict"):
        store.stage_objects(
            backup_id=backup_id,
            objects=(
                BackupObject(
                    PurePosixPath("work/page.md"),
                    b"different",
                    BackupTier.WORK,
                ),
            ),
        )


def test_backup_evidence_inspection_reads_published_manifests_without_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    source = _source(tmp_path)
    (tmp_path / "capture" / "event.json").write_bytes(b"capture")
    receipt = get_backup_job("JOB-011").run(
        source=source,
        store=FilesystemBackupStore(root=root),
        created_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )

    snapshot = inspect_backup_evidence(root)

    assert receipt.object_count == 1
    assert snapshot.manifest_count == 1
    assert snapshot.malformed_count == 0
    assert snapshot.profile_latest[0][0] == "capture"
    assert snapshot.profile_latest[0][1].isoformat() == "2026-08-16T12:00:00+00:00"


def test_backup_evidence_rejects_missing_or_corrupt_objects(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    root.mkdir()
    source = _source(tmp_path)
    (tmp_path / "capture" / "event.json").write_bytes(b"capture")
    receipt = get_backup_job("JOB-011").run(
        source=source,
        store=FilesystemBackupStore(root=root),
        created_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
    )
    target = root / "backups" / receipt.backup_id / "objects" / "capture" / "event.json"
    target.write_bytes(b"corrupt")

    snapshot = inspect_backup_evidence(root)

    assert snapshot.manifest_count == 0
    assert snapshot.malformed_count == 1


def test_backup_evidence_inspection_counts_malformed_entries(tmp_path: Path) -> None:
    root = tmp_path / "backup"
    malformed = root / "backups" / ("backup-" + "b" * 24)
    malformed.mkdir(parents=True)
    (malformed / "manifest.json").write_bytes(b"not-json")

    snapshot = inspect_backup_evidence(root)

    assert snapshot.manifest_count == 0
    assert snapshot.malformed_count == 1
    assert snapshot.profile_latest == ()
