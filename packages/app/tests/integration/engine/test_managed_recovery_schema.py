from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.local_schema import (
    PHASE1_STATE_DATABASE,
    PHASE1_STATE_SCHEMA_VERSION,
    classify_local_schema,
    inspect_phase1_state,
    open_local_database,
)
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.storage import sqlite as storage_sqlite
from open_brain_engine.storage.migrations import NewerSchemaError, apply_migrations
from open_brain_engine.storage.sqlite import SchemaError

from open_brain.profile import compile_single_user_local
from tests.unit.storage._factories import FixedClock

FIXTURE = Path(__file__).parents[5] / "tests/fixtures/local-schema/ledger-v5.sql"


def _restore_v5(root: Path) -> LocalEngineContext:
    profile = compile_single_user_local(root, starter_spaces=())
    database = root / PHASE1_STATE_DATABASE
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(FIXTURE.read_text())
    database.chmod(0o600)  # SQLite inherits this mode for a hot rollback journal.
    return profile


def _insert_workspace(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO managed_workspaces
        (workspace_id, root_path, device, inode, owner_actor_id,
         origin_owner_actor_id, created_at)
        VALUES ('workspace-schema', '/synthetic/workspace', '1', '2',
                'actor-schema', 'actor-schema', '2026-09-17T00:00:00Z')"""
    )


def _insert_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    kind: str = "materialize",
    status: str = "prepared",
) -> None:
    completed_at = "2026-09-17T00:01:00Z" if status in {"completed", "cancelled"} else None
    connection.execute(
        """INSERT INTO managed_operations
        (operation_id, request_sha256, workspace_id, kind, caller_actor_id,
         target_relative_path, status, stage, created_at, completed_at)
        VALUES (?, ?, 'workspace-schema', ?, 'actor-schema', 'synthetic.md', ?, 0,
                '2026-09-17T00:00:00Z', ?)""",
        (operation_id, "a" * 64, kind, status, completed_at),
    )


def _populated_v5(root: Path) -> tuple[LocalEngineContext, list[tuple[object, ...]]]:
    profile = _restore_v5(root)
    with sqlite3.connect(root / PHASE1_STATE_DATABASE) as connection:
        _insert_workspace(connection)
        _insert_operation(connection, "setup-pending", kind="setup")
        _insert_operation(connection, "materialize-terminal", status="completed")
        _insert_operation(connection, "refresh-pending", kind="refresh")
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM managed_operations ORDER BY operation_id"
            )
        ]
    return profile, before


def test_populated_v5_upgrade_marks_every_historical_write_without_descriptors(
    tmp_path: Path,
) -> None:
    profile, operations_before = _populated_v5(tmp_path / "brain")
    assert inspect_phase1_state(profile).state == "supported_old"

    upgraded = open_local_database(profile)
    try:
        assert PHASE1_STATE_SCHEMA_VERSION == 6
        assert classify_local_schema(upgraded).state == "current"
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 6
        assert [tuple(row) for row in upgraded.execute("SELECT * FROM runtime_compatibility")] == [
            (1, 1, 6)
        ]
        assert [
            tuple(row)
            for row in upgraded.execute(
                "SELECT operation_id, authority_version, descriptor_json, descriptor_sha256 "
                "FROM managed_write_authority ORDER BY operation_id"
            )
        ] == [
            ("materialize-terminal", 0, None, None),
            ("setup-pending", 0, None, None),
        ]
        assert [
            tuple(row)
            for row in upgraded.execute(
                "SELECT * FROM managed_operations ORDER BY operation_id"
            )
        ] == operations_before
        assert (
            upgraded.execute("SELECT count(*) FROM managed_recovery_decisions").fetchone()[0]
            == 0
        )
        snapshot = list(upgraded.iterdump())
        ledger = [tuple(row) for row in upgraded.execute("SELECT * FROM schema_migrations")]
    finally:
        upgraded.close()

    reopened = open_local_database(profile)
    try:
        assert list(reopened.iterdump()) == snapshot
        assert [tuple(row) for row in reopened.execute("SELECT * FROM schema_migrations")] == ledger
        _insert_operation(reopened, "future-materialize")
        assert (
            reopened.execute(
                "SELECT 1 FROM managed_write_authority WHERE operation_id='future-materialize'"
            ).fetchone()
            is None
        )
    finally:
        reopened.close()


def test_schema6_table_shapes_and_database_constraints(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    connection = open_local_database(profile)
    try:
        authority_columns = connection.execute(
            "PRAGMA table_info(managed_write_authority)"
        )
        assert [row[1] for row in authority_columns] == [
            "operation_id",
            "authority_version",
            "descriptor_json",
            "descriptor_sha256",
        ]
        assert [
            row[1] for row in connection.execute("PRAGMA table_info(managed_recovery_decisions)")
        ] == [
            "recovery_request_id",
            "target_operation_id",
            "actor_id",
            "workspace_id",
            "preview_sha256",
            "request_sha256",
            "snapshot_json",
            "decision",
            "completed_at",
        ]
        _insert_workspace(connection)
        for operation_id in ("legacy", "bound", "invalid", "decision", "decision-invalid"):
            _insert_operation(connection, operation_id)

        connection.execute(
            "INSERT INTO managed_write_authority VALUES ('legacy', 0, NULL, NULL)"
        )
        connection.execute(
            "INSERT INTO managed_write_authority VALUES ('bound', 1, ?, ?)",
            ('{"kind":"materialize"}', "b" * 64),
        )
        invalid_authorities = (
            (2, None, None),
            (0, "{}", None),
            (0, None, "b" * 64),
            (1, None, None),
            (1, "[]", "b" * 64),
            (1, "not-json", "b" * 64),
            (1, "{}", "short"),
        )
        for version, descriptor, digest in invalid_authorities:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO managed_write_authority VALUES ('invalid', ?, ?, ?)",
                    (version, descriptor, digest),
                )

        decision = (
            "recovery-1",
            "decision",
            "actor-schema",
            "workspace-schema",
            "c" * 64,
            "d" * 64,
            '{"operation_id":"decision"}',
            "owner_abandon_unverifiable_legacy_write",
            "2026-09-17T00:02:00Z",
        )
        connection.execute(
            "INSERT INTO managed_recovery_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            decision,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO managed_recovery_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("recovery-2", *decision[1:]),
            )
        invalid_decisions = (
            ("short", "d" * 64, "{}", "owner_abandon_unverifiable_legacy_write"),
            ("c" * 64, "short", "{}", "owner_abandon_unverifiable_legacy_write"),
            ("c" * 64, "d" * 64, "[]", "owner_abandon_unverifiable_legacy_write"),
            ("c" * 64, "d" * 64, "not-json", "owner_abandon_unverifiable_legacy_write"),
            ("c" * 64, "d" * 64, '{"value":"' + "x" * 16_384 + '"}',
             "owner_abandon_unverifiable_legacy_write"),
            ("c" * 64, "d" * 64, "{}", "different"),
        )
        for index, (preview, request, snapshot_json, outcome) in enumerate(invalid_decisions):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO managed_recovery_decisions
                    VALUES (?, 'decision-invalid', 'actor-schema', 'workspace-schema',
                            ?, ?, ?, ?, '2026-09-17T00:02:00Z')""",
                    (f"invalid-{index}", preview, request, snapshot_json, outcome),
                )
    finally:
        connection.close()


def test_v5_migration_failure_is_atomic_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, _ = _populated_v5(tmp_path / "brain")
    database = profile.root / PHASE1_STATE_DATABASE
    before = database.read_bytes()
    original = storage_sqlite._connect_from_parent
    fired = False

    def instrument(parent: int, name: str) -> sqlite3.Connection:
        connection = original(parent, name)

        def deny(action: int, table: str | None, *_args: object) -> int:
            nonlocal fired
            if action == sqlite3.SQLITE_INSERT and table == "managed_write_authority":
                fired = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny)
        return connection

    monkeypatch.setattr(storage_sqlite, "_connect_from_parent", instrument)
    with pytest.raises(SchemaError):
        open_local_database(profile)
    assert fired
    assert database.read_bytes() == before
    assert inspect_phase1_state(profile).state == "supported_old"

    monkeypatch.setattr(storage_sqlite, "_connect_from_parent", original)
    retried = open_local_database(profile)
    try:
        assert classify_local_schema(retried).state == "current"
    finally:
        retried.close()


def test_interrupted_v5_migration_recovers_old_state_and_retries(tmp_path: Path) -> None:
    profile, operations_before = _populated_v5(tmp_path / "brain")
    script = """import os, sqlite3, sys
from pathlib import Path
from open_brain.profile import open_existing_single_user_local
from open_brain_engine.engine.local_schema import open_local_database
from open_brain_engine.storage import sqlite as storage
original = storage._connect_from_parent
def instrument(parent, name):
    connection = original(parent, name)
    connection.execute('PRAGMA cache_size=1')
    def interrupt(action, table, column, db, trigger):
        if action == sqlite3.SQLITE_INSERT and table == 'managed_write_authority':
            os._exit(92)
        return sqlite3.SQLITE_OK
    connection.set_authorizer(interrupt)
    return connection
storage._connect_from_parent = instrument
open_local_database(open_existing_single_user_local(Path(sys.argv[1])))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(profile.root)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 92, result.stderr.decode()
    assert inspect_phase1_state(profile).state in {"recovery_required", "supported_old"}

    recovered = open_local_database(profile)
    try:
        assert classify_local_schema(recovered).state == "current"
        assert [
            tuple(row)
            for row in recovered.execute("SELECT * FROM managed_operations ORDER BY operation_id")
        ] == operations_before
    finally:
        recovered.close()


def test_schema6_refuses_old_runtime_and_invalid_or_newer_inputs(tmp_path: Path) -> None:
    profile = _restore_v5(tmp_path / "old-runtime")
    current = open_local_database(profile)
    current.close()
    database = profile.root / PHASE1_STATE_DATABASE
    with sqlite3.connect(database) as old_runtime:
        old_runtime.row_factory = sqlite3.Row
        before = list(old_runtime.iterdump())
        old_runtime.execute("BEGIN IMMEDIATE")
        with pytest.raises(NewerSchemaError, match="newer than supported"):
            apply_migrations(
                old_runtime,
                clock=FixedClock(),
                migrations=LOCAL_MIGRATIONS[:5],
                schema_version=5,
            )
        old_runtime.execute("ROLLBACK")
        assert list(old_runtime.iterdump()) == before

    for name, make_current, statement, expected_state in (
        ("missing-authority", True, "DROP TABLE managed_write_authority", "invalid"),
        ("invalid", False, "CREATE TABLE unexpected (id TEXT)", "invalid"),
        ("newer", False, "PRAGMA user_version=7", "newer"),
    ):
        candidate = _restore_v5(tmp_path / name)
        if make_current:
            open_local_database(candidate).close()
        candidate_database = candidate.root / PHASE1_STATE_DATABASE
        with sqlite3.connect(candidate_database) as connection:
            connection.execute(statement)
        candidate_before = candidate_database.read_bytes()
        assert inspect_phase1_state(candidate).state == expected_state
        with pytest.raises(SchemaError, match=expected_state):
            open_local_database(candidate)
        assert candidate_database.read_bytes() == candidate_before
