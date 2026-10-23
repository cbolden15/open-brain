"""Portable17 retains owner egress policy history and re-derives its overlay on restore."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import TextPayload, local_schema, paging
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.owner_egress_policy import (
    OwnerEgressPolicyTasks,
    read_owner_egress_policy,
)
from open_brain_engine.engine.owner_egress_policy_schema import OWNER_EGRESS_POLICY_MIGRATION
from open_brain_engine.engine.privacy_repairs import audit_privacy_repair_ledger
from open_brain_engine.portable.egress_policy_v17 import (
    OWNER_EGRESS_POLICY_CONTRACT,
    OWNER_EGRESS_POLICY_PATH,
)
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v16 import validated_portable_snapshot_v16
from open_brain_engine.portable.v17_catalog import PORTABLE_V17_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.t03_adapters import owner_authority


@pytest.fixture(autouse=True)
def frozen_schema22_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep frozen Portable17 recipes at their original schema22 boundary."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 22)
    monkeypatch.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 22)


def _engine(root: Path) -> BrainEngine:
    root.parent.chmod(0o700)
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=()))


def _export(engine: BrainEngine, root: Path, *, version: int = 17) -> dict[str, bytes]:
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


def _set(engine: BrainEngine, tiers: set[PrivacyTier], operation_id: str) -> None:
    OwnerEgressPolicyTasks(engine).set(
        tiers,
        operation_id=operation_id,
        authority=owner_authority(engine.tasks, session_id="owner-test"),
    )


def _capture(engine: BrainEngine, tier: PrivacyTier) -> str:
    return engine.capture.accept(
        TextPayload(f"Synthetic portable {tier.value} record"),
        delivery_id=f"portable.{tier.value}",
        privacy_tier=tier,
    ).capture_id


def _connect(engine: BrainEngine) -> sqlite3.Connection:
    connection = sqlite3.connect(engine.profile.root / local_schema.PHASE1_STATE_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def _egress(engine: BrainEngine, capture_id: str) -> tuple[int, int, bool]:
    with _connect(engine) as connection:
        search = connection.execute(
            "SELECT effective_cloud,effective_external_egress FROM search_documents "
            "WHERE capture_id=? AND record_type='source'",
            (capture_id,),
        ).fetchone()
        revision = connection.execute(
            "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id=?",
            (capture_id,),
        ).fetchone()[0]
    return (
        search["effective_cloud"],
        search["effective_external_egress"],
        json.loads(revision)["authority"]["external_egress"],
    )


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
    assert files[OWNER_EGRESS_POLICY_PATH] == b""
    assert (
        PORTABLE_V17_SCHEMA_CATALOG["migration22_sha256"]
        == OWNER_EGRESS_POLICY_MIGRATION.checksum
    )
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v16(tmp_path / "archive")


def test_policy_round_trips_and_exported_privacy_stays_the_base(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    work = _capture(engine, PrivacyTier.WORK)
    personal = _capture(engine, PrivacyTier.PERSONAL)
    unknown = _capture(engine, PrivacyTier.UNKNOWN)
    unshared = _export(engine, tmp_path / "unshared")
    _set(engine, {PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}, "op-1")
    _set(engine, {PrivacyTier.WORK}, "op-2")
    assert _egress(engine, work) == (0, 1, True)
    with _connect(engine) as connection:
        audit_privacy_repair_ledger(connection)
    files = _export(engine, tmp_path / "archive")
    rows = [json.loads(line) for line in files[OWNER_EGRESS_POLICY_PATH].splitlines()]
    assert [(row["version"], row["tiers"], row["operation_id"]) for row in rows] == [
        (1, ["personal", "public", "work"], "op-1"),
        (2, ["work"], "op-2"),
    ]
    assert {row["contract"] for row in rows} == {OWNER_EGRESS_POLICY_CONTRACT}
    # Only the policy family differs: exported privacy evidence is policy-independent.
    assert {path: raw for path, raw in files.items() if path != OWNER_EGRESS_POLICY_PATH} == {
        path: raw for path, raw in unshared.items() if path != OWNER_EGRESS_POLICY_PATH
    }
    restored = _restore(engine, tmp_path / "archive", tmp_path / "restored")
    with _connect(restored) as connection:
        policy = read_owner_egress_policy(connection)
        audit_privacy_repair_ledger(connection)
    assert (policy.version, policy.tiers, policy.operation_id) == (
        2,
        frozenset({PrivacyTier.WORK}),
        "op-2",
    )
    assert _egress(restored, work) == (0, 1, True)
    assert _egress(restored, personal) == (0, 0, False)
    assert _egress(restored, unknown) == (0, 0, False)
    assert _export(restored, tmp_path / "again") == files
    _set(restored, set(), "op-3")
    assert _egress(restored, work) == (0, 0, False)


@pytest.mark.parametrize(
    "mutation",
    [
        b'{"contract":"ob.owner-egress-policy.v1","operation_id":"op","recorded_at":'
        b'"2026-10-22T00:00:00Z","tiers":["secret"],"version":1}\n',
        b'{"contract":"ob.owner-egress-policy.v1","operation_id":"op","recorded_at":'
        b'"2026-10-22T00:00:00Z","tiers":["work"],"version":2}\n',
        b'{"contract":"ob.owner-egress-policy.v1","operation_id":"op","recorded_at":'
        b'"2026-10-22T00:00:00Z","tiers":["work","personal"],"version":1}\n',
        b'{"tiers": ["work"]}\n',
        b'{"contract":"ob.owner-egress-policy.v1","operation_id":"op","recorded_at":'
        b'"2026-10-22T00:00:00Z","tiers":["work"],"version":1}',
    ],
)
def test_tampered_history_refuses_before_destination_mutation(
    tmp_path: Path, mutation: bytes
) -> None:
    engine = _engine(tmp_path / "brain")
    _export(engine, tmp_path / "archive")
    (tmp_path / "archive" / OWNER_EGRESS_POLICY_PATH).write_bytes(mutation)
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
    (tmp_path / "archive" / OWNER_EGRESS_POLICY_PATH).unlink()
    _rehash(tmp_path / "archive")
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path / "archive")


def test_portable16_input_restores_with_no_shareable_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 21)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:21])
        predecessor.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 21)
        old = _engine(tmp_path / "brain")
        work = _capture(old, PrivacyTier.WORK)
        files = _export(old, tmp_path / "archive", version=16)
    assert OWNER_EGRESS_POLICY_PATH not in files
    current = _engine(tmp_path / "current")
    restored = _restore(current, tmp_path / "archive", tmp_path / "restored")
    with _connect(restored) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        assert read_owner_egress_policy(connection).version == 0
    assert _egress(restored, work) == (0, 0, False)
    assert _export(restored, tmp_path / "again")[OWNER_EGRESS_POLICY_PATH] == b""
