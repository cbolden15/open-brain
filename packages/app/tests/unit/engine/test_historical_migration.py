"""Schema13 bootstrap and fenced upgrade preserve the frozen twelve migrations."""

import fcntl
import json
import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    StateSchemaUnavailableError,
    TextPayload,
    local_schema,
)
from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.historical_contracts import HistoricalDestination
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_migration import migrate_historical
from open_brain_engine.engine.historical_registry import HistoricalRegistryStore
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import (
    exclusive_runtime_admission,
    hold_runtime_registry,
)
from open_brain_engine.engine.sharing_contracts import SharingError

from open_brain.profile import compile_single_user_local


@pytest.fixture(autouse=True)
def frozen_schema_thirteen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise actual Portable8/schema13 behavior after the current format advances."""
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 13)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:13])


def _schema_twelve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEngineContext:
    profile = compile_single_user_local(tmp_path / "old", starter_spaces=())
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 12)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:12])
        with closing(local_schema.open_local_database(profile)):
            pass
    return profile


def _assert_empty_state(profile: LocalEngineContext) -> None:
    with local_schema.open_local_database_read_only(profile) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert tuple(connection.execute("SELECT * FROM runtime_compatibility").fetchone()) == (
            1,
            8,
            13,
        )
        identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
        destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
        registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(destination)
        assert registry.generation == 0 and registry.memberships == ()
        HistoricalPendingFence(profile.root, profile.root_identity).assert_settled(registry)
        row = connection.execute(
            "SELECT brain_id,issuer_epoch,generation,registry_sha256 FROM historical_registry_state"
        ).fetchone()
        assert tuple(row) == (
            destination.brain_id,
            destination.issuer_epoch,
            0,
            registry.registry_sha256,
        )
        for table in (
            "historical_claims",
            "historical_operations",
            "historical_baselines",
            "historical_relations",
            "historical_revocations",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_schema_thirteen_fresh_bootstrap_has_explicit_empty_denial_state(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "fresh", starter_spaces=())
    BrainEngine.open(profile)
    _assert_empty_state(profile)
    fixture = (
        Path(__file__).resolve().parents[5] / "tests/fixtures/local-schema/catalog-checksums.json"
    )
    frozen = json.loads(fixture.read_text())
    assert {item.name: item.checksum for item in LOCAL_MIGRATIONS[:12]} == frozen


def test_coordinator_reaches_thirteen_and_ordinary_opener_cannot_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _schema_twelve(tmp_path, monkeypatch)
    with pytest.raises(StateSchemaUnavailableError, match="historical authority migration"):
        BrainEngine.open(profile)
    coordinate_local_migration(profile)
    _assert_empty_state(profile)
    BrainEngine.open(profile)


@pytest.mark.parametrize("stage", ["historical_schema_applied", "historical_files_initialized"])
def test_historical_upgrade_rolls_back_then_retries_without_inventing_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    profile = _schema_twelve(tmp_path, monkeypatch)

    def crash(observed: str) -> None:
        if observed == stage:
            raise RuntimeError("synthetic historical migration crash")

    with (
        exclusive_runtime_admission(profile) as admission,
        pytest.raises(RuntimeError, match="synthetic historical migration crash"),
    ):
        migrate_historical(
            profile, admission=admission, clock=lambda: datetime.now(UTC), checkpoint=crash
        )
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState(
        "supported_old", 12
    )
    with exclusive_runtime_admission(profile) as admission:
        migrate_historical(profile, admission=admission, clock=lambda: datetime.now(UTC))
        migrate_historical(profile, admission=admission, clock=lambda: datetime.now(UTC))
    _assert_empty_state(profile)


def test_old_schema_floor_refuses_thirteen_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema_twelve(tmp_path, monkeypatch)
    with exclusive_runtime_admission(profile) as admission:
        migrate_historical(profile, admission=admission, clock=lambda: datetime.now(UTC))
    path = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = path.read_bytes()
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 12)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:12])
        with pytest.raises(StateSchemaUnavailableError, match="newer"):
            BrainEngine.open(profile)
    assert path.read_bytes() == before


def test_historical_upgrade_lost_response_retries_committed_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema_twelve(tmp_path, monkeypatch)

    def lost_response(stage: str) -> None:
        if stage == "historical_complete":
            raise RuntimeError("synthetic lost migration response")

    with (
        exclusive_runtime_admission(profile) as admission,
        pytest.raises(RuntimeError, match="synthetic lost migration response"),
    ):
        migrate_historical(
            profile, admission=admission, clock=lambda: datetime.now(UTC), checkpoint=lost_response
        )
    _assert_empty_state(profile)
    path = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = path.read_bytes()
    with exclusive_runtime_admission(profile) as admission:
        migrate_historical(profile, admission=admission, clock=lambda: datetime.now(UTC))
    assert path.read_bytes() == before
    _assert_empty_state(profile)


def test_historical_migration_refuses_live_peer_before_database_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import historical_migration

    profile = _schema_twelve(tmp_path, monkeypatch)
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
        with monkeypatch.context() as blocked:
            blocked.setattr(
                historical_migration,
                "connect_database",
                lambda **_kwargs: pytest.fail("live peer reached historical database writes"),
            )
            with hold_runtime_registry(profile, directory, descriptor) as proof:
                assert proof.live_peer_count == 1
                with pytest.raises(SharingError, match="operation_pending"):
                    migrate_historical(profile, admission=proof, clock=lambda: datetime.now(UTC))
        assert path.read_bytes() == before
    finally:
        for handle in (session, directory, descriptor):
            os.close(handle)


def test_upgrade_preserves_retained_captures_sources_aliases_and_migration_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema_twelve(tmp_path, monkeypatch)
    with monkeypatch.context() as old:
        old.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 12)
        old.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:12])
        engine = BrainEngine.open(profile)
        receipt = engine.capture.accept(
            TextPayload("synthetic immutable pre-upgrade body"), delivery_id="old.owner.delivery"
        )
    tables = (
        "captures", "source_revisions", "source_aliases", "logical_sources", "schema_migrations"
    )
    with local_schema.open_local_database_read_only(profile, allow_old=True) as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables
        }
        source_path = connection.execute(
            "SELECT source_path FROM captures WHERE capture_id=?", (receipt.capture_id,)
        ).fetchone()[0]
    source_bytes = (profile.root / source_path).read_bytes()
    coordinate_local_migration(profile)
    _assert_empty_state(profile)
    with local_schema.open_local_database_read_only(profile) as connection:
        for table in tables:
            after = [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            if table == "schema_migrations":
                assert after[:12] == before[table]
                assert len(after) == 13
            else:
                assert after == before[table]
    assert (profile.root / source_path).read_bytes() == source_bytes
