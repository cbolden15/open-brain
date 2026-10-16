"""Production lifecycle cutover keeps predecessor evidence and reader fences."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    LocalEngineContext,
    StateSchemaUnavailableError,
    TextPayload,
    coordinate_local_migration,
    local_schema,
)
from open_brain_engine.engine.capture_lifecycle_journal import JOURNAL_CONTRACT, JOURNAL_SCHEMA
from open_brain_engine.engine.capture_lifecycle_schema import (
    CAPTURE_LIFECYCLE_SCHEMA,
    migrate_capture_lifecycle,
)
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.t03_contracts import T03Error
from open_brain_engine.storage.migrations import SchemaError

from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session
from open_brain.services.local_bootstrap import open_local_brain


def _clock() -> datetime:
    return datetime(2026, 10, 15, tzinfo=UTC)


def _legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEngineContext:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as previous:
        previous.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 17)
        previous.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:17])
        engine = BrainEngine.open(profile)
        engine.capture.accept(TextPayload("synthetic lifecycle cutover"), delivery_id="cutover.one")
    return profile


def _evidence(profile: LocalEngineContext) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("captures", "brain_identity", "schema_migrations")
        }


def test_fresh_lifecycle_schema_uses_frozen_journal_and_durable_brain(tmp_path: Path) -> None:
    assert CAPTURE_LIFECYCLE_SCHEMA[: len(JOURNAL_SCHEMA)] == JOURNAL_SCHEMA
    assert LOCAL_MIGRATIONS[16].checksum == (
        "bc6ae08a3fd8ea081ca485a9eb0ce529d4fe1dacb5845ad4652a37528b22f819"
    )
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with local_schema.open_local_database(profile) as connection:
        assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
            "current", 18
        )
        brain_id = connection.execute("SELECT brain_id FROM brain_identity").fetchone()[0]
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM lifecycle_journal_identity")
        ] == [(1, JOURNAL_CONTRACT, brain_id)]
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM runtime_compatibility")
        ] == [(1, 13, 18)]
        assert local_runtime_session.RUNTIME_SESSION_VERSION == 13


def test_explicit_schema_seventeen_cutover_preserves_old_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _legacy(tmp_path, monkeypatch)
    before = _evidence(profile)
    with pytest.raises(StateSchemaUnavailableError, match="migration"):
        BrainEngine.open(profile)
    assert _evidence(profile) == before
    coordinate_local_migration(profile, clock=_clock)
    after = _evidence(profile)
    assert after["captures"] == before["captures"]
    assert after["brain_identity"] == before["brain_identity"]
    assert after["schema_migrations"][:17] == before["schema_migrations"]
    assert len(after["schema_migrations"]) == 18
    with exclusive_runtime_admission(profile) as admission:
        migrate_capture_lifecycle(
            profile,
            admission=admission,
            clock=_clock,
            checkpoint=lambda _stage: pytest.fail("committed cutover reran"),
        )
    assert _evidence(profile) == after


@pytest.mark.parametrize(
    ("stage", "expected_version"),
    [
        ("capture_lifecycle_preflight", 17),
        ("capture_lifecycle_before_commit", 17),
        ("capture_lifecycle_after_commit", 18),
    ],
)
def test_schema_cutover_faults_reconcile_without_rewriting_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, expected_version: int
) -> None:
    profile = _legacy(tmp_path, monkeypatch)
    before = _evidence(profile)

    def fail(actual: str) -> None:
        if actual == stage:
            raise RuntimeError("synthetic schema cutover fault")

    error = (
        local_schema.LocalSchemaUpgradeCommittedError if expected_version == 18 else RuntimeError
    )
    with exclusive_runtime_admission(profile) as admission, pytest.raises(error):
        migrate_capture_lifecycle(profile, admission=admission, clock=_clock, checkpoint=fail)
    assert local_schema.inspect_phase1_state(profile).version == expected_version
    after = _evidence(profile)
    assert after["captures"] == before["captures"]
    assert after["brain_identity"] == before["brain_identity"]
    assert after["schema_migrations"][:17] == before["schema_migrations"]
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 18)


def test_cutover_refuses_live_peer_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _legacy(tmp_path, monkeypatch)
    before = _evidence(profile)
    with (
        local_runtime_session.hold_local_runtime_session(
            profile.root,
            profile.root_identity,
            legacy_state_exists=False,
            recover_abandoned_sessions=lambda: None,
        ),
        pytest.raises(T03Error, match="operation_pending"),
        exclusive_runtime_admission(profile) as admission,
    ):
        migrate_capture_lifecycle(profile, admission=admission, clock=_clock)
    assert _evidence(profile) == before


@pytest.mark.parametrize("opener", ["engine", "bootstrap", "read_only"])
def test_runtime_twelve_refuses_schema_eighteen_before_database_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    profile = _legacy(tmp_path, monkeypatch)
    coordinate_local_migration(profile, clock=_clock)
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 17)
        previous.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:17])
        previous.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 12)
        with pytest.raises(StateSchemaUnavailableError if opener != "read_only" else SchemaError):
            if opener == "engine":
                BrainEngine.open(profile)
            elif opener == "read_only":
                local_schema.open_local_database_read_only(profile)
            else:
                selection = select_local_root(
                    data_dir=str(profile.root),
                    environment={"HOME": str(tmp_path)},
                    platform_name="darwin",
                )
                with open_local_brain(selection, filesystem_type_probe=lambda *_a, **_k: "apfs"):
                    pytest.fail("runtime twelve entered schema eighteen")
    assert database.read_bytes() == before


def test_schema_classifier_refuses_missing_lifecycle_identity(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with local_schema.open_local_database(profile) as connection:
        connection.execute("DROP TRIGGER lifecycle_journal_identity_delete")
        connection.execute("DELETE FROM lifecycle_journal_identity")
        for statement in JOURNAL_SCHEMA:
            if statement.startswith("CREATE TRIGGER lifecycle_journal_identity_delete "):
                connection.execute(statement)
        assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
            "invalid", 18
        )
