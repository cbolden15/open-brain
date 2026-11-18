"""Portable 20 preserves opaque saved-item admission and browser operation evidence."""

import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import BrainEngine, TextPayload, local_schema, paging
from open_brain_engine.engine.contracts import CaptureAction, CaptureSubmission
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.saved_item_identity import SavedItemSource
from open_brain_engine.engine.saved_item_schema import BROWSER_OPERATION_MIGRATION
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v19 import validated_portable_snapshot_v19
from open_brain_engine.portable.v20_catalog import PORTABLE_V20_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority

PATH = "history/saved-item-admission/admissions-v1.json"
SOURCE = SavedItemSource("saved_item", "folder:" + "a" * 32, "items:" + "b" * 32)
OTHER_SOURCE = SavedItemSource("saved_item", "folder:" + "a" * 32, "items:" + "c" * 32)
ITEM_KEY = "1" * 32
DELIVERY = SOURCE.delivery_id(ITEM_KEY)
EXPORT_ID = "export_00000000-0000-4000-8000-000000000001"
IMPORT_ID = "import_00000000-0000-4000-8000-000000000002"


def _export(
    tmp_path: Path, *, scenario: str = "accepted", version: int = 20
) -> tuple[BrainEngine, Path]:
    tmp_path.chmod(0o700)
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    engine = BrainEngine.open(profile)
    if scenario != "empty":
        payload = TextPayload("Synthetic page with saved evidence")
        refused = scenario in {"refused", "discarded"}
        space = (
            engine.inbox.create_space("Temporary queue destination", delivery_id="fixture.space")
            if refused
            else None
        )
        if scenario == "accepted":
            engine.capture.accept(payload, delivery_id=DELIVERY)
            custody = None
        else:
            custody = engine.ingestion.enqueue(
                CaptureSubmission.for_local_owner(
                    profile=profile,
                    payload=payload,
                    delivery_id=DELIVERY,
                    action=(
                        CaptureAction.CANONICAL_NOTE if space is not None else CaptureAction.QUICK
                    ),
                    space_id=None if space is None else space.space_id,
                )
            )
        with closing(sqlite3.connect(profile.root / PHASE1_STATE_DATABASE)) as conn, conn:
            SOURCE.register(conn)
            OTHER_SOURCE.register(conn)
            brain_id = conn.execute("SELECT brain_id FROM brain_identity").fetchone()[0]
            digest = (
                custody.request_sha256
                if custody is not None
                else conn.execute(
                    "SELECT request_sha256 FROM captures WHERE delivery_id=?", (DELIVERY,)
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO saved_item_admissions VALUES (?,?,?)", (DELIVERY, "browser", digest)
            )
            conn.execute(
                "INSERT INTO browser_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    brain_id,
                    "00000000-0000-4000-8000-000000000003",
                    *SOURCE.values(),
                    "00000000-0000-4000-8000-000000000004",
                    "page.save",
                    "3" * 64,
                    "accepted" if custody is None else "pending",
                    DELIVERY,
                    ITEM_KEY,
                ),
            )
        if space is not None:
            with closing(sqlite3.connect(profile.root / PHASE1_STATE_DATABASE)) as conn, conn:
                previous = conn.execute(
                    "SELECT * FROM spaces WHERE space_id=?", (space.space_id,)
                ).fetchone()
                conn.execute("DELETE FROM spaces WHERE space_id=?", (space.space_id,))
            assert engine.recover() == 0
            assert engine.ingestion.status()[0].state == "quarantined"
            if scenario == "discarded":
                journal = engine.tasks.journal
                assert journal is not None
                journal.discard(
                    DELIVERY,
                    reason="owner_requested",
                    authority=owner_authority(engine.tasks, session_id="portable-browser-test"),
                )
            with closing(sqlite3.connect(profile.root / PHASE1_STATE_DATABASE)) as conn, conn:
                conn.execute(
                    f"INSERT INTO spaces VALUES ({','.join('?' for _ in previous)})", previous
                )
    archive = tmp_path / "archive"
    assert engine.tasks.portability.export(archive, export_id=EXPORT_ID).schema_version == version
    return engine, archive


@pytest.mark.parametrize("scenario", ["empty", "accepted", "pending", "refused", "discarded"])
def test_browser_operation_archive_round_trip(tmp_path: Path, scenario: str) -> None:
    engine, archive = _export(tmp_path, scenario=scenario)
    snapshot = validated_portable_snapshot(archive)
    assert PORTABLE_V20_SCHEMA_CATALOG["migration25_sha256"] == BROWSER_OPERATION_MIGRATION.checksum
    payload = json.loads(snapshot.files[PATH])
    assert (
        len(payload["browser_operations"])
        == len(payload["saved_item_admissions"])
        == (scenario != "empty")
    )
    if scenario != "empty":
        expected_state = "refused" if scenario == "discarded" else scenario
        assert payload["browser_operations"][0]["state"] == expected_state
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v19(archive)
    destination = tmp_path / "destination"
    result = engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert result.schema_version == 20
    assert engine.tasks.portability.import_clean(
        archive, destination, import_id=IMPORT_ID
    ).duplicate
    with closing(sqlite3.connect(destination / PHASE1_STATE_DATABASE)) as connection:
        connection.row_factory = sqlite3.Row
        for table in ("browser_operations", "saved_item_admissions"):
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            assert rows == payload[table]
        assert not connection.execute("SELECT 1 FROM browser_grants").fetchall()
        assert not connection.execute("SELECT 1 FROM browser_grant_bindings").fetchall()


@pytest.mark.parametrize(
    "defect", ["destination", "source", "authority", "delivery", "digest", "duplicate", "missing"]
)
def test_invalid_browser_operation_bindings_refuse_restore(tmp_path: Path, defect: str) -> None:
    engine, archive = _export(tmp_path)
    manifest_path = archive / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(entry for entry in manifest["files"] if entry["path"] == PATH)
    body = archive / PATH
    contents = json.loads(body.read_bytes())
    row = contents["browser_operations"][0]
    if defect == "destination":
        row["brain_id"] = "brn_a" + "1" * 25
    elif defect == "source":
        row["resource_id"] = OTHER_SOURCE.resource_id
    elif defect == "authority":
        row["retry_credential"] = "private"
    elif defect == "delivery":
        row["delivery_id"] = SOURCE.delivery_id("4" * 32)
    elif defect == "digest":
        contents["saved_item_admissions"][0]["request_sha256"] = "7" * 64
    elif defect == "duplicate":
        contents["browser_operations"].append(row)
    if defect == "missing":
        body.unlink()
        manifest["files"].remove(entry)
    else:
        raw = canonical(contents)
        body.write_bytes(raw)
        entry["sha256"] = sha256(raw).hexdigest()
    manifest_path.write_bytes(canonical(manifest))

    destination = tmp_path / "destination"
    with pytest.raises(PortableValidationError):
        engine.tasks.portability.import_clean(archive, destination, import_id=IMPORT_ID)
    assert not destination.exists()


def test_portable19_restores_without_browser_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 24)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:24])
        predecessor.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 24)
        engine, archive = _export(tmp_path, scenario="empty", version=19)
    destination = tmp_path / "destination"
    assert (
        engine.tasks.portability.import_clean(
            archive, destination, import_id=IMPORT_ID
        ).schema_version
        == 19
    )
    with closing(sqlite3.connect(destination / PHASE1_STATE_DATABASE)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (25,)
        assert connection.execute("SELECT * FROM saved_item_admissions").fetchall() == []
        assert connection.execute("SELECT * FROM browser_operations").fetchall() == []
