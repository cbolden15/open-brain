"""Schema fourteen appends a floor without rewriting historical evidence."""

import fcntl
import json
import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, TextPayload, local_schema
from open_brain_engine.engine.historical_compatibility_migration import (
    migrate_historical_compatibility,
)
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import (
    exclusive_runtime_admission,
    hold_runtime_registry,
)
from open_brain_engine.engine.sharing_contracts import SharingError

from open_brain.profile import compile_single_user_local


def test_schema_fourteen_preserves_thirteen_migration_checksums(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[5]
    frozen = json.loads(
        (root / "tests/fixtures/local-schema/catalog-checksums-thirteen.json").read_bytes()
    )
    assert {m.name: m.checksum for m in LOCAL_MIGRATIONS[:13]} == frozen
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    with engine._store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
            9,
            14,
        )


def test_schema_thirteen_upgrade_preserves_capture_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        engine = BrainEngine.open(profile)
        engine.capture.accept(TextPayload("synthetic upgrade"), delivery_id="synthetic.upgrade")
        with engine._store.connect() as connection:
            before = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("captures", "source_revisions", "source_aliases", "schema_migrations")
            }
            relative = connection.execute("SELECT source_path FROM captures").fetchone()[0]
        raw = (profile.root / relative).read_bytes()
    coordinate_local_migration(profile)
    with closing(local_schema.open_local_database_read_only(profile)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        for table, rows in before.items():
            after = [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            assert (after[:13] if table == "schema_migrations" else after) == rows
    assert (profile.root / relative).read_bytes() == raw


@pytest.mark.parametrize(
    "stage",
    [
        "historical_compatibility_preflight",
        "historical_compatibility_schema_applied",
        "historical_compatibility_complete",
    ],
)
def test_schema_fourteen_migration_rolls_back_or_retries_exact_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        BrainEngine.open(profile)

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic compatibility interruption")

    with exclusive_runtime_admission(profile) as admission:
        with pytest.raises(RuntimeError, match="synthetic compatibility interruption"):
            migrate_historical_compatibility(
                profile, admission=admission, clock=lambda: datetime.now(UTC), checkpoint=crash
            )
        expected = 14 if stage == "historical_compatibility_complete" else 13
        assert local_schema.inspect_phase1_state(profile).version == expected
        migrate_historical_compatibility(
            profile, admission=admission, clock=lambda: datetime.now(UTC)
        )
        before = (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes()
        migrate_historical_compatibility(
            profile, admission=admission, clock=lambda: datetime.now(UTC)
        )
        assert (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes() == before
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 14)


@pytest.mark.parametrize("damage", ["pending", "missing_registry"])
def test_schema_fourteen_migration_refuses_unsettled_authority_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from open_brain_engine.engine.historical_fence import HistoricalPendingFence
    from open_brain_engine.engine.historical_registry import _REGISTRY_PATH

    from .test_historical_projection import _claim_transition

    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        engine = BrainEngine.open(profile)
        if damage == "pending":
            record = _claim_transition(engine)
            HistoricalPendingFence(profile.root, profile.root_identity).prepare(record)
        else:
            (profile.root / _REGISTRY_PATH).unlink()
    before = (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes()
    with exclusive_runtime_admission(profile) as admission, pytest.raises(SharingError):
        migrate_historical_compatibility(
            profile, admission=admission, clock=lambda: datetime.now(UTC)
        )
    assert (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes() == before
    assert local_schema.inspect_phase1_state(profile).version == 13


def test_schema_fourteen_migration_refuses_live_peer_before_database_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import historical_compatibility_migration

    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        BrainEngine.open(profile)
    path = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = path.read_bytes()
    with exclusive_runtime_admission(profile):
        pass
    registry = profile.root / ".open-brain/runtime-sessions/registry.lock"
    descriptor = os.open(registry, os.O_RDWR)
    directory = os.open(registry.parent, os.O_RDONLY | os.O_DIRECTORY)
    peer = registry.parent / "session-11111111111111111111111111111111.lock"
    session = os.open(peer, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(session, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            historical_compatibility_migration,
            "connect_database",
            lambda **_kwargs: pytest.fail("live peer reached compatibility database writes"),
        )
        with hold_runtime_registry(profile, directory, descriptor) as proof:
            assert proof.live_peer_count == 1
            with pytest.raises(SharingError, match="operation_pending"):
                migrate_historical_compatibility(
                    profile, admission=proof, clock=lambda: datetime.now(UTC)
                )
        assert path.read_bytes() == before
    finally:
        for handle in (session, directory, descriptor):
            os.close(handle)


def test_schema_thirteen_reader_refuses_fourteen_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import StateSchemaUnavailableError

    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    before = (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes()
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        with pytest.raises(StateSchemaUnavailableError, match="newer"):
            BrainEngine.open(profile)
    assert (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes() == before


def test_schema_fourteen_rebuild_preserves_populated_v1_chain_and_constraints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from .test_portable_v8_historical import _linked

    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        engine = BrainEngine.open(profile)
        _linked(engine)
        with engine._store.connect() as connection:
            before = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in (
                    "historical_operations",
                    "historical_claims",
                    "historical_baselines",
                    "historical_relations",
                    "historical_revocations",
                )
            }
            triggers = [
                tuple(row)
                for row in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'historical%immutable' ORDER BY name"
                )
            ]
    coordinate_local_migration(profile)
    with closing(local_schema.open_local_database(profile)) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        for table, rows in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")] == rows
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'historical%immutable' ORDER BY name"
            )
        ] == triggers
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM historical_operations")
        row = list(before["historical_operations"][0])
        row[0], row[6] = "bound.synthetic.v1", 4
        raw = bytes(row[3])
        row[3] = raw + b" " * (65537 - len(raw))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute("INSERT INTO historical_operations VALUES(?,?,?,?,?,?,?)", row)
        value = json.loads(raw)
        value["dto_version"] = 2
        raw_v2 = json.dumps(value).encode()
        row[0] = "bound.synthetic.v2"
        row[3] = raw_v2 + b" " * (524288 - len(raw_v2))
        connection.execute("INSERT INTO historical_operations VALUES(?,?,?,?,?,?,?)", row)
        row[0], row[6], row[3] = "bound.synthetic.over", 5, row[3] + b" "
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute("INSERT INTO historical_operations VALUES(?,?,?,?,?,?,?)", row)
        baseline = list(before["historical_baselines"][0])
        raw_envelope = bytes(baseline[6])
        baseline[6] = raw_envelope + b" " * (65537 - len(raw_envelope))
        with pytest.raises(sqlite3.IntegrityError, match="V1 envelope"):
            connection.execute("INSERT INTO historical_baselines VALUES(?,?,?,?,?,?,?)", baseline)
        connection.rollback()


def test_schema_fourteen_rebuild_refuses_damaged_existing_chain_before_schema_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.historical_recovery import _historical_transaction
    from open_brain_engine.engine.historical_tasks import register_historical_claim
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    from .test_historical_tasks import _request

    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])
        engine = BrainEngine.open(profile)
        request = _request(engine)
        owner = EffectiveAuthority(profile.owner_actor_id, "owner", frozenset(), None, owner=True)
        with exclusive_runtime_admission(profile) as admission:
            register_historical_claim(
                profile,
                request,
                authority=owner,
                admission=admission,
                validate_before_write=lambda: None,
            )
        with _historical_transaction(profile, lambda: None) as connection:
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='historical_operations_update_immutable'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER historical_operations_update_immutable")
            connection.execute("UPDATE historical_operations SET request_sha256=?", ("0" * 64,))
            connection.execute(definition)
    before = (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes()
    with (
        exclusive_runtime_admission(profile) as admission,
        pytest.raises(SharingError, match="binding_mismatch"),
    ):
        migrate_historical_compatibility(
            profile, admission=admission, clock=lambda: datetime.now(UTC)
        )
    assert (profile.root / local_schema.PHASE1_STATE_DATABASE).read_bytes() == before
    assert local_schema.inspect_phase1_state(profile).version == 13
