"""Portable16 retains owner embedding policy history and never re-enables a restored Brain."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import local_schema, paging, portable_v10_restore
from open_brain_engine.engine.embedding_policy import EmbeddingPolicyTasks, read_policy
from open_brain_engine.engine.embedding_policy_schema import EMBEDDING_POLICY_MIGRATION
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.portable_v15_evidence import serialize_portable_v15_state
from open_brain_engine.portable.embedding_policy_v16 import (
    EMBEDDING_POLICY_CONTRACT,
    EMBEDDING_POLICY_PATH,
)
from open_brain_engine.portable.v1 import PortableSnapshot, PortableValidationError
from open_brain_engine.portable.v15 import validated_portable_snapshot_v15
from open_brain_engine.portable.v16_catalog import PORTABLE_V16_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.t03_adapters import owner_authority

COMPONENT = "ab" * 32


@pytest.fixture(autouse=True)
def frozen_schema21_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep frozen Portable16 recipes at their original schema21 boundary."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 21)
    monkeypatch.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 21)


def _engine(root: Path) -> BrainEngine:
    root.parent.chmod(0o700)
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=()))


def _export(engine: BrainEngine, root: Path, *, version: int = 16) -> dict[str, bytes]:
    receipt = engine.tasks.portability.export(root, export_id="export_" + str(uuid4()))
    assert receipt.schema_version == version
    return {
        path: raw
        for path, raw in validated_portable_snapshot(root).files.items()
        if path != "portable-manifest.json"
    }


def _restore(engine: BrainEngine, archive: Path, destination: Path) -> BrainEngine:
    engine.tasks.portability.import_clean(
        archive, destination, import_id="import_" + str(uuid4())
    )
    return BrainEngine.open(open_existing_single_user_local(destination))


def _policy(engine: BrainEngine) -> EmbeddingPolicyTasks:
    return EmbeddingPolicyTasks(engine)


def _rehash(root: Path) -> None:
    path = root / "portable-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"] = [
        {"path": file.relative_to(root).as_posix(), "sha256": sha256(file.read_bytes()).hexdigest()}
        for file in sorted(root.rglob("*"))
        if file.is_file() and file != path
    ]
    path.write_bytes(canonical(manifest))


def test_empty_family_catalog_and_predecessor_reader_refusal(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    files = _export(engine, tmp_path / "archive")
    assert files[EMBEDDING_POLICY_PATH] == b""
    assert PORTABLE_V16_SCHEMA_CATALOG["migration21_sha256"] == EMBEDDING_POLICY_MIGRATION.checksum
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v15(tmp_path / "archive")


def test_history_round_trips_exactly_and_restore_is_always_disabled(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    owner = owner_authority(engine.tasks, session_id="owner-test")
    _policy(engine).enable(COMPONENT, authority=owner)
    _policy(engine).disable(authority=owner)
    _policy(engine).enable("cd" * 32, authority=owner)
    files = _export(engine, tmp_path / "archive")
    rows = [json.loads(line) for line in files[EMBEDDING_POLICY_PATH].splitlines()]
    assert [(row["version"], row["action"]) for row in rows] == [
        (1, "enable"),
        (2, "disable"),
        (3, "enable"),
    ]
    assert {row["contract"] for row in rows} == {EMBEDDING_POLICY_CONTRACT}
    restored = _restore(engine, tmp_path / "archive", tmp_path / "restored")
    policy = _policy(restored).status(authority=owner_authority(restored.tasks, session_id="r"))
    assert policy.status() == {
        "schema": "ob.embedding-policy-status.v1",
        "enabled": False,
        "component_id": None,
        "consent_version": 4,
    }
    assert _export(restored, tmp_path / "again") == files
    # Later owner transitions continue monotonically after the restored history.
    later = _policy(restored).enable(
        COMPONENT, authority=owner_authority(restored.tasks, session_id="r")
    )
    assert later.consent_version == 5
    again = _export(restored, tmp_path / "later")
    assert again[EMBEDDING_POLICY_PATH].startswith(files[EMBEDDING_POLICY_PATH])


def test_vectors_and_cache_paths_are_not_portable(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    cache = engine.profile.root / ".open-brain/cache/embedding-v1"
    cache.mkdir(parents=True, mode=0o700)
    (cache / "vectors.bin").write_bytes(b"synthetic")
    files = _export(engine, tmp_path / "archive")
    assert not any("embedding-v1" in path for path in files)


@pytest.mark.parametrize(
    "mutation",
    [
        b'{"action":"enable","changed_at":"2026-10-17T00:00:00Z","component_id":null,'
        b'"contract":"ob.embedding-policy.v1","version":1}\n',
        b'{"action":"disable","changed_at":"2026-10-17T00:00:00Z","component_id":null,'
        b'"contract":"ob.embedding-policy.v1","version":2}\n'
        b'{"action":"disable","changed_at":"2026-10-17T00:00:00Z","component_id":null,'
        b'"contract":"ob.embedding-policy.v1","version":1}\n',
        b'{"action": "disable"}\n',
        b'{"action":"disable","changed_at":"2026-10-17T00:00:00Z","component_id":null,'
        b'"contract":"ob.embedding-policy.v1","version":1}',
    ],
)
def test_tampered_history_refuses_before_destination_mutation(
    tmp_path: Path, mutation: bytes
) -> None:
    engine = _engine(tmp_path / "brain")
    _export(engine, tmp_path / "archive")
    (tmp_path / "archive" / EMBEDDING_POLICY_PATH).write_bytes(mutation)
    _rehash(tmp_path / "archive")
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path / "archive")
    with pytest.raises((PortableValidationError, ValueError)):
        engine.tasks.portability.import_clean(
            tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
        )
    assert not (tmp_path / "restored").exists()


def test_missing_family_refuses(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    _export(engine, tmp_path / "archive")
    (tmp_path / "archive" / EMBEDDING_POLICY_PATH).unlink()
    _rehash(tmp_path / "archive")
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path / "archive")


def test_portable15_input_restores_with_empty_history_and_disabled_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 20)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:20])
        predecessor.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 20)
        old = _engine(tmp_path / "brain")
        files = _export(old, tmp_path / "archive", version=15)
    assert EMBEDDING_POLICY_PATH not in files
    current = _engine(tmp_path / "current")
    restored = _restore(current, tmp_path / "archive", tmp_path / "restored")
    with sqlite3.connect(
        restored.profile.root / local_schema.PHASE1_STATE_DATABASE
    ) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 21
        policy = read_policy(connection)
        assert connection.execute("SELECT count(*) FROM embedding_policy_history").fetchone() == (
            0,
        )
    assert (policy.enabled, policy.component_id, policy.consent_version) == (False, None, 0)
    assert _export(restored, tmp_path / "again")[EMBEDDING_POLICY_PATH] == b""


def test_inherited_evidence_is_byte_identical_to_portable15_writers(tmp_path: Path) -> None:
    # An exact v15 export of a schema21 Brain is impossible by design (the exporter selects
    # the serializer from the schema), so compare against the v15 evidence writer itself.
    engine = _engine(tmp_path / "brain")
    owner = owner_authority(engine.tasks, session_id="owner-test")
    _policy(engine).enable(COMPONENT, authority=owner)
    files = _export(engine, tmp_path / "archive")
    with sqlite3.connect(engine.profile.root / local_schema.PHASE1_STATE_DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        inherited = serialize_portable_v15_state(
            connection,
            engine.profile,
            relationship_sidecar_present=True,
            _expected_schema=21,
        )
    assert inherited.sidecars
    assert EMBEDDING_POLICY_PATH not in inherited.sidecars
    for path, raw in inherited.sidecars.items():
        assert files[path] == raw, path


@pytest.mark.parametrize("stage", ["embedding_policy_restored", "historical_audited"])
def test_interrupted_restore_leaves_no_destination_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    engine = _engine(tmp_path / "brain")
    owner = owner_authority(engine.tasks, session_id="owner-test")
    _policy(engine).enable(COMPONENT, authority=owner)
    _export(engine, tmp_path / "archive")
    before = validated_portable_snapshot(tmp_path / "archive").files
    destination = tmp_path / "restored"
    restore = portable_v10_restore.restore_portable_v10_root

    def checkpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic embedding policy restore interruption")

    def interrupted(
        root: Path, *, snapshot: PortableSnapshot, expected_root_identity: Any
    ) -> Any:
        return restore(
            root,
            snapshot=snapshot,
            expected_root_identity=expected_root_identity,
            checkpoint=checkpoint,
        )

    import_id = "import_" + str(uuid4())
    with monkeypatch.context() as fault:
        fault.setattr(portable_v10_restore, "restore_portable_v10_root", interrupted)
        with pytest.raises(RuntimeError, match="synthetic embedding policy restore"):
            engine.tasks.portability.import_clean(
                tmp_path / "archive", destination, import_id=import_id
            )
    assert not destination.exists()
    assert validated_portable_snapshot(tmp_path / "archive").files == before
    result = engine.tasks.portability.import_clean(
        tmp_path / "archive", destination, import_id=import_id
    )
    assert not result.duplicate
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    status = _policy(restored).status(authority=owner_authority(restored.tasks, session_id="r"))
    assert (status.enabled, status.consent_version) == (False, 2)
