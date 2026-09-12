from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import (
    BackupFault,
    CaptureAction,
    InjectedFault,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine.portability_ports import LocalTenantStorage

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.appliance_init import initialize_appliance
from open_brain.services.appliance_scheduler import ApplianceJobResult, ApplianceScheduler


def _portable_bytes(root: Path) -> dict[str, bytes]:
    profile = open_existing_single_user_local(root)
    storage = LocalTenantStorage(
        root=root,
        tenant_id=profile.tenant_id,
        root_identity=profile.root_identity,
    )
    return {
        relative: payload
        for relative, payload in storage.portable_files()
        if relative == "brain.toml" or relative.startswith(("content/", "history/", "sources/"))
    }


def test_backup_creates_exact_portable_and_sqlite_snapshots_without_credentials_or_indexes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    space = tasks.inbox.spaces()[0]
    tasks.capture.accept(
        TextPayload("Synthetic recovery page\n"),
        delivery_id="backup.capture.canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    scheduler = ApplianceScheduler(
        open_existing_single_user_local(root),
        handlers={"engine-recover": lambda _context: ApplianceJobResult.empty()},
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    scheduler.run_due(now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))

    destination = tmp_path / "backup"
    receipt = tasks.backup.create(
        destination,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b1",
    )

    manifest_path = destination / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    files = {entry["path"] for entry in manifest["files"]}
    backed_up_portable = {
        path.removeprefix("portable/"): (destination / path).read_bytes()
        for path in files
        if path.startswith("portable/")
    }
    restored_state = sqlite3.connect(destination / "sqlite" / "phase1.sqlite3")
    try:
        assert restored_state.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert restored_state.execute("SELECT COUNT(*) FROM captures").fetchone() == (1,)
    finally:
        restored_state.close()

    assert receipt.status == "created"
    assert backed_up_portable == _portable_bytes(root)
    assert "app-state/appliance-init.json" in files
    assert "app-state/appliance-scheduler/state.json" not in files
    assert "sqlite/phase1.sqlite3" in files
    assert not any(
        "credentials" in path
        or "indexes" in path
        or "control.sock" in path
        or ".open-brain-locks" in path
        or path.endswith(("-journal", "-shm", "-wal"))
        for path in files
    )


def test_backup_retry_conflicts_and_disposable_restore_behaviors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    space = tasks.inbox.spaces()[0]
    tasks.capture.accept(
        TextPayload("Retryable backup body\n"),
        delivery_id="backup.retry.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    destination = tmp_path / "backup"

    first = tasks.backup.create(
        destination,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b2",
    )
    duplicate = tasks.backup.create(
        destination,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b2",
    )

    tasks.capture.accept(
        TextPayload("Changed backup source body\n"),
        delivery_id="backup.retry.changed",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )

    with pytest.raises(ValueError, match="conflicts"):
        tasks.backup.create(
            destination,
            backup_id="backup_123e4567-e89b-42d3-a456-4266141740b2",
        )

    restore_root = tmp_path / "restored"
    restore_root.mkdir(mode=0o700)
    tasks.backup.restore(destination, restore_root)
    restored = open_local_engine(open_existing_single_user_local(restore_root))

    tampered = destination / "sqlite" / "phase1.sqlite3"
    tampered.write_bytes(b"not-a-sqlite-snapshot")
    with pytest.raises(ValueError, match="integrity"):
        tasks.backup.verify(destination)

    assert first.status == "created"
    assert duplicate.duplicate is True
    assert restored.retrieval.search("Retryable backup body")[0].title == "Retryable backup body"


def test_backup_fault_before_manifest_publication_leaves_no_partial_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    space = tasks.inbox.spaces()[0]
    tasks.capture.accept(
        TextPayload("Interrupted backup body\n"),
        delivery_id="backup.fault.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    interrupted = open_local_engine(
        compile_single_user_local(root),
        faults={BackupFault.AFTER_BACKUP_FILE},
    )
    destination = tmp_path / "interrupted-backup"

    with pytest.raises(InjectedFault):
        interrupted.backup.create(
            destination,
            backup_id="backup_123e4567-e89b-42d3-a456-4266141740b3",
        )

    assert not destination.exists()


def test_backup_verify_rejects_structurally_valid_manifest_with_invalid_sqlite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    destination = tmp_path / "backup"
    tasks.backup.create(
        destination,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b4",
    )
    sqlite_path = destination / "sqlite" / "phase1.sqlite3"
    sqlite_path.write_bytes(b"not-a-valid-sqlite-snapshot")
    manifest_path = destination / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest["files"]:
        if entry["path"] == "sqlite/phase1.sqlite3":
            entry["sha256"] = sha256(sqlite_path.read_bytes()).hexdigest()
            entry["size_bytes"] = sqlite_path.stat().st_size
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="SQLite"):
        tasks.backup.verify(destination)


def test_backup_verify_rejects_forbidden_restore_inventory_even_with_matching_digest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    destination = tmp_path / "backup"
    tasks.backup.create(
        destination,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b5",
    )
    forbidden = destination / "app-state" / "appliance-owner-credential"
    forbidden.write_bytes(b"synthetic-forbidden-credential\n")
    manifest_path = destination / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"].append(
        {
            "path": "app-state/appliance-owner-credential",
            "sha256": sha256(forbidden.read_bytes()).hexdigest(),
            "size_bytes": forbidden.stat().st_size,
        }
    )
    manifest["files"].sort(key=lambda entry: entry["path"])
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="inventory"):
        tasks.backup.verify(destination)


def test_backup_verify_rejects_credential_fields_inside_allowlisted_app_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    destination = tmp_path / "backup"
    tasks.backup.create(
        destination,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b9",
    )
    app_state_path = destination / "app-state" / "appliance-init.json"
    app_state_path.write_bytes(
        canonical_json_bytes({"credential": "synthetic-forbidden-credential"})
    )
    manifest_path = destination / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest["files"]:
        if entry["path"] == "app-state/appliance-init.json":
            entry["sha256"] = sha256(app_state_path.read_bytes()).hexdigest()
            entry["size_bytes"] = app_state_path.stat().st_size
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="app state"):
        tasks.backup.verify(destination)


def test_backup_restore_rejects_backup_and_live_root_containment(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    backup = tmp_path / "backup"
    tasks.backup.create(
        backup,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b6",
    )
    inside_backup = backup / "restore-target"
    inside_backup.mkdir(mode=0o700)
    inside_live_root = root / "restore-target"
    inside_live_root.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="contain"):
        tasks.backup.restore(backup, inside_backup)
    with pytest.raises(ValueError, match="contain"):
        tasks.backup.restore(backup, inside_live_root)


def test_backup_rejects_unbounded_scheduler_run_state(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    run_root = root / ".open-brain" / "state" / "appliance-scheduler" / "runs" / "engine-recover"
    run_root.mkdir(mode=0o700, parents=True)
    for index in range(300):
        (run_root / f"run_{index:04d}.json").write_bytes(canonical_json_bytes({"index": index}))
    tasks = open_local_engine(open_existing_single_user_local(root))

    with pytest.raises(ValueError, match="bounded"):
        tasks.backup.create(
            tmp_path / "backup",
            backup_id="backup_123e4567-e89b-42d3-a456-4266141740b7",
        )


def test_backup_restore_is_atomic_before_promotion_and_replays_after_promotion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    backup = tmp_path / "backup"
    tasks.backup.create(
        backup,
        backup_id="backup_123e4567-e89b-42d3-a456-4266141740b8",
    )
    before_promotion = tmp_path / "restore-before-promotion"
    before_promotion.mkdir(mode=0o700)
    interrupted = open_local_engine(
        open_existing_single_user_local(root),
        faults={BackupFault.AFTER_RESTORE_FILE},
    )

    with pytest.raises(InjectedFault):
        interrupted.backup.restore(backup, before_promotion)
    assert not before_promotion.exists()
    restored = tasks.backup.restore(backup, before_promotion)

    after_promotion = tmp_path / "restore-after-promotion"
    promoted = open_local_engine(
        open_existing_single_user_local(root),
        faults={BackupFault.AFTER_RESTORE_PROMOTION},
    )
    with pytest.raises(InjectedFault):
        promoted.backup.restore(backup, after_promotion)
    assert after_promotion.is_dir()
    replayed = tasks.backup.restore(backup, after_promotion)

    assert restored.status == "restored"
    assert replayed.status == "restored"
    assert replayed.duplicate is True
