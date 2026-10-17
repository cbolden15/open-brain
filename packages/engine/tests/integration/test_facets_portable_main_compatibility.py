"""Real Portable10 roundtrips retain mixed history, custody, and facet receipts."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.historical_tasks import adopt_historical_baseline
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.facets_v10 import FACET_SIDECAR_PATHS
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v9 import (
    validate_historical_authority,
    validated_portable_snapshot_v9,
)
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import open_existing_single_user_local
from packages.app.tests.unit.engine._historical_compatibility_fixtures import (
    portable5_import_engine,
    retained_import_engine,
)
from packages.app.tests.unit.engine.test_historical_baseline import _baseline
from packages.app.tests.unit.engine.test_historical_compatibility import linked_v2


def _mixed(engine: BrainEngine) -> tuple[str, dict[str, Any], EffectiveAuthority]:
    owner = EffectiveAuthority(
        engine.profile.owner_actor_id, "portable10", frozenset(), None, owner=True
    )
    old = _baseline(engine, retained_delivery_id="portable10.old.owner")
    with exclusive_runtime_admission(engine.profile) as admission:
        adopt_historical_baseline(
            engine.profile,
            old,
            authority=owner,
            admission=admission,
            validate_before_write=lambda: None,
        )
    linked_v2(engine)
    dimension = engine.facets.dimension_create(
        fc.DimensionCreateRequest(
            operation_id="portable10.dimension", expected_version=0, name="Mixed evidence"
        ),
        authority=owner,
    ).to_wire()["subject_id"]
    assert isinstance(dimension, str)
    value = engine.facets.value_create(
        fc.ValueCreateRequest(
            operation_id="portable10.value",
            expected_version=0,
            dimension_id=dimension,
            expected_dimension_version=1,
            name="Retained",
        ),
        authority=owner,
    ).to_wire()["subject_id"]
    arguments: dict[str, Any] = {
        "dto_version": 1,
        "operation_id": "portable10.assignment",
        "expected_version": 0,
        "record_id": old.retained_original.capture_id,
        "expected_revision_id": old.retained_original.capture_id,
        "add_value_ids": [value],
        "remove_value_ids": [],
    }
    receipt = engine.facets.assignments_patch(
        fc.AssignmentsPatchRequest(**arguments), authority=owner
    ).to_wire()
    return old.retained_original.capture_id, {"arguments": arguments, "receipt": receipt}, owner


@pytest.mark.parametrize("fixture", [portable5_import_engine, retained_import_engine])
def test_mixed_history_custody_and_facets_survive_clean_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: Any,
) -> None:
    engine = fixture(tmp_path, monkeypatch)
    capture_id, assignment, owner = _mixed(engine)
    tables = (
        "captures",
        "source_revisions",
        "source_aliases",
        "historical_operations",
        "sharing_previews",
        "sharing_decisions",
        "sharing_links",
        "facet_operations",
        "facet_events",
    )
    with engine._store.connect() as connection:
        before_rows = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables
        }
    archive = tmp_path / "portable10"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    before = validated_portable_snapshot(archive)
    assert before.manifest["schema_version"] == 15
    history = validate_historical_authority(before.files)
    assert [record.request.dto_version for record in history.records] == [1, 2, 2, 2]
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v9(archive)
    destination = tmp_path / "restored10"
    import_id = "import_" + str(uuid4())
    assert not engine.portability.import_clean(archive, destination, import_id=import_id).duplicate
    assert engine.portability.import_clean(archive, destination, import_id=import_id).duplicate
    restored = BrainEngine.open(open_existing_single_user_local(destination))
    with restored._store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        for table, rows in before_rows.items():
            assert [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ] == rows
    assert (
        restored.facets.assignments_patch(
            fc.AssignmentsPatchRequest(**assignment["arguments"]), authority=owner
        ).to_wire()
        == assignment["receipt"]
    )
    assert capture_id in before_rows["captures"][0] or any(
        capture_id in row for row in before_rows["captures"]
    )
    reexport = tmp_path / "reexport10"
    restored.portability.export(reexport, export_id="export_" + str(uuid4()))
    after = validated_portable_snapshot(reexport)
    assert {path: raw for path, raw in after.files.items() if path != "portable-manifest.json"} == {
        path: raw for path, raw in before.files.items() if path != "portable-manifest.json"
    }
    assert after.files.keys() >= FACET_SIDECAR_PATHS
    assert [
        record.canonical_bytes() for record in validate_historical_authority(after.files).records
    ] == [record.canonical_bytes() for record in history.records]


@pytest.mark.parametrize(
    "stage", ["facets_restored", "historical_sql_committed", "historical_audited"]
)
def test_mixed_restore_fault_never_promotes_and_retry_preserves_both_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    from open_brain_engine.engine import portable_v10_restore
    from open_brain_engine.portable.v1 import PortableSnapshot
    from open_brain_engine.storage.filesystem import RootIdentity

    engine = portable5_import_engine(tmp_path, monkeypatch)
    _mixed(engine)
    archive = tmp_path / "portable10"
    engine.portability.export(archive, export_id="export_" + str(uuid4()))
    before = validated_portable_snapshot(archive)
    destination = tmp_path / "restored10"
    restore = portable_v10_restore.restore_portable_v10_root

    def checkpoint(current: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic mixed restore interruption")

    def interrupted(
        root: Path, *, snapshot: PortableSnapshot, expected_root_identity: RootIdentity
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
        with pytest.raises(RuntimeError, match="synthetic mixed restore interruption"):
            engine.portability.import_clean(archive, destination, import_id=import_id)
    assert not destination.exists()
    assert validated_portable_snapshot(archive).files == before.files
    assert not engine.portability.import_clean(archive, destination, import_id=import_id).duplicate
    assert engine.portability.import_clean(archive, destination, import_id=import_id).duplicate
    assert validated_portable_snapshot(destination).files == before.files
