"""Schema-eleven lifecycle and admission archive acceptance with synthetic sources."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import BrainEngine, EngineTaskSet, open_local_engine
from open_brain_engine.engine.materializer import _profile
from open_brain_engine.engine.portable_v5_restore import restore_portable_v5_root
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import (
    SourceInspectRequest,
    SourceWithdrawRequest,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v5 import validate_portable_file_set_v5
from open_brain_engine.portable.v6 import (
    SOURCE_ADMISSION_PATH,
    SOURCE_LIFECYCLE_PATH,
    V6_SIDECAR_PATHS,
    validate_portable_file_set_v6,
)
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


def _revision(tasks: EngineTaskSet) -> SourceRevisionSubmission:
    capture = _public_submission(tasks)
    return SourceRevisionSubmission(
        capture=capture,
        namespace=dict(
            connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
        ),
        revision_key="revision-one",
        canonical_sha256=capture.request_sha256(),
        expected_head=None,
        ordering={"kind": "unordered"},
        expected_control_epoch=0,
    )


def test_schema11_runtime6_portable6_lifecycle_admission_roundtrip(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    submission = _revision(tasks)
    accepted = tasks.sources.submit_revision(submission)
    assert accepted.source_id is not None and accepted.capture_id is not None
    owner = EffectiveAuthority("synthetic-owner", "owner-session", frozenset(), None, owner=True)
    inspection = tasks.sources.inspect(
        SourceInspectRequest(source_id=accepted.source_id), authority=owner
    )
    withdrawal = SourceWithdrawRequest(
        operation_id="withdraw.synthetic",
        source_id=accepted.source_id,
        expected_head=accepted.capture_id,
        expected_lifecycle_version=inspection.lifecycle_version,
        brain_id=inspection.destination_brain_id,
        issuer_epoch=inspection.issuer_epoch,
        reason_code="owner_choice",
    )
    receipt = tasks.sources.withdraw(withdrawal, authority=owner)
    export, restored, again = (tmp_path / name for name in ("export", "restored", "again"))
    assert tasks.portability.export(export, export_id="export_" + str(uuid4())).schema_version == 6
    snapshot = validated_portable_snapshot(export)
    assert snapshot.manifest["schema_version"] == 6
    assert snapshot.files.keys() >= V6_SIDECAR_PATHS
    assert (
        tasks.portability.import_clean(
            export, restored, import_id="import_" + str(uuid4())
        ).schema_version
        == 6
    )
    engine = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))
    assert engine.sources.withdraw(withdrawal, authority=owner) == receipt
    assert engine.sources.submit_revision(submission) == accepted
    assert (
        engine.sources.inspect(
            SourceInspectRequest(source_id=accepted.source_id), authority=owner
        ).lifecycle
        == "retired"
    )
    engine.portability.export(again, export_id="export_" + str(uuid4()))
    second = validated_portable_snapshot(again)
    assert all(second.files[path] == snapshot.files[path] for path in V6_SIDECAR_PATHS)
    for path, data in snapshot.files.items():
        if path != "portable-manifest.json":
            assert second.files[path] == data


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "bool-version", "foreign-source", "revision-missing", "namespace-digest"],
)
def test_portable6_closed_authority_refuses_semantic_tampering(
    tmp_path: Path, mutation: str
) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    tasks.sources.submit_revision(_revision(tasks))
    export = tmp_path / "export"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    files = {
        path: data for path, data in snapshot.files.items() if path != "portable-manifest.json"
    }
    path = (
        SOURCE_ADMISSION_PATH
        if mutation in {"revision-missing", "namespace-digest"}
        else SOURCE_LIFECYCLE_PATH
    )
    value = json.loads(files[path])
    if mutation == "missing":
        files.pop(path)
    else:
        if mutation == "extra":
            value["unexpected"] = True
        elif mutation == "bool-version":
            value["source_lifecycle_state"][0]["lifecycle_version"] = False
        elif mutation == "foreign-source":
            value["source_lifecycle_state"][0]["source_id"] = "source_foreign"
        elif mutation == "revision-missing":
            value["revision_admission"] = []
        elif mutation == "namespace-digest":
            value["source_namespaces"][0]["namespace_sha256"] = "0" * 64
        files[path] = canonical(value)
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v6(files, tenant_id=tasks.profile.tenant_id)


def test_portable5_cannot_silently_accept_source_authority(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    export = tmp_path / "export"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    files = {
        path: data
        for path, data in validated_portable_snapshot(export).files.items()
        if path != "portable-manifest.json"
    }
    with pytest.raises(PortableValidationError):
        validate_portable_file_set_v5(files, tenant_id=tasks.profile.tenant_id)


def test_portable6_import_retry_audits_lifecycle_authority(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    tasks.sources.submit_revision(_revision(tasks))
    export, restored = tmp_path / "export", tmp_path / "restored"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    import_id = "import_" + str(uuid4())
    tasks.portability.import_clean(export, restored, import_id=import_id)
    assert tasks.portability.import_clean(export, restored, import_id=import_id).duplicate
    engine = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))
    with engine._store.transaction() as connection:
        connection.execute("UPDATE source_lifecycle_state SET lifecycle_version=1")
    with pytest.raises(ValueError, match="source authority mismatch"):
        tasks.portability.import_clean(export, restored, import_id=import_id)


def test_portable6_managed_delivery_exact_replay_after_clean_restore(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    tasks = open_local_engine(profile)
    submission = _revision(tasks)
    with engine._store.connect() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    binding = SourceRevisionBinding(
        destination_brain_id=identity["brain_id"],
        issuer_epoch=identity["issuer_epoch"],
        root_fingerprint="synthetic-root",
        accepted_source_id="synthetic-selection",
        namespace=submission.namespace,
    )
    delivery = SourceRevisionDelivery(
        binding=binding,
        submission=submission,
        expected_lifecycle_version=0,
        delivery_id="synthetic.delivery.one",
    )
    accepted = engine.sources.public_revision_sink(binding).submit(delivery)
    export, restored = tmp_path / "export", tmp_path / "restored"
    engine.portability.export(export, export_id="export_" + str(uuid4()))
    snapshot = validated_portable_snapshot(export)
    admission = json.loads(snapshot.files[SOURCE_ADMISSION_PATH])
    assert len(admission["managed_source_deliveries"]) == 1
    engine.portability.import_clean(export, restored, import_id="import_" + str(uuid4()))
    reopened = BrainEngine.open(_profile(restored, validated_portable_snapshot(restored)))
    assert reopened.sources.public_revision_sink(binding).submit(delivery) == accepted


def test_portable6_pending_managed_envelope_refuses_export(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    with engine._store.transaction() as connection:
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        connection.execute(
            "INSERT INTO managed_source_deliveries("
            "delivery_id,envelope_sha256,envelope_bytes,source_id,destination_brain_id,issuer_epoch,"
            "expected_head,expected_lifecycle_version,source_delivery_id,receipt_json) "
            "VALUES(?,?,?,NULL,?,?,NULL,0,?,NULL)",
            (
                "synthetic.pending",
                "0" * 64,
                b"{}",
                identity["brain_id"],
                identity["issuer_epoch"],
                "synthetic.source.delivery",
            ),
        )
    with pytest.raises(ValueError, match="^ingestion_pending$"):
        engine.portability.export(tmp_path / "refused", export_id="export_" + str(uuid4()))
    assert not (tmp_path / "refused").exists()


@pytest.mark.parametrize(
    "stage",
    [
        "base_materialized",
        "source_history_restored",
        "privacy_evidence_restored",
        "search_rederived",
    ],
)
def test_portable6_restore_crash_rolls_back_authority(tmp_path: Path, stage: str) -> None:
    tmp_path.chmod(0o700)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    assert tasks.sources is not None
    submission = _revision(tasks)
    accepted = tasks.sources.submit_revision(submission)
    export, restore = tmp_path / "export", tmp_path / "restore"
    tasks.portability.export(export, export_id="export_" + str(uuid4()))
    shutil.copytree(export, restore)
    snapshot = validated_portable_snapshot(restore)

    def crash(actual: str) -> None:
        if actual == stage:
            raise RuntimeError("synthetic restore crash")

    with pytest.raises(RuntimeError, match="synthetic restore crash"):
        restore_portable_v5_root(
            restore,
            snapshot=snapshot,
            expected_root_identity=snapshot.root_identity,
            checkpoint=crash,
        )
    with sqlite3.connect(restore / ".open-brain/state/phase1.sqlite3") as connection:
        for table in (
            "captures",
            "logical_sources",
            "source_lifecycle_state",
            "source_intakes",
            "source_namespaces",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # The orchestrator retries into a fresh hidden stage. It never reseeds an
    # existing SQLite issuer identity, even when its content transaction rolled back.
    retry = tmp_path / "retry"
    shutil.copytree(export, retry)
    retry_snapshot = validated_portable_snapshot(retry)
    restored = restore_portable_v5_root(
        retry, snapshot=retry_snapshot, expected_root_identity=retry_snapshot.root_identity
    )
    engine = BrainEngine.open(restored.profile)
    assert engine.sources.submit_revision(submission) == accepted
