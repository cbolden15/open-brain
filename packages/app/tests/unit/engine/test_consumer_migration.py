"""Real schema20 admission, exclusive migration and preserved predecessor custody."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    CaptureSubmission,
    LocalEngineContext,
    StateSchemaUnavailableError,
    TextPayload,
    local_schema,
)
from open_brain_engine.engine.capture_lifecycle import open_lifecycle_journal
from open_brain_engine.engine.capture_lifecycle_contracts import (
    GroupIntent,
    LifecycleRequest,
    digest,
)
from open_brain_engine.engine.capture_lifecycle_journal import JOURNAL_SCHEMA
from open_brain_engine.engine.consumer_journal import LifecycleJournalV2
from open_brain_engine.engine.consumer_schema import migrate_consumer_intake
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error
from open_brain_engine.storage.migrations import SchemaError

from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session
from open_brain.services.local_runtime_session import hold_local_runtime_session


@pytest.fixture(autouse=True)
def _frozen_consumer_schema20(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the predecessor cutover's exact20/15 assertions and adapter semantics."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 20)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:20])
    monkeypatch.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 15)


def _clock() -> datetime:
    return datetime(2026, 10, 16, tzinfo=UTC)


def _connect(profile: LocalEngineContext) -> sqlite3.Connection:
    return sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE)


def _schema19(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEngineContext:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 19)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:19])
        engine = BrainEngine.open(profile)
        engine.capture.accept(
            TextPayload("Synthetic prior owner evidence"), delivery_id="prior.owner"
        )
    return profile


@pytest.fixture
def current20_engine(tmp_path: Path) -> BrainEngine:
    """Explicit synthetic current fixture available to successor API tests."""
    return BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))


def test_fresh_current20_opens_real_capture_and_exact_journal(
    current20_engine: BrainEngine,
) -> None:
    engine = current20_engine
    assert local_schema.inspect_phase1_state(engine.profile) == local_schema.SchemaState(
        "current", 20
    )
    receipt = engine.capture.accept(TextPayload("Synthetic fresh schema20"), delivery_id="fresh20")
    assert receipt.capture_id.startswith("capture_")
    with engine._store.transaction() as connection:
        assert isinstance(open_lifecycle_journal(connection), LifecycleJournalV2)
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
            15,
            20,
        )
        assert connection.execute("SELECT count(*) FROM consumer_scopes").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["journal_identity", "runtime_floor", "missing_table"])
def test_current_classifier_refuses_contradictory_successor_coordinates(
    current20_engine: BrainEngine,
    damage: str,
) -> None:
    with _connect(current20_engine.profile) as connection:
        if damage == "journal_identity":
            trigger = next(
                statement
                for statement in JOURNAL_SCHEMA
                if statement.startswith("CREATE TRIGGER lifecycle_journal_identity_update ")
            )
            connection.execute("DROP TRIGGER lifecycle_journal_identity_update")
            connection.execute(
                "UPDATE lifecycle_journal_identity SET contract='ob.capture-lifecycle-journal.v1'"
            )
            connection.execute(trigger)
        elif damage == "runtime_floor":
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE runtime_compatibility SET minimum_runtime_session_version=14"
            )
            connection.execute("PRAGMA ignore_check_constraints=OFF")
        else:
            connection.execute("DROP TABLE consumer_previews")
        assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
            "invalid", 20
        )


def test_coordinator_admits_owner19_as_intermediate_before_consumer20(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 18)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:18])
        BrainEngine.open(profile)
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 20)


def test_upgrade_preserves_legacy_bytes_and_same_issuer_pending_owner_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _schema19(tmp_path, monkeypatch)
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 19)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:19])
        engine = BrainEngine.open(profile)
        owner = EffectiveAuthority(
            profile.owner_actor_id, "synthetic", frozenset(), None, owner=True
        )
        segment = engine.lifecycle.prepare_segment(
            CaptureSubmission.for_local_owner(
                profile=profile,
                payload=TextPayload("Synthetic pending owner segment"),
                delivery_id="pending.owner",
            ),
            authority=owner,
        )
        with _connect(profile) as connection:
            brain, epoch = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity"
            ).fetchone()
        manifest = GroupIntent(
            digest(["old-owner-group"]), digest(["old-owner-revision"]), None, (segment.binding,)
        )
        request = LifecycleRequest(
            str(uuid4()), "consumer_import", brain, epoch, {"groups": [manifest.value()]}
        )
        engine.lifecycle.begin(request, authority=owner)
        engine.lifecycle.stage_segment(request, manifest.group_key, segment, authority=owner)
    with _connect(profile) as connection:
        migrations = connection.execute(
            "SELECT * FROM schema_migrations ORDER BY version"
        ).fetchall()
        retained = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "lifecycle_journal_requests",
                "lifecycle_journal_events",
                "lifecycle_journal_payloads",
            )
        }
        issuer = connection.execute("SELECT issuer_epoch FROM brain_identity").fetchone()[0]
    with pytest.raises(SchemaError, match="exclusive admission"):
        BrainEngine.open(profile)
    coordinate_local_migration(profile, clock=_clock)
    with _connect(profile) as connection:
        assert (
            connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()[:19]
            == migrations
        )
        for table, rows in retained.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert connection.execute("SELECT issuer_epoch FROM brain_identity").fetchone()[0] == issuer
    engine = BrainEngine.open(profile)
    assert engine.lifecycle.status(request.operation_id, authority=owner)["status"] != "committed"
    engine.lifecycle.recover_pending(request, authority=owner)
    engine.lifecycle.activate_group(
        request, manifest.group_key, session_id="capsess_" + str(uuid4()), authority=owner
    )
    assert engine.lifecycle.finish(request, authority=owner)["status"] == "committed"


@pytest.mark.parametrize(
    "stage,version",
    [
        ("consumer_intake_preflight", 19),
        ("consumer_intake_before_commit", 19),
        ("consumer_intake_after_commit", 20),
    ],
)
def test_exclusive_migration_fault_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    version: int,
) -> None:
    profile = _schema19(tmp_path, monkeypatch)

    def interrupt(actual: str) -> None:
        if actual == stage:
            raise RuntimeError("synthetic consumer migration interruption")

    expected = local_schema.LocalSchemaUpgradeCommittedError if version == 20 else RuntimeError
    with exclusive_runtime_admission(profile) as admission, pytest.raises(expected):
        migrate_consumer_intake(profile, admission=admission, clock=_clock, checkpoint=interrupt)
    assert local_schema.inspect_phase1_state(profile).version == version
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 20)


def test_live_peer_fences_before_migration_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _schema19(tmp_path, monkeypatch)
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()
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
    assert database.read_bytes() == before


@pytest.mark.parametrize("opener", ["engine", "readonly"])
def test_predecessor19_reader_refuses20_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opener: str,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    BrainEngine.open(profile)
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 19)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:19])
        error = StateSchemaUnavailableError if opener == "engine" else SchemaError
        with pytest.raises(error):
            if opener == "engine":
                BrainEngine.open(profile)
            else:
                local_schema.open_local_database_read_only(profile)
    assert database.read_bytes() == before
