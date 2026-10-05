"""Portable9 preserves separate commitments and mixed historical authority."""

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import open_existing_single_user_local

from ._historical_compatibility_fixtures import portable5_import_engine, retained_import_engine
from .test_historical_compatibility import linked_v2


def test_v9_mixed_chain_preserves_v1_bytes_and_refuses_frozen_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.historical_projection import verify_historical_projection
    from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
    from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
    from open_brain_engine.engine.historical_transition import HistoricalTransitionStore
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.sharing_contracts import SharingError
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority
    from open_brain_engine.portable.v9 import validate_historical_authority

    from .test_historical_baseline import _baseline

    engine = portable5_import_engine(tmp_path, monkeypatch)
    old = _baseline(engine, retained_delivery_id="owner.separate.old")
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "owner", frozenset(), None, owner=True
    )
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            old,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    old_record = HistoricalTransitionStore(engine.profile.root, engine.profile.root_identity).read(
        old.operation_id
    )
    relation, _, _ = linked_v2(engine)
    with engine._store.connect() as connection:
        registry = HistoricalRegistryStore(engine.profile.root, engine.profile.root_identity).read(
            relation.destination
        )
        with pytest.raises(SharingError, match="binding_mismatch"):
            verify_historical_projection(connection, engine.profile, registry)
    archive = tmp_path / "mixed"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(archive)
    history = validate_historical_authority(snapshot.files)
    assert [record.request.dto_version for record in history.records] == [1, 2, 2, 2]
    assert history.records[0].canonical_bytes() == old_record.canonical_bytes()
    destination = tmp_path / "mixed-restored"
    engine.portability.import_clean(archive, destination, import_id="import_" + str(uuid4()))
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    assert (
        HistoricalTransitionStore(restored.profile.root, restored.profile.root_identity)
        .read(old.operation_id)
        .canonical_bytes()
        == old_record.canonical_bytes()
    )


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unknown",
        "boolean_version",
        "operation_version",
        "receipt_version",
        "transition_hash",
        "duplicate",
        "capture_request",
        "accepted_receipt",
        "revision_request",
        "revision_key",
        "alias",
        "import_path",
    ],
)
def test_v9_refuses_damaged_projection_and_mixed_wire_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    import json

    from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
    from open_brain_engine.portable.v1 import PortableValidationError
    from open_brain_engine.portable.v6 import SOURCE_ADMISSION_PATH
    from open_brain_engine.portable.v8_capture_metadata import CAPTURE_METADATA_PATH
    from open_brain_engine.portable.v9 import (
        HISTORICAL_AUTHORITY_PATH,
        validate_historical_authority,
    )

    engine = portable5_import_engine(tmp_path, monkeypatch)
    linked_v2(engine)
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    files = dict(validated_portable_snapshot(archive).files)
    value = json.loads(files[HISTORICAL_AUTHORITY_PATH])
    capture_id = value["operations"][0]["request"]["retained_original"]["capture_id"]
    if damage == "missing":
        files.pop(HISTORICAL_AUTHORITY_PATH)
    elif damage in ("capture_request", "accepted_receipt", "import_path"):
        metadata = json.loads(files[CAPTURE_METADATA_PATH])
        row = next(row for row in metadata["captures"] if row["capture_id"] == capture_id)
        row[
            {
                "capture_request": "request_sha256",
                "accepted_receipt": "accepted_receipt_id",
                "import_path": "submission_path",
            }[damage]
        ] = "wrong"
        files[CAPTURE_METADATA_PATH] = canonical(metadata)
    elif damage in ("revision_request", "revision_key", "alias"):
        admission = json.loads(files[SOURCE_ADMISSION_PATH])
        revision = next(
            row for row in admission["revision_admission"] if row["capture_id"] == capture_id
        )
        if damage == "alias":
            # An invented derived delivery alias must not upgrade an import witness.
            evidence = value["operations"][0]["request"]["retained_original"]
            admission["source_aliases"].append(
                {
                    "delivery_id": evidence["retained_delivery_id"],
                    "source_id": value["operations"][0]["request"]["source_cas"]["source_id"],
                    "evidence_sha256": "0" * 64,
                }
            )
        else:
            revision["request_sha256" if damage == "revision_request" else "revision_key"] = (
                "0" * 64
            )
        files[SOURCE_ADMISSION_PATH] = canonical(admission)
    else:
        if damage == "unknown":
            value["consent"] = []
        elif damage == "boolean_version":
            value["schema_version"] = True
        elif damage == "duplicate":
            value["operations"].append(value["operations"][0])
        elif damage == "transition_hash":
            value["operations"][0]["transition_sha256"] = "0" * 64
        else:
            value["operations"][0]["request" if damage == "operation_version" else "receipt"][
                "dto_version"
            ] = 1
        files[HISTORICAL_AUTHORITY_PATH] = canonical(value)
    with pytest.raises(PortableValidationError):
        validate_historical_authority(files)


@pytest.mark.parametrize(
    "stage",
    [
        "historical_intent_restored",
        "historical_registry_advanced",
        "historical_sql_committed",
        "historical_completed",
        "historical_audited",
    ],
)
def test_v9_hidden_restore_retries_without_promoting_interrupted_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from open_brain_engine.engine import portable_v9_restore
    from open_brain_engine.portable.v1 import PortableSnapshot
    from open_brain_engine.storage.filesystem import RootIdentity

    engine = portable5_import_engine(tmp_path, monkeypatch)
    linked_v2(engine)
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    original = validated_portable_snapshot(archive)
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    restore = portable_v9_restore.restore_portable_v9_root

    def checkpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic interrupted restore")

    def fail_restore(
        root: Path, *, snapshot: PortableSnapshot, expected_root_identity: RootIdentity
    ) -> object:
        return restore(
            root,
            snapshot=snapshot,
            expected_root_identity=expected_root_identity,
            checkpoint=checkpoint,
        )

    with monkeypatch.context() as fault:
        fault.setattr(portable_v9_restore, "restore_portable_v9_root", fail_restore)
        with pytest.raises(RuntimeError, match="synthetic interrupted restore"):
            engine.portability.import_clean(archive, destination, import_id=import_id)
    assert not destination.exists()
    assert validated_portable_snapshot(archive).files == original.files
    assert not engine.portability.import_clean(archive, destination, import_id=import_id).duplicate
    assert engine.portability.import_clean(archive, destination, import_id=import_id).duplicate
    assert validated_portable_snapshot(destination).files == original.files


def test_v9_archive_is_refused_by_actual_v8_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.portable.v1 import PortableValidationError
    from open_brain_engine.portable.v8 import validated_portable_snapshot_v8

    engine = portable5_import_engine(tmp_path, monkeypatch)
    linked_v2(engine)
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v8(archive)


def test_frozen_v8_archive_restores_to_fourteen_without_reinterpreting_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import TextPayload, local_schema
    from open_brain_engine.engine.historical_transition import HistoricalTransitionStore
    from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS

    from open_brain.profile import compile_single_user_local

    from .test_historical_continuity import _adopt

    archive = tmp_path / "frozen-eight"
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        old_engine = BrainEngine.open(compile_single_user_local(tmp_path / "old"))
        baseline = _adopt(old_engine)
        transition = (
            HistoricalTransitionStore(old_engine.profile.root, old_engine.profile.root_identity)
            .read(baseline.operation_id)
            .canonical_bytes()
        )
        assert (
            old_engine.portability.export(
                archive, export_id="export_" + str(uuid4())
            ).schema_version
            == 8
        )
    control = BrainEngine.open(compile_single_user_local(tmp_path / "control"))
    destination = tmp_path / "restored-eight"
    assert (
        control.portability.import_clean(
            archive, destination, import_id="import_" + str(uuid4())
        ).schema_version
        == 8
    )
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    with restored._store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
    assert (
        HistoricalTransitionStore(restored.profile.root, restored.profile.root_identity)
        .read(baseline.operation_id)
        .canonical_bytes()
        == transition
    )
    replay = restored.capture.accept(
        TextPayload("synthetic retained"), delivery_id="owner.original"
    )
    assert replay.duplicate and replay.capture_id == baseline.retained_original.capture_id
    latest = tmp_path / "latest-nine"
    assert (
        restored.portability.export(latest, export_id="export_" + str(uuid4())).schema_version == 9
    )


@pytest.mark.parametrize("fixture", [retained_import_engine, portable5_import_engine])
def test_v9_export_restore_preserves_import_and_cloud_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: Callable[[Path, pytest.MonkeyPatch], BrainEngine],
) -> None:
    engine = fixture(tmp_path, monkeypatch)
    linked_v2(engine)
    with engine._store.connect() as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("captures", "source_revisions", "source_aliases", "historical_operations")
        }
    archive = tmp_path / "archive"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(archive)
    assert snapshot.manifest["schema_version"] == 9
    restored_path = tmp_path / "restored"
    engine.portability.import_clean(archive, restored_path, import_id="import_" + str(uuid4()))
    restored = BrainEngine.open(open_existing_single_user_local(restored_path))
    with restored._store.connect() as connection:
        for table, rows in before.items():
            assert [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ] == rows
    second = tmp_path / "second"
    restored.portability.export(second, export_id="export_" + str(uuid4()))
    assert {
        path: raw
        for path, raw in validated_portable_snapshot(second).files.items()
        if path != "portable-manifest.json"
    } == {path: raw for path, raw in snapshot.files.items() if path != "portable-manifest.json"}
