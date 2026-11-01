from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    CaptureAction,
    LocalEngineContext,
    ReferencePayload,
    TextPayload,
    local_schema,
    root_identity_repair,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import (
    PHASE1_STATE_DATABASE,
    open_local_database,
    open_local_database_read_only,
)
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.root_identity_repair import repair_recorded_root_devices
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.source_migration import JOURNAL, migrate_sources
from open_brain_engine.engine.t03_contracts import T03Error
from open_brain_engine.portable.v1 import PortableValidationError, validated_portable_snapshot
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH, validated_portable_snapshot_v4
from open_brain_engine.storage.migrations import SchemaError
from open_brain_engine.storage.sqlite import connect_database_read_only

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


@pytest.mark.parametrize(
    "crash",
    [
        None,
        "journal_durable",
        "old_visible_barrier",
        "sidecars_durable",
        "schema_committed",
        "metadata_published",
        "validated",
    ],
)
def test_source_cutover_recovers_without_rewriting_legacy_bytes(
    tmp_path: Path, crash: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        engine = BrainEngine.open(profile)
        submission = _public_submission(engine.tasks)
        first = replace(submission, payload=ReferencePayload(submission.source_reference, "first"))
        old = engine.capture.submit(first)
        current = engine.capture.submit(
            replace(first, payload=ReferencePayload(first.source_reference, "second"))
        )
    preimages = {
        p.relative_to(profile.root): p.read_bytes()
        for p in (profile.root / "sources").rglob("*")
        if p.is_file()
    }

    class Crash(RuntimeError):
        pass

    def checkpoint(stage: str) -> None:
        if stage == crash:
            raise Crash

    with exclusive_runtime_admission(profile) as admission:
        if crash is not None:
            with pytest.raises(Crash):
                migrate_sources(
                    profile,
                    admission=admission,
                    clock=lambda: datetime.now(UTC),
                    checkpoint=checkpoint,
                )
            with pytest.raises(SchemaError, match="migration is pending"):
                open_local_database_read_only(profile)
            with pytest.raises(SchemaError, match="migration is pending"):
                open_local_database(profile)
            if crash != "journal_durable":
                with monkeypatch.context() as legacy:
                    legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
                    with pytest.raises(SchemaError):
                        engine.capture.accept(TextPayload("refused"), delivery_id="legacy.refused")
        migrate_sources(profile, admission=admission, clock=lambda: datetime.now(UTC))
    assert json.loads((profile.root / JOURNAL).read_bytes())["stage"] == "complete"
    for path, data in preimages.items():
        assert (profile.root / path).read_bytes() == data
    metadata = json.loads((profile.root / SOURCE_METADATA_PATH).read_bytes())
    export = tmp_path / "synthetic-export"
    export.mkdir(mode=0o700)
    manifest_bytes = (profile.root / "portable-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    for entry in manifest["files"]:
        target = export / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes((profile.root / entry["path"]).read_bytes())
    (export / "portable-manifest.json").write_bytes(manifest_bytes)
    validated_portable_snapshot_v4(export)
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(export)
    assert {row["capture_id"] for row in metadata["revisions"]} == {
        old.capture_id,
        current.capture_id,
    }
    with connect_database_read_only(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    ) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
            2,
            7,
        )
        assert (
            connection.execute(
                "SELECT historical_only FROM logical_sources WHERE head_capture_id=?",
                (old.capture_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT historical_only FROM logical_sources WHERE head_capture_id=?",
                (current.capture_id,),
            ).fetchone()[0]
            == 0
        )


def _legacy_store_with_managed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalEngineContext, Path]:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        engine = BrainEngine.open(profile)
        engine.capture.accept(
            TextPayload("Managed body"),
            delivery_id="managed.capture",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=engine.inbox.spaces()[0].space_id,
        )
        engine.managed_workspace.setup(str(workspace), operation_id="managed.setup")
    assert any(workspace.rglob("*.md"))
    return profile, workspace


def _migrate(profile: LocalEngineContext) -> None:
    with exclusive_runtime_admission(profile) as admission:
        migrate_sources(profile, admission=admission, clock=lambda: datetime.now(UTC))


def test_source_cutover_accepts_a_workspace_whose_device_number_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # macOS can renumber a volume's st_dev across reboots; the folder is unchanged.
    profile, workspace = _legacy_store_with_managed_workspace(tmp_path, monkeypatch)
    _shift_recorded_device(profile)
    with pytest.raises(T03Error, match="operation_pending"):
        _migrate(profile)

    assert repair_recorded_root_devices(profile) == 1
    _migrate(profile)

    assert json.loads((profile.root / JOURNAL).read_bytes())["stage"] == "complete"
    assert _recorded_device(profile) == str(workspace.stat().st_dev)


def test_root_device_repair_leaves_a_matching_record_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, workspace = _legacy_store_with_managed_workspace(tmp_path, monkeypatch)

    assert repair_recorded_root_devices(profile) == 0
    assert _recorded_device(profile) == str(workspace.stat().st_dev)


def test_source_cutover_refuses_a_replaced_workspace_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, workspace = _legacy_store_with_managed_workspace(tmp_path, monkeypatch)
    original = tmp_path / "workspace-original"
    workspace.rename(original)
    shutil.copytree(original, workspace)
    _shift_recorded_device(profile)

    assert repair_recorded_root_devices(profile) == 0
    with pytest.raises(T03Error, match="operation_pending"):
        _migrate(profile)


def test_root_device_repair_skips_a_root_on_another_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inode numbers repeat across volumes, so a matching inode on a different
    # device than the Brain's (another drive mounted at the path) is not proof.
    profile, workspace = _legacy_store_with_managed_workspace(tmp_path, monkeypatch)
    _shift_recorded_device(profile)
    other_volume = (workspace.stat().st_dev + 7, workspace.stat().st_ino)
    monkeypatch.setattr(root_identity_repair, "capture_root_identity", lambda _root: other_volume)

    assert repair_recorded_root_devices(profile) == 0
    assert _recorded_device(profile) == str(workspace.stat().st_dev + 1)


def test_root_device_repair_skips_a_workspace_with_a_pending_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, workspace = _legacy_store_with_managed_workspace(tmp_path, monkeypatch)
    _shift_recorded_device(profile)
    database = sqlite3.connect(profile.root / PHASE1_STATE_DATABASE)
    with database:
        pending = "UPDATE managed_operations SET status='prepared', completed_at=NULL"
        assert database.execute(pending).rowcount >= 1
    database.close()

    assert repair_recorded_root_devices(profile) == 0
    assert _recorded_device(profile) == str(workspace.stat().st_dev + 1)


def _shift_recorded_device(profile: LocalEngineContext) -> None:
    """Simulate the volume being renumbered: the folder is unchanged, st_dev is not."""
    database = sqlite3.connect(profile.root / PHASE1_STATE_DATABASE)
    with database:
        assert (
            database.execute(
                "UPDATE managed_workspaces SET device=CAST(CAST(device AS INTEGER)+1 AS TEXT)"
            ).rowcount
            == 1
        )
    database.close()


def _recorded_device(profile: LocalEngineContext) -> str:
    database = sqlite3.connect(profile.root / PHASE1_STATE_DATABASE)
    try:
        return str(database.execute("SELECT device FROM managed_workspaces").fetchone()[0])
    finally:
        database.close()
