"""Schema-11 evidence upgrades exclusively and runtime 6 cannot open schema 12."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, StateSchemaUnavailableError, local_schema
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import (
    exclusive_runtime_admission,
    hold_runtime_registry,
)
from open_brain_engine.engine.sharing_contracts import SharingError
from open_brain_engine.engine.sharing_migration import migrate_sharing

from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session


def _schema_eleven(tmp_path: Path):  # type: ignore[no-untyped-def]
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    fixture = (
        Path(__file__).resolve().parents[5]
        / "tests/fixtures/local-schema/schema-11-c663828.sql"
    )
    path = profile.root / local_schema.PHASE1_STATE_DATABASE
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState(
        "supported_old", 11
    )
    return profile


def test_sharing_migration_rolls_back_and_retries_exact_schema_eleven(
    tmp_path: Path,
) -> None:
    profile = _schema_eleven(tmp_path)
    path = profile.root / local_schema.PHASE1_STATE_DATABASE
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert len(before) == 11

    def fault(stage: str) -> None:
        if stage == "sharing_schema_applied":
            raise RuntimeError("synthetic sharing migration fault")

    with (
        exclusive_runtime_admission(profile) as admission,
        pytest.raises(RuntimeError, match="synthetic sharing migration fault"),
    ):
        migrate_sharing(
            profile, admission=admission, clock=lambda: datetime.now(UTC), checkpoint=fault
        )
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState(
        "supported_old", 11
    )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            == before
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='sharing_previews'"
            ).fetchall()
            == []
        )
    with exclusive_runtime_admission(profile) as admission:
        migrate_sharing(profile, admission=admission, clock=lambda: datetime.now(UTC))
        migrate_sharing(profile, admission=admission, clock=lambda: datetime.now(UTC))
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState(
        "supported_old", 12
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [(1, 7, 12)]
        assert [
            tuple(row[:3])
            for row in connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            )
        ][:11] == before


def test_runtime_six_refuses_schema_twelve_before_recovery_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema_eleven(tmp_path)
    with exclusive_runtime_admission(profile) as admission:
        migrate_sharing(profile, admission=admission, clock=lambda: datetime.now(UTC))
    path = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = path.read_bytes()
    with monkeypatch.context() as historical:
        historical.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 11)
        historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:11])
        historical.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 6)
        with pytest.raises(StateSchemaUnavailableError, match="newer"):
            BrainEngine.open(profile)
    assert path.read_bytes() == before


def test_sharing_migration_refuses_live_peer_before_database_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import sharing_migration

    profile = _schema_eleven(tmp_path)
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
                sharing_migration,
                "connect_database",
                lambda **_kwargs: pytest.fail("live peer reached sharing database writes"),
            )
            with hold_runtime_registry(profile, directory, descriptor) as proof:
                assert proof.live_peer_count == 1
                with pytest.raises(SharingError, match="operation_pending"):
                    migrate_sharing(
                        profile, admission=proof, clock=lambda: datetime.now(UTC)
                    )
        assert path.read_bytes() == before
    finally:
        for handle in (session, directory, descriptor):
            os.close(handle)
