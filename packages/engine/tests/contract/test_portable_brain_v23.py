"""Portable recovery for browser filing of folder-saved pages."""

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
from open_brain_engine.engine.browser_filing import BrowserFiling
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup, BrowserPages
from open_brain_engine.engine.browser_recovery import list_intents
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.saved_item_identity import SavedItemSource
from open_brain_engine.portable.browser_filing_v23 import FILING_WITNESS_COLUMNS
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v23_catalog import PORTABLE_V23_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority
from open_brain_collector.lifecycle import EngineCaptureSink
from open_brain_collector.runner import collector_capture_sink
from open_brain_connectors.runtime.saved_item import SavedItemSourceAdapter, parse_record
from open_brain_connectors.runtime.source_registry import SourceResourceSelection
from packages.engine.tests.contract.test_portable_brain_v21 import _brain, _retract
from packages.engine.tests.contract.test_portable_brain_v22 import EXPORT_ID, IMPORT_ID, _bind

PATH = "history/browser-filing/witnesses-v1.json"


def test_filing_witness_family_is_present_without_browser_setup(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    archive = tmp_path / "archive"
    receipt = engine.tasks.portability.export(archive, export_id=EXPORT_ID)
    assert receipt.schema_version == 23
    assert json.loads(validated_portable_snapshot(archive).files[PATH])["witnesses"] == []


def _file_folder_page(engine: BrainEngine) -> tuple[SavedItemSource, str, str, str]:
    setup = BrowserOwnerSetup(engine.profile, owner_authority(engine.tasks, session_id="setup"))
    source = setup.create_source()
    grant = _bind(engine, source)
    record = parse_record(
        canonical(
            {
                "schema_version": 1,
                "url": "https://example.test/folder-page#fragment-only-in-producer",
                "why": "Synthetic folder intake",
                "received_at": "2026-11-18T12:00:00Z",
                "fetch_status": "not_attempted",
            }
        )
    )
    intake = SavedItemSourceAdapter(SourceResourceSelection(*source.values(), "folder")).intake(
        record
    )
    EngineCaptureSink(
        collector_capture_sink(engine.profile.root, selected=(source, record.item_key))
    ).submit(intake)
    pages = BrowserPages(engine.profile)
    parent = pages.lookup(grant, record.item_key)
    assert parent is not None
    target = engine.inbox.create_space("Target", delivery_id="target").space_id
    operation = str(uuid4())
    result = BrowserFiling(engine.profile).file(
        grant, operation, record.item_key, parent.capture_id, target
    )
    assert result.state == "accepted" and result.space_id == target
    return source, grant, operation, parent.capture_id


@pytest.mark.parametrize("withdrawn", [False, True])
def test_filing_witness_only_recovers_for_fresh_grant_on_original_scope(
    tmp_path: Path, withdrawn: bool
) -> None:
    engine = _brain(tmp_path)
    source, grant, operation, capture_id = _file_folder_page(engine)
    setup = BrowserOwnerSetup(engine.profile, owner_authority(engine.tasks, session_id="cleanup"))
    other = setup.create_source()
    if withdrawn:
        _retract(engine, grant, capture_id)
    setup.revoke(grant)
    archive = tmp_path / "archive"
    engine.tasks.portability.export(archive, export_id=EXPORT_ID)
    snapshot = validated_portable_snapshot(archive)
    witnesses = json.loads(snapshot.files[PATH])["witnesses"]
    assert len(witnesses) == int(not withdrawn)
    assert b"fragment-only-in-producer" not in snapshot.files[PATH]
    assert grant.encode() not in snapshot.files[PATH]
    if witnesses:
        assert set(witnesses[0]) == set(FILING_WITNESS_COLUMNS)
        assert witnesses[0]["connector_name"] == source.connector_name
    destination = tmp_path / "restored"
    engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    restored = BrainEngine.open(compile_single_user_local(destination))
    pages = BrowserPages(restored.profile)
    with pytest.raises(PermissionError):
        pages.status(grant, operation)
    fresh = _bind(restored, source)
    assert pages.status(fresh, operation).state == ("refused" if withdrawn else "accepted")
    assert list_intents(pages, fresh, None, 20)["intents"] == (
        [] if withdrawn else [{"id": operation, "state": "accepted"}]
    )
    assert pages.status(_bind(restored, other), operation).state == "refused"
    assert list_intents(pages, _bind(restored, other), None, 20)["intents"] == []
    restored_setup = BrowserOwnerSetup(
        restored.profile, owner_authority(restored.tasks, session_id="setup")
    )
    other_principal = restored_setup.bind(
        source,
        tiers=("public", "work", "personal"),
        spaces=(None,),
        workflow_principal=str(uuid4()),
        operations=("lookup", "save", "status"),
        complete_domain=True,
    )
    assert pages.status(other_principal, operation).state == "refused"
    assert list_intents(pages, other_principal, None, 20)["intents"] == []
    after = tmp_path / "after"
    restored.tasks.portability.export(after, export_id="export_" + str(uuid4()))
    assert validated_portable_snapshot(after).files[PATH] == snapshot.files[PATH]


def test_schema27_migration_recovers_populated_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 27)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:27])
        old.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 27)
        engine = _brain(tmp_path)
        _, grant, _, _ = _file_folder_page(engine)
    coordinate_local_migration(engine.profile)
    setup = BrowserOwnerSetup(engine.profile, owner_authority(engine.tasks, session_id="cleanup"))
    setup.revoke(grant)
    archive = tmp_path / "archive"
    engine.tasks.portability.export(archive, export_id=EXPORT_ID)
    rows = json.loads(validated_portable_snapshot(archive).files[PATH])["witnesses"]
    assert len(rows) == 1
    assert PORTABLE_V23_SCHEMA_CATALOG["migration28_sha256"] == LOCAL_MIGRATIONS[27].checksum


@pytest.mark.parametrize("kind", ["source", "principal", "authority", "duplicate", "item"])
def test_invalid_filing_witness_refuses_before_destination_setup(tmp_path: Path, kind: str) -> None:
    engine = _brain(tmp_path)
    _file_folder_page(engine)
    setup = BrowserOwnerSetup(engine.profile, owner_authority(engine.tasks, session_id="setup"))
    other = setup.create_source()
    archive = tmp_path / "archive"
    engine.tasks.portability.export(archive, export_id=EXPORT_ID)
    sidecar = archive / PATH
    value = json.loads(sidecar.read_bytes())
    witness = value["witnesses"][0]
    if kind == "source":
        witness["connection_id"], witness["resource_id"] = other.values()[1:]
        witness["delivery_id"] = other.delivery_id(witness["item_key"])
    elif kind == "principal":
        witness["workflow_principal"] = str(uuid4())
    elif kind == "authority":
        witness["token"] = "synthetic-forged-token"
    elif kind == "item":
        witness["item_key"] = "0" * 32
    else:
        value["witnesses"].append(witness.copy())
    raw = canonical(value)
    sidecar.write_bytes(raw)
    manifest_path = archive / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    next(entry for entry in manifest["files"] if entry["path"] == PATH)["sha256"] = sha256(
        raw
    ).hexdigest()
    manifest_path.write_bytes(canonical(manifest))
    destination = tmp_path / "restored"
    with pytest.raises(PortableValidationError):
        engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert not destination.exists()


def test_old_frozen_archive_restores_to_schema28_with_empty_filing_evidence(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures/browser-revisits-v22.zip"
    archive = tmp_path / "fixture"
    archive.mkdir(mode=0o700)
    with ZipFile(fixture) as zipped:
        zipped.extractall(archive)
    snapshot = validated_portable_snapshot(archive)
    assert snapshot.manifest["schema_version"] == 22 and PATH not in snapshot.files
    engine = _brain(tmp_path)
    destination = tmp_path / "restored"
    assert (
        engine.tasks.portability.import_clean(
            archive, destination, import_id=IMPORT_ID
        ).schema_version
        == 22
    )
    with closing(sqlite3.connect(destination / local_schema.PHASE1_STATE_DATABASE)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (28,)
        assert db.execute("SELECT * FROM browser_filing_witnesses").fetchall() == []
        assert db.execute("SELECT count(*) FROM browser_revisits").fetchone()[0] > 0
    assert validated_portable_snapshot(destination).files == snapshot.files
