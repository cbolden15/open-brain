"""Portable14 preserves immutable owner approvals without exporting preview custody."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.capture_lifecycle_contracts import LifecycleRequest, digest
from open_brain_engine.engine.capture_lifecycle_journal import LifecycleJournal
from open_brain_engine.engine.contracts import TextPayload
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.owner_retraction_contracts import (
    COUNTS,
    approval_record,
    make_receipt,
)
from open_brain_engine.engine.portable_v10_restore import restore_portable_v10_root
from open_brain_engine.engine.reconciliation import rederive_live_search_projection
from open_brain_engine.engine.retraction_impact import inspect_retraction
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.retraction_v14 import APPROVALS_CONTRACT, APPROVALS_PATH
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v5 import ISSUER_MIGRATION_PATH
from open_brain_engine.portable.v13 import (
    validate_portable_file_set_v13,
    validated_portable_snapshot_v13,
)
from open_brain_engine.portable.v13_catalog import PORTABLE_V13_SCHEMA_CATALOG_DIGEST
from open_brain_engine.portable.v14_catalog import PORTABLE_V14_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local, open_existing_single_user_local


@pytest.fixture(autouse=True)
def frozen_schema19_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep frozen Portable14 recipes at their original schema19 boundary."""
    from open_brain_engine.engine import local_schema, paging

    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 19)
    monkeypatch.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 19)


def _engine(root: Path) -> BrainEngine:
    root.parent.chmod(0o700)
    return BrainEngine.open(compile_single_user_local(root))


def _journal(connection: sqlite3.Connection) -> LifecycleJournal:
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    return LifecycleJournal.for_engine(connection, brain_id=identity[0], issuer_epoch=identity[1])


def _export(engine: BrainEngine, root: Path) -> dict[str, bytes]:
    assert engine.tasks.portability.export(
        root, export_id="export_" + str(uuid4())
    ).schema_version == 14
    return {
        path: raw for path, raw in validated_portable_snapshot(root).files.items()
        if path != "portable-manifest.json"
    }


def _approve(engine: BrainEngine, *, corruption: str | None = None) -> dict[str, object]:
    capture = engine.capture.accept(TextPayload("Synthetic owner retraction archive body."),
                                    delivery_id="synthetic.portable14." + str(uuid4()))
    with engine._store.transaction() as connection:
        journal = _journal(connection)
        impact = inspect_retraction(
            journal, capture_id=capture.capture_id, session_id=None, expected_preimage_sha256=None
        )
        request = LifecycleRequest(str(uuid4()), "capture_retract", journal.brain_id,
                                   journal.issuer_epoch, {"capture_id": capture.capture_id})
        approval: dict[str, object] = {
            "contract": "ob.owner-retraction-approval.v1",
            "operation_id": request.operation_id,
            "brain_id": request.brain_id,
            "issuer_epoch": request.issuer_epoch,
            "principal_id": engine.profile.owner_actor_id,
            "preview_binding_sha256": sha256(b"synthetic prior preview identifier").hexdigest(),
            "lifecycle_request_sha256": request.request_sha256,
            "impact": {
                "target": {"kind": impact.target_kind, "id": impact.target_id},
                "capture_ids": list(impact.capture_ids),
                "membership_version": impact.membership_version,
                "membership_sha256": impact.membership_sha256,
                "items": [asdict(item) for item in impact.items],
                "counts": dict(impact.counts),
                "preimage_sha256": impact.preimage_sha256,
            },
        }
        terminal = journal.retract(request)
        receipt = make_receipt(approval, cast(str, terminal["receipt_sha256"]))
        record = approval_record(approval, receipt)
        request_raw, receipt_raw = canonical(approval), canonical(receipt)
        if corruption == "request_bytes":
            request_raw += b"\n"
        connection.execute("INSERT INTO retraction_approvals VALUES(?,?,?,?,?)", (
            request.operation_id, request_raw,
            "a" * 64 if corruption == "request_hash" else sha256(request_raw).hexdigest(),
            receipt_raw,
            "a" * 64 if corruption == "receipt_hash" else sha256(receipt_raw).hexdigest(),
        ))
    rederive_live_search_projection(engine)
    return record


def _rehash(root: Path) -> None:
    manifest_path = root / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = [
        {"path": path.relative_to(root).as_posix(), "sha256": sha256(path.read_bytes()).hexdigest()}
        for path in sorted(root.rglob("*")) if path.is_file() and path != manifest_path
    ]
    manifest_path.write_bytes(canonical(manifest))


def test_required_empty_evidence_and_exact_frozen13_composition(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    files = _export(engine, tmp_path / "archive")
    assert json.loads(files[APPROVALS_PATH]) == {"contract": APPROVALS_CONTRACT, "records": []}
    assert PORTABLE_V14_SCHEMA_CATALOG["base_catalog_sha256"] == PORTABLE_V13_SCHEMA_CATALOG_DIGEST
    validate_portable_file_set_v13(
        {path: raw for path, raw in files.items() if path != APPROVALS_PATH},
        tenant_id=engine.profile.tenant_id,
    )
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v13(files, tenant_id=engine.profile.tenant_id)
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v13(tmp_path / "archive")


@pytest.mark.parametrize("corruption", ["request_bytes", "request_hash", "receipt_hash"])
def test_export_refuses_corrupt_retained_approval_before_destination_mutation(
    tmp_path: Path, corruption: str
) -> None:
    engine = _engine(tmp_path / "brain")
    _approve(engine, corruption=corruption)
    with pytest.raises(PortableValidationError, match="retained owner retraction evidence"):
        _export(engine, tmp_path / "archive")
    assert not (tmp_path / "archive").exists()


def test_approval_roundtrip_retains_receipts_and_excludes_operational_previews(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "brain")
    record = _approve(engine)
    # Live preview custody must neither become Portable evidence nor survive restore.
    with engine._store.transaction() as connection:
        journal = _journal(connection)
        connection.execute("INSERT INTO retraction_previews VALUES(?,?,?,?,?,?,?,?)", (
            "retprev_" + "A" * 43, engine.profile.owner_actor_id, journal.brain_id,
            journal.issuer_epoch, "2026-10-15T12:00:00Z", "2026-10-15T13:00:00Z",
            b"{}", sha256(b"{}").hexdigest(),
        ))
    files = _export(engine, tmp_path / "archive")
    assert json.loads(files[APPROVALS_PATH])["records"] == [record]
    assert b"retprev_" not in b"".join(files.values())
    destination = tmp_path / "restored"
    import_id = "import_" + str(uuid4())
    engine.tasks.portability.import_clean(tmp_path / "archive", destination, import_id=import_id)
    assert engine.tasks.portability.import_clean(
        tmp_path / "archive", destination, import_id=import_id
    ).duplicate
    reopened = BrainEngine.open(open_existing_single_user_local(destination))
    with reopened._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM retraction_previews").fetchone()[0] == 0
        row = connection.execute(
            "SELECT request_bytes,receipt_bytes FROM retraction_approvals"
        ).fetchone()
        assert row[0] == canonical(record["request"])
        assert row[1] == canonical(record["receipt"])
        operation = cast(str, cast(dict[str, object], record["request"])["operation_id"])
        assert _journal(connection).status(operation)["receipt_sha256"] == cast(
            dict[str, object], record["receipt"]
        )["journal_receipt_sha256"]
    assert _export(reopened, tmp_path / "again") == files


@pytest.mark.parametrize("tamper", [
    "missing", "contract", "extra", "duplicate-key", "type", "counts", "journal-receipt",
    "orphan", "duplicate-operation", "unsorted", "boolean-receipt-count", "float-receipt-count",
])
def test_rehashed_approval_corruption_refused_before_destination_mutation(
    tmp_path: Path, tamper: str
) -> None:
    engine = _engine(tmp_path / "brain")
    _approve(engine)
    _approve(engine)
    archive = tmp_path / "archive"
    _export(engine, archive)
    path = archive / APPROVALS_PATH
    value = json.loads(path.read_bytes())
    if tamper == "missing":
        path.unlink()
    elif tamper == "duplicate-key":
        path.write_bytes(b'{"contract":"ob.owner-retraction-evidence.v1",' + path.read_bytes()[1:])
    else:
        if tamper == "contract":
            value["contract"] = "ob.owner-retraction-evidence.v2"
        elif tamper == "extra":
            value["previews"] = []
        elif tamper == "type":
            value["records"] = {}
        elif tamper == "duplicate-operation":
            value["records"].append(value["records"][0])
        elif tamper == "unsorted":
            value["records"].reverse()
        elif tamper in {"boolean-receipt-count", "float-receipt-count"}:
            value["records"][0]["receipt"]["counts"]["selected_captures"] = (
                True if tamper == "boolean-receipt-count" else 1.0
            )
        else:
            record = value["records"][0]
            if tamper == "counts":
                record["request"]["impact"]["counts"][COUNTS[0]] += 1
            elif tamper == "orphan":
                record["request"]["operation_id"] = str(uuid4())
                request = LifecycleRequest(
                    record["request"]["operation_id"], "capture_retract",
                    record["request"]["brain_id"], record["request"]["issuer_epoch"],
                    {"capture_id": record["request"]["impact"]["target"]["id"]},
                )
                record["request"]["lifecycle_request_sha256"] = request.request_sha256
            record["receipt"] = (
                make_receipt(record["request"], "a" * 64) if tamper != "counts"
                else {**record["receipt"], "approval_sha256": digest(record["request"])}
            )
        path.write_bytes(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if tamper == "float-receipt-count" else canonical(value)
        )
    _rehash(archive)
    with pytest.raises(PortableValidationError):
        engine.tasks.portability.import_clean(
            archive, tmp_path / "destination", import_id="import_" + str(uuid4())
        )
    assert not (tmp_path / "destination").exists()


def test_frozen13_predecessor_restores_with_empty_owner_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import local_schema

    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 18)
        engine = _engine(tmp_path / "old")
        engine.capture.accept(TextPayload("Synthetic frozen13 retained body."),
                              delivery_id="synthetic.frozen13")
        assert engine.tasks.portability.export(
            tmp_path / "v13", export_id="export_" + str(uuid4())
        ).schema_version == 13
    frozen = validated_portable_snapshot_v13(tmp_path / "v13")
    engine.tasks.portability.import_clean(
        tmp_path / "v13", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    reopened = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    with reopened._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM retraction_approvals").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM retraction_previews").fetchone()[0] == 0
    files = _export(reopened, tmp_path / "v14")
    assert {path: raw for path, raw in files.items() if path != APPROVALS_PATH} == {
        path: raw for path, raw in frozen.files.items() if path != "portable-manifest.json"
    }


def test_owner_exact_replay_survives_issuer_change_and_restore(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "synthetic-portable14-owner", frozenset(), None, owner=True
    )
    capture = engine.capture.accept(TextPayload("Synthetic public retraction roundtrip."),
                                    delivery_id="synthetic.portable14.public")
    preview = engine.lifecycle.preview_retraction(capture_id=capture.capture_id, authority=owner)
    preview_id = cast(str, preview["preview_id"])
    operation = str(uuid4())
    receipt = engine.lifecycle.commit_retraction(preview_id, operation, authority=owner)
    files = _export(engine, tmp_path / "archive")
    # The accepting journal/approval issuer can precede the restored container issuer.
    # Construct that supported Portable boundary without changing immutable live identity.
    issuer = json.loads(files[ISSUER_MIGRATION_PATH])
    issuer["current_issuer_epoch"] += 1
    from open_brain_engine.engine.historical_contracts import HistoricalDestination
    from open_brain_engine.engine.historical_registry import HistoricalClaimRegistry
    from open_brain_engine.portable.v9 import HISTORICAL_AUTHORITY_PATH, historical_authority_bytes

    files[HISTORICAL_AUTHORITY_PATH] = historical_authority_bytes(
        HistoricalClaimRegistry.empty(HistoricalDestination(
            brain_id=issuer["brain_id"], issuer_epoch=issuer["current_issuer_epoch"]
        )), (),
    )
    (tmp_path / "archive" / HISTORICAL_AUTHORITY_PATH).write_bytes(files[HISTORICAL_AUTHORITY_PATH])
    files[ISSUER_MIGRATION_PATH] = canonical(issuer)
    (tmp_path / "archive" / ISSUER_MIGRATION_PATH).write_bytes(files[ISSUER_MIGRATION_PATH])
    _rehash(tmp_path / "archive")
    engine.tasks.portability.import_clean(
        tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
    )
    reopened = BrainEngine.open(open_existing_single_user_local(tmp_path / "restored"))
    assert reopened.lifecycle.retraction_status(operation, authority=owner) == receipt
    assert reopened.lifecycle.commit_retraction(preview_id, operation, authority=owner) == receipt
    assert _export(reopened, tmp_path / "again") == files


@pytest.mark.parametrize("failure_stage", [
    "capture_lifecycle_restored", "owner_retraction_approvals_restored",
])
def test_restore_fault_rolls_back_journal_and_approvals_together(
    tmp_path: Path, failure_stage: str
) -> None:
    engine = _engine(tmp_path / "brain")
    _approve(engine)
    _export(engine, tmp_path / "archive")
    stage = tmp_path / "hidden-stage"
    shutil.copytree(tmp_path / "archive", stage)
    snapshot = validated_portable_snapshot(stage)

    def fail(checkpoint: str) -> None:
        if checkpoint == failure_stage:
            raise RuntimeError("synthetic restore interruption")

    with pytest.raises(RuntimeError, match="synthetic restore interruption"):
        restore_portable_v10_root(
            stage, snapshot=snapshot, expected_root_identity=snapshot.root_identity,
            checkpoint=fail,
        )
    with sqlite3.connect(stage / PHASE1_STATE_DATABASE) as connection:
        assert connection.execute(
            "SELECT count(*) FROM lifecycle_journal_requests"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM retraction_approvals").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM retraction_previews").fetchone()[0] == 0
