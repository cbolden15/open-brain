"""Schema21 owner-local embedding policy, migration from schema20 and runtime16 fencing."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
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
from open_brain_engine.engine.embedding_policy import (
    EmbeddingPolicyError,
    EmbeddingPolicyTasks,
    disable,
    enable,
    history_rows,
    read_policy,
)
from open_brain_engine.engine.embedding_policy_schema import EMBEDDING_POLICY_MIGRATION
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.migrations import SchemaError

from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.t03_adapters import agent_authority, owner_authority

COMPONENT = "ab" * 32
OTHER = "cd" * 32


def _clock() -> datetime:
    return datetime(2026, 10, 17, tzinfo=UTC)


def _connect(profile: LocalEngineContext) -> sqlite3.Connection:
    return sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE)


def _schema20(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEngineContext:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 20)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:20])
        predecessor.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 15)
        engine = BrainEngine.open(profile)
        engine.capture.accept(TextPayload("Synthetic schema20 evidence"), delivery_id="prior.20")
    return profile


def _tasks(engine: BrainEngine) -> EmbeddingPolicyTasks:
    return EmbeddingPolicyTasks(engine)


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-test")


def test_fresh_schema21_starts_disabled_with_exact_coordinates(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    assert local_schema.inspect_phase1_state(engine.profile) == local_schema.SchemaState(
        "current", 21
    )
    assert LOCAL_MIGRATIONS[20] == EMBEDDING_POLICY_MIGRATION
    with _connect(engine.profile) as connection:
        assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [
            (1, 16, 21)
        ]
        policy = read_policy(connection)
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    assert (policy.brain_id, policy.issuer_epoch) == tuple(identity)
    assert policy.status() == {
        "schema": "ob.embedding-policy-status.v1",
        "enabled": False,
        "component_id": None,
        "consent_version": 0,
    }
    assert (policy.policy_generation, policy.changed_at) == (0, None)


def test_enable_disable_are_monotone_and_append_history(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    tasks, owner = _tasks(engine), _owner(engine)
    first = tasks.enable(COMPONENT, authority=owner)
    assert (first.enabled, first.component_id, first.consent_version) == (True, COMPONENT, 1)
    second = tasks.enable(OTHER, authority=owner)
    third = tasks.disable(authority=owner)
    assert (third.enabled, third.component_id, third.consent_version) == (False, None, 3)
    assert first.policy_generation < second.policy_generation < third.policy_generation
    assert tasks.status(authority=owner) == third
    with _connect(engine.profile) as connection:
        assert [(row["version"], row["action"], row["component_id"]) for row in history_rows(
            connection
        )] == [(1, "enable", COMPONENT), (2, "enable", OTHER), (3, "disable", None)]
        assert all(str(row["changed_at"]).endswith("Z") for row in history_rows(connection))


def test_history_and_policy_reject_direct_rewrites(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    _tasks(engine).enable(COMPONENT, authority=_owner(engine))
    with _connect(engine.profile) as connection:
        for statement in (
            "UPDATE embedding_policy_history SET component_id=NULL",
            "DELETE FROM embedding_policy_history",
            "DELETE FROM embedding_policy",
            "UPDATE embedding_policy SET enabled=0,component_id=NULL",
            "UPDATE embedding_policy SET consent_version=0,policy_generation=policy_generation+1",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)


@pytest.mark.parametrize("component", ["AB" * 32, "ab" * 31, "g" * 64, "", None])
def test_enable_validates_component_format_before_mutation(
    tmp_path: Path, component: object
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    with pytest.raises(EmbeddingPolicyError, match="invalid_component_id"):
        _tasks(engine).enable(component, authority=_owner(engine))  # type: ignore[arg-type]
    assert _tasks(engine).status(authority=_owner(engine)).consent_version == 0


def test_enable_runs_the_typed_component_hook_before_mutation(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    seen: list[str] = []

    def refuse(component_id: str) -> None:
        seen.append(component_id)
        raise EmbeddingPolicyError("component_unavailable")

    with pytest.raises(EmbeddingPolicyError, match="component_unavailable"):
        _tasks(engine).enable(COMPONENT, authority=_owner(engine), verify_component=refuse)
    assert seen == [COMPONENT]
    assert _tasks(engine).status(authority=_owner(engine)).enabled is False


def test_non_owner_authority_is_refused(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    agent = agent_authority(
        principal_id="synthetic-agent", session_id="agent", grants=frozenset({"search"})
    )
    foreign = replace(_owner(engine), brain_id="brn_" + "a" * 26, issuer_epoch=1)
    for authority in (agent, foreign):
        with pytest.raises(EmbeddingPolicyError, match="owner_required"):
            _tasks(engine).enable(COMPONENT, authority=authority)
        with pytest.raises(EmbeddingPolicyError, match="owner_required"):
            _tasks(engine).disable(authority=authority)
        with pytest.raises(EmbeddingPolicyError, match="owner_required"):
            _tasks(engine).status(authority=authority)
    with _connect(engine.profile) as connection:
        assert connection.execute("SELECT count(*) FROM embedding_policy_history").fetchone() == (
            0,
        )
        with pytest.raises(EmbeddingPolicyError, match="transaction_required"):
            disable(connection, authority=_owner(engine), changed_at="2026-10-17T00:00:00Z")
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(EmbeddingPolicyError, match="owner_required"):
            enable(connection, authority=agent, component_id=COMPONENT, changed_at="x")
        connection.rollback()


def test_schema20_migrates_disabled_and_preserves_predecessor_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema20(tmp_path, monkeypatch)
    with _connect(profile) as connection:
        migrations = connection.execute("SELECT * FROM schema_migrations").fetchall()
        captures = connection.execute("SELECT * FROM captures").fetchall()
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    with pytest.raises((SchemaError, StateSchemaUnavailableError)):
        BrainEngine.open(profile)
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 21)
    with _connect(profile) as connection:
        assert connection.execute("SELECT * FROM schema_migrations").fetchall()[:20] == migrations
        assert connection.execute("SELECT * FROM captures").fetchall() == captures
        assert connection.execute("SELECT * FROM embedding_policy").fetchall() == [
            (1, *identity, 0, None, 0, 0, None)
        ]
        assert connection.execute("SELECT * FROM embedding_policy_history").fetchall() == []
        assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [
            (1, 16, 21)
        ]
    coordinate_local_migration(profile, clock=_clock)
    engine = BrainEngine.open(profile)
    assert _tasks(engine).enable(COMPONENT, authority=_owner(engine)).consent_version == 1


@pytest.mark.parametrize("damage", ["missing_row", "foreign_brain", "runtime_floor"])
def test_classifier_refuses_contradictory_schema21_state(tmp_path: Path, damage: str) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    with _connect(engine.profile) as connection:
        if damage == "missing_row":
            connection.execute("DROP TRIGGER embedding_policy_delete_immutable")
            connection.execute("DELETE FROM embedding_policy")
            trigger = next(
                statement
                for statement in EMBEDDING_POLICY_MIGRATION.statements
                if "embedding_policy_delete_immutable" in statement
            )
            connection.execute(trigger)
        elif damage == "foreign_brain":
            connection.execute("DROP TRIGGER embedding_policy_update_guard")
            connection.execute("UPDATE embedding_policy SET brain_id='brain_synthetic'")
            trigger = next(
                statement
                for statement in EMBEDDING_POLICY_MIGRATION.statements
                if "embedding_policy_update_guard" in statement
            )
            connection.execute(trigger)
        else:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE runtime_compatibility SET minimum_runtime_session_version=15"
            )
            connection.execute("PRAGMA ignore_check_constraints=OFF")
        assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
            "invalid", 21
        )


@pytest.mark.parametrize("opener", ["engine", "bootstrap", "read_only"])
def test_runtime15_client_refuses_schema21_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    profile = _schema20(tmp_path, monkeypatch)
    coordinate_local_migration(profile, clock=_clock)
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 20)
        previous.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:20])
        previous.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 15)
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
                    pytest.fail("runtime15 client admitted schema21")
    assert database.read_bytes() == before
    assert local_runtime_session.RUNTIME_SESSION_VERSION == 16
