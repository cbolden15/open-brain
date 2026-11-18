"""Portable 21 retains browser annotations and their immutable bindings."""

import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import BrainEngine, local_schema, paging
from open_brain_engine.engine.browser_annotations import Annotation, BrowserAnnotations
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages
from open_brain_engine.engine.capture_lifecycle_contracts import LifecycleRequest
from open_brain_engine.engine.contracts import CaptureSubmission, PublicJobCaptureContext
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.saved_item_identity import SavedItemSource
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v20 import validated_portable_snapshot_v20
from open_brain_engine.portable.v21_catalog import PORTABLE_V21_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority
from open_brain_connectors.runtime.saved_item import SavedItemSourceAdapter, parse_record
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

PATH = "history/browser-annotations/annotations-v1.json"
EXPORT_ID = "export_00000000-0000-4000-8000-000000000001"
IMPORT_ID = "import_00000000-0000-4000-8000-000000000002"
WORKFLOW = "00000000-0000-4000-8000-000000000003"
URL = "https://example.test/saved"
TIERS = ("public", "work", "personal")


def _brain(tmp_path: Path) -> BrainEngine:
    tmp_path.chmod(0o700)
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))


def _saved_page(
    engine: BrainEngine,
) -> tuple[SavedItemSource, str, str, str, str]:
    profile = engine.profile
    setup = BrowserOwnerSetup(profile, owner_authority(engine.tasks, session_id="setup"))
    source = setup.create_source()
    token = setup.bind(
        source,
        tiers=TIERS,
        spaces=(None,),
        workflow_principal=WORKFLOW,
        operations=("lookup", "save", "status"),
        complete_domain=True,
    )
    actor = f"actor_{WORKFLOW}"
    context = PublicJobCaptureContext.create(
        profile=profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "role_claim_id": f"role_claim_{WORKFLOW}",
            "role_id": f"role_{WORKFLOW}",
            "tenant_id": profile.tenant_id,
            "capabilities": ["capture.accept"],
        },
    )
    record = parse_record(
        canonical(
            {
                "schema_version": 1,
                "url": URL,
                "why": "Synthetic saved page",
                "received_at": "2026-11-18T12:00:00Z",
                "fetch_status": "not_attempted",
            }
        )
    )
    intake = SavedItemSourceAdapter(SourceResourceSelection(*source.values(), "folder")).intake(
        record
    )
    submission = CaptureSubmission.for_public_job(
        context=context,
        payload=intake.payload(),
        delivery_id=intake.key.delivery_id(),
        source_origin="third_party",
        source_reference=intake.source_reference,
        provenance=intake.provenance(),
        privacy=intake.privacy,
        intent="reference",
    )
    operation = str(uuid4())
    result = BrowserPages(profile).save(token, operation, submission, record.item_key)
    assert result.state == "accepted" and result.capture_id is not None
    return source, token, record.item_key, result.capture_id, operation


def _annotations(
    engine: BrainEngine, token: str, item_key: str, capture_id: str
) -> list[tuple[str, Annotation]]:
    tasks = BrowserAnnotations(engine.profile)
    operations = [
        (str(uuid4()), Annotation(str(uuid4()), "Coppersparrow", "Before", "After", "Burnished")),
        (str(uuid4()), Annotation(str(uuid4()), "Coppersparrow", "Again", "Later")),
        (str(uuid4()), Annotation(str(uuid4()), "Harborsand", "", "", "Shoreline")),
    ]
    for operation_id, annotation in operations:
        result = tasks.add(token, operation_id, item_key, capture_id, annotation)
        assert result.state == "accepted" and result.annotation_id == annotation.annotation_id
    return operations


def _retract(engine: BrainEngine, token: str, capture_id: str) -> None:
    binding = BrowserPages(engine.profile).binding(token)
    engine.lifecycle.retract(
        LifecycleRequest(
            str(uuid4()),
            "capture_retract",
            binding.brain_id,
            binding.issuer_epoch,
            {"capture_id": capture_id},
        ),
        authority=owner_authority(engine.tasks, session_id="retract"),
    )


def _export(engine: BrainEngine, tmp_path: Path, *, version: int = 21) -> Path:
    archive = tmp_path / "archive"
    receipt = engine.tasks.portability.export(archive, export_id=EXPORT_ID)
    assert receipt.schema_version == version
    return archive


def test_export_includes_required_empty_annotation_family(tmp_path: Path) -> None:
    archive = _export(_brain(tmp_path), tmp_path)
    snapshot = validated_portable_snapshot(archive)
    assert json.loads(snapshot.files[PATH])["annotations"] == []
    assert PORTABLE_V21_SCHEMA_CATALOG["migration26_sha256"] == LOCAL_MIGRATIONS[25].checksum
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v20(archive)


@pytest.mark.parametrize("retracted", [False, True])
def test_annotations_restore_replay_projection_and_retraction(
    tmp_path: Path, retracted: bool
) -> None:
    engine = _brain(tmp_path)
    source, token, key, capture_id, _ = _saved_page(engine)
    operations = _annotations(engine, token, key, capture_id)
    if retracted:
        _retract(engine, token, capture_id)
    archive = _export(engine, tmp_path)
    snapshot = validated_portable_snapshot(archive)
    rows = json.loads(snapshot.files[PATH])["annotations"]
    assert {row["annotation_id"] for row in rows} == {
        annotation.annotation_id for _, annotation in operations
    }
    if retracted:
        assert {row["state"] for row in rows} == {"withdrawn"}
        assert not any(
            content in raw
            for content in (b"Coppersparrow", b"Burnished", b"Harborsand", b"Shoreline", b"Before")
            for raw in snapshot.files.values()
        )
    else:
        assert [row["quote"] for row in rows].count("Coppersparrow") == 2
    assert not any(token.encode() in raw for raw in snapshot.files.values())

    destination = tmp_path / "destination"
    receipt = engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert receipt.schema_version == 21
    assert engine.tasks.portability.import_clean(
        archive, destination, import_id=IMPORT_ID
    ).duplicate
    restored = BrainEngine.open(compile_single_user_local(destination))
    annotations = BrowserAnnotations(restored.profile)
    with pytest.raises(PermissionError):
        annotations.list(token, key, capture_id, limit=10, offset=0)
    setup = BrowserOwnerSetup(
        restored.profile, owner_authority(restored.tasks, session_id="new-setup")
    )
    fresh = setup.bind(
        source,
        tiers=TIERS,
        spaces=(None,),
        workflow_principal=WORKFLOW,
        operations=("lookup", "save", "status"),
        complete_domain=True,
    )
    if retracted:
        with pytest.raises(PermissionError):
            annotations.list(fresh, key, capture_id, limit=10, offset=0)
        assert not restored.retrieval.search("Coppersparrow")
        with closing(sqlite3.connect(destination / local_schema.PHASE1_STATE_DATABASE)) as conn:
            scrubbed = conn.execute(
                "SELECT quote,prefix,suffix,note FROM browser_annotations"
            ).fetchall()
            assert scrubbed == [(None, None, None, None)] * 3
    else:
        hits = restored.retrieval.search("Coppersparrow")
        assert hits
        before, cursor = annotations.list(fresh, key, capture_id, limit=10, offset=0)
        assert cursor is None and len(before) == 3
        operation, annotation = operations[0]
        result = annotations.add(fresh, operation, key, capture_id, annotation)
        assert result.annotation_id == annotation.annotation_id
        assert annotations.list(fresh, key, capture_id, limit=10, offset=0) == (before, None)


@pytest.mark.parametrize(
    "defect",
    ["pending", "parent", "cross_operation", "protected", "digest", "withdrawn_text", "duplicate"],
)
def test_malformed_annotation_archive_refuses_before_staging(tmp_path: Path, defect: str) -> None:
    engine = _brain(tmp_path)
    _, token, key, capture_id, save_operation = _saved_page(engine)
    _annotations(engine, token, key, capture_id)
    if defect == "withdrawn_text":
        _retract(engine, token, capture_id)
    archive = _export(engine, tmp_path)
    manifest_path = archive / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(entry for entry in manifest["files"] if entry["path"] == PATH)
    path = archive / PATH
    payload = json.loads(path.read_bytes())
    row = payload["annotations"][0]
    if defect == "pending":
        row["state"] = "pending"
    elif defect == "parent":
        row["capture_id"] = "cap_00000000-0000-4000-8000-000000000006"
    elif defect == "cross_operation":
        row["operation_id"] = save_operation
    elif defect == "protected":
        row["note"] = URL
        request = {
            key: row[key]
            for key in (
                "item_key",
                "capture_id",
                "annotation_id",
                "quote",
                "prefix",
                "suffix",
                "note",
            )
        }
        row["request_sha256"] = sha256(canonical(request)).hexdigest()
    elif defect == "digest":
        row["quote"] = "Synthetic altered anchor"
    elif defect == "withdrawn_text":
        row["note"] = "Synthetic restored leak"
    else:
        payload["annotations"].insert(0, row)
    payload["annotations"].sort(key=lambda row: row["operation_id"])
    raw = canonical(payload)
    path.write_bytes(raw)
    entry["sha256"] = sha256(raw).hexdigest()
    manifest_path.write_bytes(canonical(manifest))
    destination = tmp_path / "destination"
    with pytest.raises(PortableValidationError):
        engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert not destination.exists()


def test_populated_portable20_restores_without_annotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 25)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:25])
        predecessor.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 25)
        engine = _brain(tmp_path)
        _saved_page(engine)
        archive = _export(engine, tmp_path, version=20)
        before = validated_portable_snapshot_v20(archive)
    destination = tmp_path / "destination"
    receipt = engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert receipt.schema_version == 20
    assert validated_portable_snapshot(destination).files == before.files
    with closing(sqlite3.connect(destination / local_schema.PHASE1_STATE_DATABASE)) as conn:
        assert conn.execute("SELECT count(*) FROM browser_operations").fetchone() == (1,)
        assert conn.execute("SELECT * FROM browser_annotations").fetchall() == []


def test_frozen_populated_portable21_fixture_restores(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures/browser-annotations-v21.zip"
    assert sha256(fixture.read_bytes()).hexdigest() == (
        "c9076e3b32f4a27c3f97f27d49dfd976c0a1ac4108f4b70402ea3b705f87671c"
    )
    engine = _brain(tmp_path)
    archive = tmp_path / "fixture"
    archive.mkdir(mode=0o700)
    with ZipFile(fixture) as zipped:
        zipped.extractall(archive)
    snapshot = validated_portable_snapshot(archive)
    annotations = json.loads(snapshot.files[PATH])["annotations"]
    assert len(annotations) == 3
    assert len({entry["capture_id"] for entry in annotations}) == 1
    assert [entry["quote"] for entry in annotations].count("Coppersparrow") == 2

    destination = tmp_path / "restored"
    assert (
        engine.tasks.portability.import_clean(
            archive, destination, import_id=IMPORT_ID
        ).schema_version
        == 21
    )
    restored = BrainEngine.open(compile_single_user_local(destination))
    assert restored.retrieval.search("Harborsand")


def test_preexisting_decomposed_annotation_round_trips_without_mutating_source(
    tmp_path: Path,
) -> None:
    engine = _brain(tmp_path)
    _, token, key, capture_id, _ = _saved_page(engine)
    tasks = BrowserAnnotations(engine.profile)
    annotation, operation = str(uuid4()), str(uuid4())
    tasks.add(token, operation, key, capture_id, Annotation(annotation, "Café", "", ""))
    decomposed = "Cafe\u0301"
    payload = {
        "item_key": key,
        "capture_id": capture_id,
        "annotation_id": annotation,
        "quote": decomposed,
        "prefix": "",
        "suffix": "",
        "note": None,
    }
    old_digest = sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    ).hexdigest()
    state = engine.profile.root / local_schema.PHASE1_STATE_DATABASE
    with closing(sqlite3.connect(state)) as db, db:
        db.execute(
            "UPDATE browser_annotations SET quote=?,request_sha256=? WHERE annotation_id=?",
            (decomposed, old_digest, annotation),
        )
    archive = _export(engine, tmp_path)
    with closing(sqlite3.connect(state)) as db:
        assert db.execute("SELECT quote,request_sha256 FROM browser_annotations").fetchone() == (
            decomposed,
            old_digest,
        )
    destination = tmp_path / "restored"
    assert (
        engine.tasks.portability.import_clean(
            archive, destination, import_id=IMPORT_ID
        ).schema_version
        == 21
    )
    with closing(sqlite3.connect(destination / local_schema.PHASE1_STATE_DATABASE)) as db:
        assert db.execute("SELECT quote FROM browser_annotations").fetchone() == ("Café",)
