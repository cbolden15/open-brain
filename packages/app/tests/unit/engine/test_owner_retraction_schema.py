"""Owner retraction's supplemental evidence preserves the lifecycle journal."""

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
    local_schema,
)
from open_brain_engine.engine.capture_lifecycle_contracts import LifecycleError
from open_brain_engine.engine.capture_lifecycle_journal import LifecycleJournal
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.owner_retraction_schema import migrate_owner_retraction
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.t03_contracts import T03Error
from open_brain_engine.storage.migrations import SchemaError

from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.local_runtime_session import hold_local_runtime_session


@pytest.fixture(autouse=True)
def _frozen_owner_schema19(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the predecessor cutover's exact19/14 assertions and adapter semantics."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 19)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:19])
    monkeypatch.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 14)


def _clock() -> datetime:
    return datetime(2026, 10, 16, tzinfo=UTC)


def _previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEngineContext:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as previous:
        previous.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 18)
        previous.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:18])
        engine = BrainEngine.open(profile)
        engine.capture.accept(TextPayload("synthetic owner migration"), delivery_id="owner.cutover")
    return profile


def _connect(profile: LocalEngineContext) -> sqlite3.Connection:
    return sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE)


def test_cutover_preserves_journal_and_previous_migration_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _previous(tmp_path, monkeypatch)
    with _connect(profile) as connection:
        before = connection.execute("SELECT * FROM schema_migrations").fetchall()
        journal = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name LIKE 'lifecycle_%'"
        ).fetchall()
        policy = connection.execute(
            "SELECT projection_policy_version FROM engine_generations"
        ).fetchone()[0]
    coordinate_local_migration(profile, clock=_clock)
    with _connect(profile) as connection:
        assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
            "current", 19
        )
        assert connection.execute("SELECT * FROM schema_migrations").fetchall()[:18] == before
        assert (
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name LIKE 'lifecycle_%'"
            ).fetchall()
            == journal
        )
        assert (
            connection.execute(
                "SELECT projection_policy_version FROM engine_generations"
            ).fetchone()[0]
            == policy + 1
        )
        assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [(1, 14, 19)]
        assert connection.execute("SELECT count(*) FROM retraction_previews").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM retraction_approvals").fetchone()[0] == 0
    with exclusive_runtime_admission(profile) as admission:
        migrate_owner_retraction(
            profile,
            admission=admission,
            clock=_clock,
            checkpoint=lambda _stage: pytest.fail("cutover reran"),
        )


@pytest.mark.parametrize(
    "stage,version",
    [
        ("owner_retraction_preflight", 18),
        ("owner_retraction_before_commit", 18),
        ("owner_retraction_after_commit", 19),
    ],
)
def test_cutover_fault_rolls_back_or_reports_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, version: int
) -> None:
    profile = _previous(tmp_path, monkeypatch)

    def fail(actual: str) -> None:
        if actual == stage:
            raise RuntimeError("synthetic migration fault")

    error = local_schema.LocalSchemaUpgradeCommittedError if version == 19 else RuntimeError
    with exclusive_runtime_admission(profile) as admission, pytest.raises(error):
        migrate_owner_retraction(profile, admission=admission, clock=_clock, checkpoint=fail)
    assert local_schema.inspect_phase1_state(profile).version == version
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 19)


def test_cutover_refuses_live_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _previous(tmp_path, monkeypatch)
    before = (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes()
    with (
        hold_local_runtime_session(
            profile.root,
            profile.root_identity,
            legacy_state_exists=False,
            recover_abandoned_sessions=lambda: None,
        ),
        pytest.raises(T03Error, match="operation_pending"),
    ):
        coordinate_local_migration(profile, clock=_clock)
    assert (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes() == before


@pytest.mark.parametrize("schema,runtime", [(18, 13), (19, 14), (18, 14), (19, 13)])
def test_journal_adapter_requires_exact_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: int, runtime: int
) -> None:
    profile = _previous(tmp_path, monkeypatch)
    if schema == 19:
        coordinate_local_migration(profile, clock=_clock)
    with _connect(profile) as connection:
        connection.execute("DROP TABLE runtime_compatibility")
        connection.execute(
            "CREATE TABLE runtime_compatibility("
            "singleton, minimum_runtime_session_version, state_schema_version)"
        )
        connection.execute("INSERT INTO runtime_compatibility VALUES(1,?,?)", (runtime, schema))
        brain_id, epoch = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity"
        ).fetchone()
        if (schema, runtime) in {(18, 13), (19, 14)}:
            LifecycleJournal.for_engine(connection, brain_id=brain_id, issuer_epoch=epoch)
        else:
            with pytest.raises(LifecycleError, match="invalid_evidence"):
                LifecycleJournal.for_engine(connection, brain_id=brain_id, issuer_epoch=epoch)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE retraction_approvals SET request_bytes=x'01'", "DELETE FROM retraction_approvals"],
)
def test_approval_evidence_is_immutable(tmp_path: Path, statement: str) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    BrainEngine.open(profile)
    with _connect(profile) as connection:
        # A synthetic parent is unnecessary here: sqlite3's default FK setting is off.
        connection.execute(
            "INSERT INTO retraction_approvals "
            "VALUES('synthetic-operation',x'00','request',x'00','receipt')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(statement)


@pytest.mark.parametrize("opener", ["engine", "bootstrap", "read_only"])
def test_predecessor_runtime_refuses_owner_schema_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    profile = _previous(tmp_path, monkeypatch)
    coordinate_local_migration(profile, clock=_clock)
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 18)
        previous.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:18])
        previous.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 13)
        error = StateSchemaUnavailableError if opener != "read_only" else SchemaError
        with pytest.raises(error):
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
                    pytest.fail("predecessor runtime admitted owner schema")
    assert database.read_bytes() == before
