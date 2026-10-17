"""Portable15 proves mixed shared replay and consumer custody before destination writes."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import CaptureFault, InjectedFault
from open_brain_engine.engine.consumer_schema import CONSUMER_INTAKE_MIGRATION
from open_brain_engine.engine.contracts import TextPayload
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.owner_retraction import OwnerRetractionTasks
from open_brain_engine.portable.consumer_v15 import CONSUMER_OBJECT_PREFIX, CONSUMER_PATH
from open_brain_engine.portable.lifecycle_v15 import LIFECYCLE_PATH
from open_brain_engine.portable.retraction_v14 import APPROVALS_PATH
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v14 import validated_portable_snapshot_v14
from open_brain_engine.portable.v15_catalog import PORTABLE_V15_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from packages.engine.tests.test_consumer_intake_engine import (
    _commit,
    _conversation,
    _owner,
    _plan,
    _reimport,
)
from packages.engine.tests.test_consumer_intake_engine import active_profiles as active_profiles


def _export(engine: BrainEngine, path: Path) -> dict[str, bytes]:
    assert (
        engine.tasks.portability.export(path, export_id="export_" + str(uuid4())).schema_version
        == 15
    )
    return {
        name: raw
        for name, raw in validated_portable_snapshot(path).files.items()
        if name != "portable-manifest.json"
    }


def _rehash(root: Path) -> None:
    path = root / "portable-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"] = [
        {"path": file.relative_to(root).as_posix(), "sha256": sha256(file.read_bytes()).hexdigest()}
        for file in sorted(root.rglob("*"))
        if file.is_file() and file != path
    ]
    path.write_bytes(canonical(manifest))


def test_empty_successor_required_families_and_catalog(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    files = _export(engine, tmp_path / "archive")
    assert {LIFECYCLE_PATH, CONSUMER_PATH, APPROVALS_PATH} <= set(files)
    assert json.loads(files[CONSUMER_PATH])["objects"] == []
    assert PORTABLE_V15_SCHEMA_CATALOG["migration20_sha256"] == CONSUMER_INTAKE_MIGRATION.checksum
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v14(tmp_path / "archive")


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_real_consumer_roundtrip_reexport_and_unchanged_receipt(
    tmp_path: Path,
    active_profiles: None,
    provider: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan(provider)
    _, result = _commit(engine, plan)
    assert result["status"] == "committed"
    with engine._store.connect() as connection:
        scope = connection.execute("SELECT scope_id FROM consumer_scopes").fetchone()[0]
    files = _export(engine, tmp_path / "archive")
    assert any(path.startswith(CONSUMER_OBJECT_PREFIX) for path in files)
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    assert _export(restored, tmp_path / "again") == files
    assert (
        restored.consumer_imports.consumer_import_status(
            plan.operation_id, authority=_owner(restored)
        )
        == result
    )
    _, unchanged = _commit(restored, _reimport(plan, scope))
    assert cast(dict[str, int], unchanged["counts"])["unchanged"] == 1
    assert unchanged["session_id"] is None
    with restored._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
    # The second accepted operation adds a repackaged archive binding and no capture membership.
    assert _export(restored, tmp_path / "after-unchanged")


def test_no_text_accepted_proof_round_trips_without_registering_scope(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    from dataclasses import replace

    plan = _plan()
    plan = replace(plan, conversations=(replace(plan.conversations[0], messages=()),))
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    _, result = _commit(engine, plan)
    assert cast(dict[str, int], result["counts"])["no_text"] == 1
    files = _export(engine, tmp_path / "archive")
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    with restored._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM consumer_scopes").fetchone()[0] == 0
    assert _export(restored, tmp_path / "again") == files


def test_replacement_shrinks_and_readds_stable_tail_across_restore(
    tmp_path: Path,
    active_profiles: None,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    plan = _plan(conversations=(_conversation(text="intakequartz " + "x" * 60_010),))
    _commit(engine, plan)
    with engine._store.connect() as connection:
        scope = connection.execute("SELECT scope_id FROM consumer_scopes").fetchone()[0]
        sources = {row[0] for row in connection.execute("SELECT source_id FROM logical_sources")}
    short = _reimport(plan, scope, text="intakequartz short replacement", replace_current=True)
    _commit(engine, short)
    _export(engine, tmp_path / "short")
    _commit(
        engine, _reimport(plan, scope, text="intakequartz " + "y" * 60_010, replace_current=True)
    )
    files = _export(engine, tmp_path / "archive")
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    with restored._store.connect() as connection:
        assert {
            row[0] for row in connection.execute("SELECT source_id FROM logical_sources")
        } == sources
        assert sorted(
            row[0] for row in connection.execute("SELECT head_version FROM logical_sources")
        ) == [2, 3]
    assert _export(restored, tmp_path / "again") == files


def test_owner_approval_and_consumer_retraction_survive_restore(
    tmp_path: Path, active_profiles: None
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    _commit(engine, _plan())
    owner_capture = engine.capture.accept(
        TextPayload("Synthetic owner sibling"), delivery_id="owner-sibling"
    )
    authority = _owner(engine)
    preview = OwnerRetractionTasks(engine).preview(
        capture_id=owner_capture.capture_id, authority=authority
    )
    OwnerRetractionTasks(engine).commit(
        cast(str, preview["preview_id"]), str(uuid4()), authority=authority
    )
    with engine._store.connect() as connection:
        capture = connection.execute("SELECT capture_id FROM consumer_segment_bindings").fetchone()[
            0
        ]
    preview = OwnerRetractionTasks(engine).preview(capture_id=capture, authority=authority)
    OwnerRetractionTasks(engine).commit(
        cast(str, preview["preview_id"]), str(uuid4()), authority=authority
    )
    files = _export(engine, tmp_path / "archive")
    assert len(json.loads(files[APPROVALS_PATH])["records"]) == 2
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    assert not restored.retrieval.search("intakequartz")
    assert _export(restored, tmp_path / "again") == files


@pytest.mark.parametrize(
    "point",
    [
        CaptureFault.AFTER_CONSUMER_INTENT,
        CaptureFault.AFTER_CONSUMER_SEGMENT,
        CaptureFault.AFTER_CAPTURE_RESERVATION,
        CaptureFault.AFTER_SOURCE_WRITE,
    ],
)
def test_pending_refuses_export_and_terminal_halted_custody_round_trips(
    tmp_path: Path,
    active_profiles: None,
    point: CaptureFault,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"), faults={point})
    plan = _plan()
    with pytest.raises(InjectedFault):
        _commit(engine, plan)
    with pytest.raises(ValueError, match="operation_pending"):
        _export(engine, tmp_path / "pending")
    assert not (tmp_path / "pending").exists()
    result = engine.consumer_imports.halt_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    assert result["status"] == "halted"
    with engine._store.connect() as connection:
        original_rows = [tuple(row) for row in connection.execute("SELECT * FROM captures")]
        admitted = connection.execute("SELECT count(*) FROM captures WHERE stage=3").fetchone()[0]
    original_sources = {
        path: path.read_bytes() for path in engine.profile.root.glob("sources/captures/**/*.json")
    }
    engine = BrainEngine.open(open_existing_single_user_local(tmp_path / "brain"))
    assert (
        engine.consumer_imports.consumer_import_status(plan.operation_id, authority=_owner(engine))
        == result
    )
    files = _export(engine, tmp_path / "archive")
    with engine._store.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM captures")] == original_rows
    assert {path: path.read_bytes() for path in original_sources} == original_sources
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    assert _export(restored, tmp_path / "again") == files
    assert not restored.retrieval.search("intakequartz")
    with restored._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM consumer_scopes").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == admitted
    assert (
        restored.consumer_imports.consumer_import_status(
            plan.operation_id, authority=_owner(restored)
        )
        == result
    )


@pytest.mark.parametrize("stage", [1, 2])
def test_halted_partial_committed_stages_normalize_without_materializing(
    tmp_path: Path, active_profiles: None, stage: int
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_SOURCE_WRITE}
    )
    plan = _plan()
    with pytest.raises(InjectedFault):
        _commit(engine, plan)
    # Represent a process exit immediately after a durable stage transition.
    source = next(engine.profile.root.glob("sources/captures/**/*.json"))
    with engine._store.transaction() as connection:
        connection.execute(
            "UPDATE captures SET stage=?,source_path=?",
            (stage, source.relative_to(engine.profile.root).as_posix()),
        )
    state = engine.consumer_imports.halt_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    files = _export(engine, tmp_path / "archive")
    assert source.is_file()
    with engine._store.connect() as connection:
        assert connection.execute("SELECT stage FROM captures").fetchone()[0] == stage
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    assert (
        restored.consumer_imports.consumer_import_status(
            plan.operation_id, authority=_owner(restored)
        )
        == state
    )
    assert _export(restored, tmp_path / "again") == files


@pytest.mark.parametrize("corruption", ["file", "row", "path"])
def test_halted_partial_corruption_refuses_export(
    tmp_path: Path, active_profiles: None, corruption: str
) -> None:
    engine = BrainEngine.open(
        compile_single_user_local(tmp_path / "brain"), faults={CaptureFault.AFTER_SOURCE_WRITE}
    )
    plan = _plan()
    with pytest.raises(InjectedFault):
        _commit(engine, plan)
    engine.consumer_imports.halt_consumer_import(plan.operation_id, authority=_owner(engine))
    if corruption == "file":
        next(engine.profile.root.glob("sources/captures/**/*.json")).write_bytes(b"{}")
    else:
        with engine._store.transaction() as connection:
            connection.execute(
                "UPDATE captures SET "
                + (
                    "title='changed'"
                    if corruption == "row"
                    else "source_path='sources/captures/wrong.json'"
                )
            )
    with pytest.raises(ValueError):
        _export(engine, tmp_path / "archive")
    assert not (tmp_path / "archive").exists()


@pytest.mark.parametrize("old_version", [13, 14])
def test_clean_predecessor_snapshot_audits_exactly_on_journal2_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old_version: int,
) -> None:
    from open_brain_engine.engine import local_schema

    with monkeypatch.context() as predecessor:
        predecessor.setattr(
            local_schema, "PHASE1_STATE_SCHEMA_VERSION", {13: 18, 14: 19}[old_version]
        )
        engine = BrainEngine.open(compile_single_user_local(tmp_path / "old"))
        engine.capture.accept(TextPayload("Synthetic predecessor"), delivery_id="predecessor")
        assert (
            engine.tasks.portability.export(
                tmp_path / "archive", export_id="export_" + str(uuid4())
            ).schema_version
            == old_version
        )
    snapshot = validated_portable_snapshot(tmp_path / "archive")
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    with restored._store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        assert (
            connection.execute("SELECT contract FROM lifecycle_journal_identity").fetchone()[0]
            == "ob.capture-lifecycle-journal.v2"
        )
    files = _export(restored, tmp_path / "again")
    for path, raw in snapshot.files.items():
        if path not in {"portable-manifest.json", "history/capture-lifecycle/journal-v1.json"}:
            assert files[path] == raw


@pytest.mark.parametrize(
    "tamper",
    ["orphan-object", "kind", "scope", "outcome", "approval", "source-head", "source-version"],
)
def test_rehashed_consumer_tamper_refuses_before_destination_mutation(
    tmp_path: Path,
    active_profiles: None,
    tamper: str,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    _commit(engine, _plan())
    root = tmp_path / "archive"
    _export(engine, root)
    path = root / CONSUMER_PATH
    value = json.loads(path.read_bytes())
    if tamper == "orphan-object":
        raw = b"{}"
        key = sha256(raw).hexdigest()
        (root / (CONSUMER_OBJECT_PREFIX + key)).write_bytes(raw)
        value["objects"].append({"sha256": key, "kind": "operation"})
        value["objects"].sort(key=lambda item: item["sha256"])
    elif tamper == "kind":
        value["objects"][0]["kind"] = "unknown"
    elif tamper == "scope":
        value["consumer_scopes"][0]["label"] = "Rebound"
    elif tamper == "outcome":
        value["consumer_outcomes"][0]["outcome"] = "unchanged"
    elif tamper == "approval":
        value["approvals"][0]["request_sha256"] = "0" * 64
    else:
        # The source metadata path is a public constant rather than inferred from an archive name.
        from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH

        source_path = root / SOURCE_METADATA_PATH
        metadata = json.loads(source_path.read_bytes())
        if tamper == "source-head":
            metadata["sources"][0]["historical_only"] = True
        else:
            metadata["sources"][0]["head_version"] += 1
        source_path.write_bytes(canonical(metadata))
    path.write_bytes(canonical(value))
    _rehash(root)
    destination = tmp_path / "refused"
    with pytest.raises(PortableValidationError):
        engine.tasks.portability.import_clean(root, destination, import_id="import_" + str(uuid4()))
    assert not destination.exists()


@pytest.mark.parametrize("old_schema", [17, 18, 19])
def test_predecessor_restore_retry_rejects_orphan_consumer_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_schema: int
) -> None:
    from open_brain_engine.engine import local_schema

    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", old_schema)
        engine = BrainEngine.open(compile_single_user_local(tmp_path / "old"))
        engine.tasks.portability.export(tmp_path / "archive", export_id="export_" + str(uuid4()))
    import_id = "import_" + str(uuid4())
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id=import_id
    )
    assert engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id=import_id
    ).duplicate
    restored = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    with restored._store.transaction() as connection:
        connection.execute(
            "INSERT INTO consumer_intent_objects VALUES(?,?,?)",
            (sha256(b"{}").hexdigest(), "operation", b"{}"),
        )
    with restored._store.connect() as connection:
        before = tuple(connection.iterdump())
    with pytest.raises(PortableValidationError, match="consumer authority drift"):
        engine.tasks.portability.import_clean(
            tmp_path / "archive", tmp_path / "restored", import_id=import_id
        )
    with restored._store.connect() as connection:
        assert tuple(connection.iterdump()) == before
