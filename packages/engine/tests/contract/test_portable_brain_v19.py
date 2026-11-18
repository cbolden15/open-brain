"""Portable 19 carries saved-item source tuples without installation authority."""

import json
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine import local_schema, paging
from open_brain_engine.engine.contracts import PortabilityFault
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.portability import restore_portable_clean
from open_brain_engine.engine.saved_item_schema import SAVED_ITEM_MIGRATION
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v18 import validated_portable_snapshot_v18
from open_brain_engine.portable.v19_catalog import PORTABLE_V19_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local
from open_brain.services.t03_adapters import owner_authority

PATH = "history/saved-item-sources/sources-v1.json"
SOURCE = ("saved_item", "folder:" + "a" * 32, "items:" + "b" * 32)
OTHER_SOURCE = ("saved_item", SOURCE[1], "items:" + "c" * 32)
EXPORT_ID = "export_00000000-0000-4000-8000-000000000001"
IMPORT_ID = "import_00000000-0000-4000-8000-000000000002"

@pytest.fixture(autouse=True)
def _portable19_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Portable19 producer and original clean-restore recipe frozen at schema24."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 24)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:24])
    monkeypatch.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 24)



def _export(tmp_path: Path, *, populate: bool = True) -> tuple[BrainEngine, Path, str]:
    tmp_path.chmod(0o700)
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    engine = BrainEngine.open(profile)
    with closing(sqlite3.connect(profile.root / PHASE1_STATE_DATABASE)) as connection, connection:
        if populate:
            connection.executemany(
                "INSERT INTO saved_item_sources VALUES (?,?,?)", (OTHER_SOURCE, SOURCE)
            )
        generation = connection.execute("SELECT generation FROM browser_installation").fetchone()[0]
    archive = tmp_path / "archive"
    receipt = engine.tasks.portability.export(archive, export_id=EXPORT_ID)
    assert receipt.schema_version == 19
    return engine, archive, generation


@pytest.mark.parametrize("populate", [False, True])
def test_portable_saved_item_sources_restore_and_retry(tmp_path: Path, populate: bool) -> None:
    engine, archive, original_generation = _export(tmp_path, populate=populate)
    snapshot = validated_portable_snapshot(archive)
    assert PORTABLE_V19_SCHEMA_CATALOG["migration24_sha256"] == SAVED_ITEM_MIGRATION.checksum
    assert json.loads(snapshot.files[PATH])["sources"] == (
        [
            dict(zip(("connector_name", "connection_id", "resource_id"), source, strict=True))
            for source in (SOURCE, OTHER_SOURCE)
        ]
        if populate
        else []
    )
    assert all(original_generation.encode() not in raw for raw in snapshot.files.values())
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v18(archive)

    destination = tmp_path / "destination"
    result = engine.tasks.portability.import_clean(
        archive, destination, import_id=IMPORT_ID
    )
    assert result.schema_version == 19
    with closing(sqlite3.connect(destination / PHASE1_STATE_DATABASE)) as connection:
        rows = connection.execute("SELECT * FROM saved_item_sources ORDER BY 1, 2, 3").fetchall()
        assert rows == (
            [SOURCE, OTHER_SOURCE] if populate else []
        )
        generation = connection.execute("SELECT generation FROM browser_installation").fetchone()[0]
    assert generation != original_generation
    retry = engine.tasks.portability.import_clean(
        archive, destination, import_id=IMPORT_ID
    )
    assert retry.duplicate
    with closing(sqlite3.connect(destination / PHASE1_STATE_DATABASE)) as connection:
        assert connection.execute("SELECT generation FROM browser_installation").fetchone()[0] == (
            generation
        )


@pytest.mark.parametrize("defect", ["duplicate", "malformed", "authority", "missing"])
def test_invalid_saved_item_source_evidence_refuses_restore(tmp_path: Path, defect: str) -> None:
    engine, archive, _ = _export(tmp_path)
    manifest_path = archive / "portable-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(entry for entry in manifest["files"] if entry["path"] == PATH)
    path = archive / PATH
    data = json.loads(path.read_bytes())
    source = data["sources"][0]
    if defect == "duplicate":
        data["sources"].append(source)
    elif defect == "malformed":
        source["connection_id"] = SOURCE[1].upper()
    elif defect == "authority":
        source["generation"] = "a" * 64
    if defect == "missing":
        path.unlink()
        manifest["files"].remove(entry)
    else:
        raw = canonical(data)
        path.write_bytes(raw)
        entry["sha256"] = sha256(raw).hexdigest()
    manifest_path.write_bytes(canonical(manifest))

    destination = tmp_path / "destination"
    with pytest.raises(PortableValidationError):
        engine.tasks.portability.import_clean(
            archive, destination, import_id=IMPORT_ID
        )
    assert not destination.exists()


@pytest.mark.parametrize(
    "fault", [PortabilityFault.BEFORE_PROMOTION, PortabilityFault.AFTER_PROMOTION]
)
def test_restore_recovers_installation_generation(tmp_path: Path, fault: PortabilityFault) -> None:
    engine, archive, source_generation = _export(tmp_path)
    destination = tmp_path / "destination"
    authority = owner_authority(engine.tasks, session_id="restore-saved-item-test")

    def checkpoint(stage: PortabilityFault) -> None:
        if stage is fault:
            raise RuntimeError("interrupted clean restore")

    with pytest.raises(RuntimeError, match="interrupted clean restore"):
        restore_portable_clean(
            archive,
            destination,
            import_id=IMPORT_ID,
            authority=authority,
            checkpoint=checkpoint,
        )

    def generation() -> str:
        with closing(sqlite3.connect(destination / PHASE1_STATE_DATABASE)) as connection:
            assert not connection.execute("SELECT 1 FROM browser_grants").fetchall()
            current = connection.execute("SELECT generation FROM browser_installation").fetchone()
            assert current is not None and isinstance(current[0], str)
            return current[0]

    committed = fault is PortabilityFault.AFTER_PROMOTION
    assert destination.exists() is committed
    previous_generation = generation() if committed else None
    retry = restore_portable_clean(archive, destination, import_id=IMPORT_ID, authority=authority)
    assert retry.duplicate is committed
    assert generation() != source_generation
    if previous_generation is not None:
        assert generation() == previous_generation
