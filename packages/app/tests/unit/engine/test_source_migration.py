from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import ReferencePayload, TextPayload, local_schema
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.source_migration import JOURNAL, migrate_sources
from open_brain_engine.portable.v1 import PortableValidationError, validated_portable_snapshot
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH, validated_portable_snapshot_v4
from open_brain_engine.storage.migrations import SchemaError
from open_brain_engine.storage.sqlite import connect_database_read_only

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission


@pytest.mark.parametrize(
    "crash",
    [
        None,
        "journal_durable",
        "old_visible_barrier",
        "sidecars_durable",
        "schema_committed",
        "metadata_published",
        "validated",
    ],
)
def test_source_cutover_recovers_without_rewriting_legacy_bytes(
    tmp_path: Path, crash: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:6])
        engine = BrainEngine.open(profile)
        submission = _public_submission(engine.tasks)
        first = replace(submission, payload=ReferencePayload(submission.source_reference, "first"))
        old = engine.capture.submit(first)
        current = engine.capture.submit(
            replace(first, payload=ReferencePayload(first.source_reference, "second"))
        )
    preimages = {
        p.relative_to(profile.root): p.read_bytes()
        for p in (profile.root / "sources").rglob("*")
        if p.is_file()
    }

    class Crash(RuntimeError):
        pass

    def checkpoint(stage: str) -> None:
        if stage == crash:
            raise Crash

    with exclusive_runtime_admission(profile) as admission:
        if crash is not None:
            with pytest.raises(Crash):
                migrate_sources(
                    profile,
                    admission=admission,
                    clock=lambda: datetime.now(UTC),
                    checkpoint=checkpoint,
                )
            if crash != "journal_durable":
                with monkeypatch.context() as legacy:
                    legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 6)
                    with pytest.raises(SchemaError):
                        engine.capture.accept(TextPayload("refused"), delivery_id="legacy.refused")
        migrate_sources(profile, admission=admission, clock=lambda: datetime.now(UTC))
    assert json.loads((profile.root / JOURNAL).read_bytes())["stage"] == "complete"
    for path, data in preimages.items():
        assert (profile.root / path).read_bytes() == data
    metadata = json.loads((profile.root / SOURCE_METADATA_PATH).read_bytes())
    export = tmp_path / "synthetic-export"
    export.mkdir(mode=0o700)
    manifest_bytes = (profile.root / "portable-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    for entry in manifest["files"]:
        target = export / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes((profile.root / entry["path"]).read_bytes())
    (export / "portable-manifest.json").write_bytes(manifest_bytes)
    validated_portable_snapshot_v4(export)
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(export)
    assert {row["capture_id"] for row in metadata["revisions"]} == {
        old.capture_id,
        current.capture_id,
    }
    with connect_database_read_only(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    ) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
            2,
            7,
        )
        assert (
            connection.execute(
                "SELECT historical_only FROM logical_sources WHERE head_capture_id=?",
                (old.capture_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT historical_only FROM logical_sources WHERE head_capture_id=?",
                (current.capture_id,),
            ).fetchone()[0]
            == 0
        )
