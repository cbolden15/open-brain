from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes

from open_brain_legacy._compat.open_brain.config import AppConfig, RetainedRoots
from open_brain_legacy.production.retention import (
    ProductionRetentionError,
    compose_production_retention_service,
    load_private_retention_config,
)
from open_brain_legacy.production.sqlite_backup import (
    SQLiteBackupProbeError,
    probe_local_sqlite_backups,
)


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 16, 20, 0, tzinfo=UTC)


def _app_config(tmp_path: Path) -> AppConfig:
    roots = {
        name: tmp_path / name
        for name in ("work", "personal", "capture", "saved", "state", "backup")
    }
    for root in roots.values():
        root.mkdir()
    return AppConfig(
        roots=RetainedRoots(
            work=roots["work"],
            personal=roots["personal"],
            capture=roots["capture"],
            saved_content=roots["saved"],
            state=roots["state"],
        ),
        backup=roots["backup"],
        host_identity="synthetic-host",
    )


def _private_file(path: Path, value: object) -> Path:
    path.write_bytes(canonical_json_bytes(value))
    path.chmod(0o600)
    return path


def test_sqlite_backup_probe_snapshots_every_database_via_api_without_disk_writes(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    first_root = state_root / "events"
    second_root = state_root / "review"
    first_root.mkdir(parents=True)
    second_root.mkdir()
    first_path = first_root / "events.sqlite3"
    second_path = second_root / "review.sqlite3"
    first = sqlite3.connect(first_path)
    second = sqlite3.connect(second_path)
    try:
        first.execute("PRAGMA journal_mode=WAL")
        first.execute("CREATE TABLE events(value TEXT NOT NULL)")
        first.executemany("INSERT INTO events(value) VALUES (?)", (("one",), ("two",)))
        first.commit()
        second.execute("CREATE TABLE reviews(value INTEGER NOT NULL)")
        second.execute("INSERT INTO reviews(value) VALUES (1)")
        second.commit()
        before = {
            path.relative_to(state_root): path.stat().st_mtime_ns
            for path in state_root.rglob("*")
            if path.is_file()
        }

        result = probe_local_sqlite_backups(
            state_root=state_root,
            clock=FixedClock(),
        )

        after = {
            path.relative_to(state_root): path.stat().st_mtime_ns
            for path in state_root.rglob("*")
            if path.is_file()
        }
        assert result.database_count == 2
        assert result.object_count == 2
        assert len(result.manifest_set_digest_sha256) == 64
        assert before == after
        assert first.execute("SELECT count(*) FROM events").fetchone() == (2,)
    finally:
        first.close()
        second.close()


def test_sqlite_backup_probe_rejects_malformed_and_symlinked_sources(
    tmp_path: Path,
) -> None:
    malformed_root = tmp_path / "malformed"
    malformed_root.mkdir()
    (malformed_root / "broken.sqlite3").write_bytes(b"not a database")
    with pytest.raises(SQLiteBackupProbeError):
        probe_local_sqlite_backups(state_root=malformed_root, clock=FixedClock())

    real_root = tmp_path / "real"
    linked_root = tmp_path / "linked"
    real_root.mkdir()
    linked_root.mkdir()
    database = real_root / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fixture(value INTEGER)")
    (linked_root / "source.sqlite3").symlink_to(database)
    with pytest.raises(SQLiteBackupProbeError):
        probe_local_sqlite_backups(state_root=linked_root, clock=FixedClock())


def test_private_retention_config_drives_real_dry_run_and_blocks_apply(
    tmp_path: Path,
) -> None:
    app_config = _app_config(tmp_path)
    expired = app_config.backup_root / "expired.bin"
    protected = app_config.backup_root / "recovery.json"
    expired.write_bytes(b"synthetic expired artifact")
    protected.write_bytes(b"synthetic recovery artifact")
    cutoff = FixedClock().now()
    config_path = _private_file(
        tmp_path / "retention.json",
        {
            "schema_version": 1,
            "root": "backup",
            "candidates": [
                {
                    "artifact_id": "artifact_expired",
                    "relative_path": "expired.bin",
                    "expires_at": (cutoff - timedelta(days=1)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "kind": "expirable",
                },
                {
                    "artifact_id": "artifact_recovery",
                    "relative_path": "recovery.json",
                    "expires_at": (cutoff - timedelta(days=30)).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "kind": "recovery_critical",
                },
            ],
        },
    )
    service = compose_production_retention_service(
        app_config=app_config,
        config_path=config_path,
        clock=FixedClock(),
    )

    report = service.retain(dry_run=True)

    assert report.candidate_count == 2
    assert report.protected_count == 1
    assert report.removed_count == 0
    assert len(report.manifest_digest) == 64
    assert expired.exists()
    assert protected.exists()
    with pytest.raises(ProductionRetentionError):
        service.retain(dry_run=False)
    assert expired.exists()


def test_private_retention_config_is_owner_only_canonical_and_closed(
    tmp_path: Path,
) -> None:
    path = _private_file(
        tmp_path / "retention.json",
        {"schema_version": 1, "root": "backup", "candidates": []},
    )
    assert load_private_retention_config(path).root.value == "backup"

    path.chmod(0o644)
    with pytest.raises(ProductionRetentionError):
        load_private_retention_config(path)

    extra = _private_file(
        tmp_path / "extra.json",
        {"schema_version": 1, "root": "backup", "candidates": [], "extra": True},
    )
    with pytest.raises(ProductionRetentionError):
        load_private_retention_config(extra)
