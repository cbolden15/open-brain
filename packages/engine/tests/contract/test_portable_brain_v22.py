"""Portable 22 preserves calendar-day revisit intents without installation authority."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import BrainEngine, local_schema, paging
from open_brain_engine.engine.browser_pages import BrowserOwnerSetup
from open_brain_engine.engine.browser_revisits import BrowserRevisits
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.saved_item_identity import SavedItemSource
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v21 import validated_portable_snapshot_v21
from open_brain_engine.portable.v22_catalog import PORTABLE_V22_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority
from packages.engine.tests.contract.test_portable_brain_v21 import (
    TIERS,
    WORKFLOW,
    _annotations,
    _brain,
    _retract,
    _saved_page,
)

PATH = "history/browser-revisits/revisits-v1.json"
EXPORT_ID = "export_00000000-0000-4000-8000-000000000001"
IMPORT_ID = "import_00000000-0000-4000-8000-000000000002"
DATE = "2026-11-01"
ZONE = "America/New_York"


def _export(engine: BrainEngine, tmp_path: Path) -> Path:
    archive = tmp_path / "archive"
    assert engine.tasks.portability.export(archive, export_id=EXPORT_ID).schema_version == 22
    return archive


def _restore(engine: BrainEngine, archive: Path, destination: Path) -> BrainEngine:
    receipt = engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert receipt.schema_version == 22
    assert engine.tasks.portability.import_clean(
        archive, destination, import_id=IMPORT_ID
    ).duplicate
    return BrainEngine.open(compile_single_user_local(destination))


def _bind(restored: BrainEngine, source: SavedItemSource) -> str:
    setup = BrowserOwnerSetup(
        restored.profile, owner_authority(restored.tasks, session_id="new-setup")
    )
    return setup.bind(
        source,
        tiers=TIERS,
        spaces=(None,),
        workflow_principal=WORKFLOW,
        operations=("lookup", "save", "status"),
        complete_domain=True,
    )


def test_empty_revisit_family_is_required(tmp_path: Path) -> None:
    archive = _export(_brain(tmp_path), tmp_path)
    assert json.loads(validated_portable_snapshot(archive).files[PATH])["revisits"] == []
    assert PORTABLE_V22_SCHEMA_CATALOG["migration27_sha256"] == LOCAL_MIGRATIONS[26].checksum
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v21(archive)


@pytest.mark.parametrize("withdrawn", [False, True])
def test_revisit_sequence_due_boundary_and_replay_survive_restore(
    tmp_path: Path, withdrawn: bool
) -> None:
    engine = _brain(tmp_path)
    source, grant, key, capture_id, _ = _saved_page(engine)
    tasks = BrowserRevisits(engine.profile)
    first, dismiss, final = (str(uuid4()) for _ in range(3))
    assert (
        tasks.mutate(grant, first, key, capture_id, "set", local_date="2026-10-31", zone=ZONE).state
        == "accepted"
    )
    assert tasks.mutate(grant, dismiss, key, capture_id, "dismiss").state == "accepted"
    assert (
        tasks.mutate(grant, final, key, capture_id, "set", local_date=DATE, zone=ZONE).state
        == "accepted"
    )
    if withdrawn:
        _retract(engine, grant, capture_id)
    archive = _export(engine, tmp_path)
    entries = json.loads(validated_portable_snapshot(archive).files[PATH])["revisits"]
    assert [row["operation_id"] for row in entries] == [first, dismiss, final]
    if withdrawn:
        assert all(
            row["state"] == "withdrawn" and row["local_date"] is None and row["zone"] is None
            for row in entries
        )
    restored = _restore(engine, archive, tmp_path / "restored")
    revisits = BrowserRevisits(restored.profile)
    at_due = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    with pytest.raises(PermissionError):
        revisits.due(grant, now=at_due, limit=10, offset=0)
    fresh = _bind(restored, source)
    if withdrawn:
        assert revisits.due(fresh, now=at_due, limit=10, offset=0)["revisits"] == []
        with pytest.raises(PermissionError):
            revisits.get(fresh, key, capture_id)
        return
    assert (
        revisits.due(fresh, now=datetime(2026, 11, 1, 3, 59, tzinfo=UTC), limit=10, offset=0)[
            "revisits"
        ]
        == []
    )
    assert revisits.due(fresh, now=at_due, limit=10, offset=0)["revisits"] == [
        {"capture_id": capture_id, "local_date": DATE, "zone": ZONE}
    ]
    assert (
        revisits.mutate(
            fresh, first, key, capture_id, "set", local_date="2026-10-31", zone=ZONE
        ).state
        == "accepted"
    )
    assert revisits.get(fresh, key, capture_id)["revisit"] == {
        "local_date": DATE,
        "zone": ZONE,
        "state": "scheduled",
    }
    with closing(sqlite3.connect(restored.profile.root / local_schema.PHASE1_STATE_DATABASE)) as db:
        assert db.execute("SELECT count(*) FROM browser_revisits").fetchone() == (3,)


def _replace_revisit(archive: Path, changes: dict[str, object]) -> None:
    manifest_path = archive / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    sidecar = archive / PATH
    value = json.loads(sidecar.read_bytes())
    row = value["revisits"][0]
    row.update(changes)
    request = {
        name: row[name] for name in ("action", "item_key", "capture_id", "local_date", "zone")
    }
    row["request_sha256"] = sha256(
        json.dumps(request, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()
    raw = canonical(value)
    sidecar.write_bytes(raw)
    entry = next(entry for entry in manifest["files"] if entry["path"] == PATH)
    entry["sha256"] = sha256(raw).hexdigest()
    manifest_path.write_bytes(canonical(manifest))


@pytest.mark.parametrize(
    "changes",
    [
        {"zone": "America/../Unavailable"},
        {"local_date": "2026-11-31"},
        {"state": "pending"},
        {"sequence": 0},
        {"capture_id": "cap_" + "0" * 64},
    ],
)
def test_invalid_revisit_evidence_refuses_before_staging(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    engine = _brain(tmp_path)
    _, grant, key, capture_id, _ = _saved_page(engine)
    BrowserRevisits(engine.profile).mutate(
        grant, str(uuid4()), key, capture_id, "set", local_date=DATE, zone=ZONE
    )
    archive = _export(engine, tmp_path)
    _replace_revisit(archive, changes)
    destination = tmp_path / "restored"
    with pytest.raises(PortableValidationError):
        engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert not destination.exists()


def test_unknown_but_valid_zone_survives_restore_and_needs_review(tmp_path: Path) -> None:
    engine = _brain(tmp_path)
    source, grant, key, capture_id, _ = _saved_page(engine)
    operation = str(uuid4())
    BrowserRevisits(engine.profile).mutate(
        grant, operation, key, capture_id, "set", local_date=DATE, zone=ZONE
    )
    archive = _export(engine, tmp_path)
    missing = "Future/Unavailable"
    _replace_revisit(archive, {"zone": missing})
    restored = _restore(engine, archive, tmp_path / "restored")
    reauthorized = _bind(restored, source)
    revisits = BrowserRevisits(restored.profile)
    assert revisits.get(reauthorized, key, capture_id) == {
        "revisit": {"local_date": DATE, "zone": missing, "state": "zone_unavailable"}
    }
    assert (
        revisits.due(reauthorized, now=datetime(2026, 11, 3, tzinfo=UTC), limit=10, offset=0)[
            "revisits"
        ]
        == []
    )
    assert (
        revisits.mutate(
            reauthorized, operation, key, capture_id, "set", local_date=DATE, zone=missing
        ).state
        == "accepted"
    )
    with pytest.raises(ValueError, match="unavailable revisit zone"):
        revisits.mutate(
            reauthorized, str(uuid4()), key, capture_id, "set", local_date=DATE, zone=missing
        )


def test_populated_schema26_upgrade_preserves_annotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 26)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:26])
        predecessor.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 26)
        engine = _brain(tmp_path)
        _, grant, key, capture_id, _ = _saved_page(engine)
        _annotations(engine, grant, key, capture_id)
        db_path = engine.profile.root / local_schema.PHASE1_STATE_DATABASE
        with closing(sqlite3.connect(db_path)) as db:
            before = db.execute(
                "SELECT * FROM browser_annotations ORDER BY annotation_id"
            ).fetchall()
    coordinate_local_migration(engine.profile)
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (27,)
        assert db.execute("SELECT * FROM browser_revisits").fetchall() == []
        assert (
            db.execute("SELECT * FROM browser_annotations ORDER BY annotation_id").fetchall()
            == before
        )


def test_frozen_populated_portable21_opens_with_empty_revisits(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures/browser-annotations-v21.zip"
    engine = _brain(tmp_path)
    archive = tmp_path / "fixture"
    archive.mkdir(mode=0o700)
    with ZipFile(fixture) as zipped:
        zipped.extractall(archive)
    before = validated_portable_snapshot_v21(archive)
    destination = tmp_path / "restored"
    assert (
        engine.tasks.portability.import_clean(
            archive, destination, import_id=IMPORT_ID
        ).schema_version
        == 21
    )
    assert validated_portable_snapshot(destination).files == before.files
    with closing(sqlite3.connect(destination / local_schema.PHASE1_STATE_DATABASE)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (27,)
        assert db.execute("SELECT count(*) FROM browser_annotations").fetchone() == (3,)
        assert db.execute("SELECT * FROM browser_revisits").fetchall() == []


def test_frozen_populated_portable22_retains_due_revisit(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures/browser-revisits-v22.zip"
    assert sha256(fixture.read_bytes()).hexdigest() == (
        "d25c63f6d5dce865990ab38b7f178d115c59985911fbad9478909a28dd235741"
    )
    engine = _brain(tmp_path)
    archive = tmp_path / "fixture"
    archive.mkdir(mode=0o700)
    with ZipFile(fixture) as zipped:
        zipped.extractall(archive)
    snapshot = validated_portable_snapshot(archive)
    (row,) = json.loads(snapshot.files[PATH])["revisits"]
    source = SavedItemSource(row["connector_name"], row["connection_id"], row["resource_id"])
    restored = _restore(engine, archive, tmp_path / "restored")
    grant = _bind(restored, source)
    assert BrowserRevisits(restored.profile).due(
        grant, now=datetime(2026, 11, 1, 4, 0, tzinfo=UTC), limit=10, offset=0
    )["revisits"] == [{"capture_id": row["capture_id"], "local_date": DATE, "zone": ZONE}]
    assert restored.retrieval.search("Coppersparrow")
