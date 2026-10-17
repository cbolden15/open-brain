"""Independent transaction/migration fault expectations for real facet producers."""

from __future__ import annotations

import fcntl
import importlib
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine.facet_contracts import FacetError, request_from_wire, response_to_wire
from open_brain_engine.engine.local import BrainEngine, StateSchemaUnavailableError
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema import (
    LocalSchemaUpgradeCommittedError,
    _prepare_local_schema,
    inspect_phase1_state,
)
from open_brain_engine.engine.t03_contracts import T03Error
from open_brain_engine.storage.sqlite import connect_database

from packages.engine.tests.contract.test_facets_acceptance import FacetHarness


class BoundaryFault(RuntimeError):
    """A deterministic test fault at a producer's explicit transaction checkpoint."""


def _producer(brain: FacetHarness, checkpoint: Callable[[str], None]) -> Any:
    module = importlib.import_module("open_brain_engine.engine.facets")
    return module.FacetTasks(brain.engine, checkpoint=checkpoint)


def _snapshot(brain: FacetHarness) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: brain.rows(table)
        for table in (
            "facet_dimensions",
            "facet_subject_versions",
            "facet_assignments",
            "facet_events",
            "facet_operations",
            "engine_generations",
        )
    }


@pytest.mark.parametrize("stage", ["before_semantic_commit", "after_semantic_commit"])
def test_f06_transaction_boundary_fault_preserves_none_or_one_complete_mutation(
    tmp_path: Path,
    stage: str,
) -> None:
    brain = FacetHarness(tmp_path)
    before = _snapshot(brain)
    reached: list[str] = []

    def checkpoint(actual: str) -> None:
        reached.append(actual)
        if actual == stage:
            raise BoundaryFault(stage)

    producer = _producer(brain, checkpoint)
    request = request_from_wire(
        "facet.dimension.create",
        {
            "dto_version": 1,
            "name": "Fault-boundary dimension",
            "expected_version": 0,
            "operation_id": "fault-boundary",
        },
    )
    with pytest.raises(BoundaryFault, match=stage):
        producer.dimension_create(request, authority=brain.authority)
    assert stage in reached, "Fault must execute at the agreed semantic transaction boundary"
    observed = _snapshot(brain)
    if stage == "before_semantic_commit":
        assert observed == before
    else:
        assert len(observed["facet_dimensions"]) == 1
        assert len(observed["facet_events"]) == 1
        assert len(observed["facet_operations"]) == 1
        assert observed["engine_generations"] != before["engine_generations"]
    brain.engine = BrainEngine.open(brain.profile)
    receipt = brain.call(
        "facet.dimension.create",
        name="Fault-boundary dimension",
        expected_version=0,
        operation_id="fault-boundary",
    )
    assert receipt["status"] == "applied" and receipt["event_id"] is not None
    completed = _snapshot(brain)
    assert len(completed["facet_dimensions"]) == 1
    assert len(completed["facet_events"]) == 1
    assert len(completed["facet_operations"]) == 1
    if stage == "after_semantic_commit":
        assert completed == observed, (
            "Lost-response replay must not advance generation or append evidence"
        )


def test_f06_concurrent_identical_retry_has_one_event_and_exact_receipt(tmp_path: Path) -> None:
    brain = FacetHarness(tmp_path)

    def mutate(_: int) -> dict[str, Any] | FacetError:
        try:
            return brain.call(
                "facet.dimension.create",
                name="Concurrent",
                expected_version=0,
                operation_id="concurrent-identical",
            )
        except FacetError as error:
            assert error.code == "operation_pending"
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(mutate, (1, 2)))
    successes = [result for result in results if isinstance(result, dict)]
    assert successes, "At least one uncontended mutation must commit successfully"
    original = successes[0]
    for result in results:
        recovered = mutate(0) if isinstance(result, FacetError) else result
        assert recovered == original, (
            "Contention must recover the exact receipt through the same operation"
        )
    assert len(brain.rows("facet_events")) == 1
    assert len(brain.rows("facet_operations")) == 1


def _legacy_brain(tmp_path: Path) -> FacetHarness:
    brain = FacetHarness(tmp_path, initialize=False)
    # Invoke the retained schema-14 creator for synthetic setup. No historical
    # catalogue is edited and no version-15 database is downgraded.
    connection = connect_database(
        root=brain.root,
        database_name=".open-brain/state/phase1.sqlite3",
        expected_root_identity=brain.profile.root_identity,
        prepare=partial(
            _prepare_local_schema,
            schema_version=14,
            profile=brain.profile,
            tenant_id=brain.profile.tenant_id,
            clock=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        ),
    )
    connection.close()
    assert inspect_phase1_state(brain.profile).version == 14
    return brain


def _schema_evidence(
    brain: FacetHarness,
) -> tuple[int, list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with sqlite3.connect(brain.root / ".open-brain/state/phase1.sqlite3") as connection:
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            connection.execute("SELECT * FROM runtime_compatibility").fetchall(),
        )


def test_f16_schema14_upgrade_changes_compatibility_once_and_reopens_empty_facets(
    tmp_path: Path,
) -> None:
    brain = _legacy_brain(tmp_path)
    _, historical, _ = _schema_evidence(brain)
    coordinate_local_migration(brain.profile)
    version, migrations, compatibility = _schema_evidence(brain)
    assert version == 21
    assert migrations[:14] == historical, (
        "Retained migration names/checksums/times must remain exact"
    )
    assert compatibility == [(1, 16, 21)]
    brain.engine = BrainEngine.open(brain.profile)
    assert brain.call("facet.dimension.list")["items"] == []
    assert brain.call("facet.query.list")["items"] == []
    after = _schema_evidence(brain)
    coordinate_local_migration(brain.profile)
    assert _schema_evidence(brain) == after


@pytest.mark.parametrize("stage", ["facet_preflight", "facet_schema_applied"])
def test_f16_precommit_migration_fault_leaves_exact_schema14(tmp_path: Path, stage: str) -> None:
    brain = _legacy_brain(tmp_path)
    before = _schema_evidence(brain)
    reached: list[str] = []

    def checkpoint(actual: str) -> None:
        reached.append(actual)
        if actual == stage:
            raise BoundaryFault(stage)

    with pytest.raises(BoundaryFault, match=stage):
        coordinate_local_migration(brain.profile, checkpoint=checkpoint)
    assert stage in reached
    assert _schema_evidence(brain) == before
    coordinate_local_migration(brain.profile)
    assert _schema_evidence(brain)[0] == 21


def test_f16_postcommit_migration_fault_reports_committed_upgrade(tmp_path: Path) -> None:
    brain = _legacy_brain(tmp_path)

    def checkpoint(stage: str) -> None:
        if stage == "facet_complete":
            raise BoundaryFault("committed facet migration")

    with pytest.raises(
        LocalSchemaUpgradeCommittedError, match="facet schema upgrade committed"
    ) as error:
        coordinate_local_migration(brain.profile, checkpoint=checkpoint)
    assert isinstance(error.value.__cause__, BoundaryFault)
    assert _schema_evidence(brain)[0] == 15
    assert _schema_evidence(brain)[2] == [(1, 10, 15)]
    assert inspect_phase1_state(brain.profile).state == "supported_old"
    coordinate_local_migration(brain.profile)
    brain.engine = BrainEngine.open(brain.profile)
    assert brain.call("facet.dimension.list")["items"] == []


def test_f16_bad_historical_checksum_is_not_admitted_or_repaired(tmp_path: Path) -> None:
    brain = _legacy_brain(tmp_path)
    with sqlite3.connect(brain.root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=14", ("0" * 64,))
    corrupted = _schema_evidence(brain)
    with pytest.raises(StateSchemaUnavailableError):
        coordinate_local_migration(brain.profile)
    assert _schema_evidence(brain) == corrupted


def test_f16_live_schema14_runtime_peer_blocks_upgrade(tmp_path: Path) -> None:
    brain = _legacy_brain(tmp_path)
    registry = brain.root / ".open-brain/runtime-sessions"
    registry.mkdir(mode=0o700)
    marker = registry / ("session-" + "1" * 32 + ".lock")
    marker.touch(mode=0o600)
    marker.write_text(json.dumps({"pid": 1, "session_id": "1" * 32, "version": 9}))
    before = _schema_evidence(brain)
    with marker.open("r+") as peer:
        fcntl.flock(peer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(T03Error, match="operation_pending"):
            coordinate_local_migration(brain.profile)
        assert _schema_evidence(brain) == before
        fcntl.flock(peer.fileno(), fcntl.LOCK_UN)
    coordinate_local_migration(brain.profile)
    assert _schema_evidence(brain)[0] == 21


def test_f16_unknown_newer_schema_is_refused_without_touching_evidence(tmp_path: Path) -> None:
    brain = _legacy_brain(tmp_path)
    with sqlite3.connect(brain.root / ".open-brain/state/phase1.sqlite3") as connection:
        connection.execute("PRAGMA user_version=999")
    before = _schema_evidence(brain)
    with pytest.raises(StateSchemaUnavailableError):
        coordinate_local_migration(brain.profile)
    assert _schema_evidence(brain) == before


def test_f06_persisted_receipt_bytes_match_public_retry_after_reopen(tmp_path: Path) -> None:
    brain = FacetHarness(tmp_path)
    first = brain.call(
        "facet.dimension.create",
        name="Stored receipt",
        expected_version=0,
        operation_id="stored-receipt",
    )
    with sqlite3.connect(brain.root / ".open-brain/state/phase1.sqlite3") as connection:
        stored = json.loads(
            connection.execute(
                "SELECT receipt_json FROM facet_operations WHERE operation_id=?",
                ("stored-receipt",),
            ).fetchone()[0]
        )
    assert stored == first
    brain.engine = BrainEngine.open(brain.profile)
    replay = brain.call(
        "facet.dimension.create",
        name="Stored receipt",
        expected_version=0,
        operation_id="stored-receipt",
    )
    assert response_to_wire("facet.dimension.create", replay) == stored
