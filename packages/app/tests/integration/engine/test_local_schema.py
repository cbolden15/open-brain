from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.local_schema import (
    PHASE1_STATE_DATABASE,
    classify_local_schema,
    inspect_phase1_state,
    open_local_database,
    open_local_database_read_only,
)
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.storage import sqlite as storage_sqlite
from open_brain_engine.storage.sqlite import DatabaseBusyError, SchemaError

from open_brain.profile import compile_single_user_local

FIXTURES = Path(__file__).parents[5] / "tests/fixtures/local-schema"
ERAS = ("legacy", "w2", "w3", "w4", "ledger-v1")


def _fixture(root: Path, era: str, *, nullable: bool = False) -> LocalEngineContext:
    profile = compile_single_user_local(root, starter_spaces=())
    database = root / PHASE1_STATE_DATABASE
    database.parent.mkdir(parents=True, exist_ok=True)
    schema = (FIXTURES / f"{era}.sql").read_text()
    if nullable:
        schema = schema.replace("result_id TEXT PRIMARY KEY NOT NULL", "result_id TEXT PRIMARY KEY")
    connection = sqlite3.connect(database)
    try:
        connection.executescript(schema)
        connection.executescript(
            (FIXTURES / ("data-w4.sql" if era == "w4" else "data-base.sql")).read_text()
        )
    finally:
        connection.close()
    return profile


def _rows(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'search_%' "
            "AND name != 'schema_migrations' ORDER BY name"
        )
    ]
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in tables
    }


def _search(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT result_id FROM search_documents_fts WHERE search_documents_fts MATCH 'nebula' "
            "ORDER BY bm25(search_documents_fts, 0.0, 10.0, 1.0), result_id"
        )
    ]


@pytest.mark.parametrize("era", ERAS)
def test_historical_layouts_converge_without_losing_records(tmp_path: Path, era: str) -> None:
    profile = _fixture(tmp_path / era, era)
    database = profile.root / PHASE1_STATE_DATABASE
    with sqlite3.connect(database) as before:
        records = _rows(before)
        results = before.execute(
            "SELECT result_id FROM search_documents ORDER BY result_id"
        ).fetchall()
        ordered = _search(before) if era in {"w3", "w4"} else None
    expected_state = (
        "legacy" if era == "legacy" else "supported_old" if era == "ledger-v1" else "pre_ledger"
    )
    assert inspect_phase1_state(profile).state == expected_state
    before_bytes = database.read_bytes()
    with pytest.raises(SchemaError):
        open_local_database_read_only(profile)
    assert database.read_bytes() == before_bytes
    upgraded = open_local_database(profile)
    try:
        assert classify_local_schema(upgraded).state == "current"
        after_records = _rows(upgraded)
        assert all(after_records[table] == values for table, values in records.items())
        assert [
            tuple(row)
            for row in upgraded.execute("SELECT result_id FROM search_documents ORDER BY result_id")
        ] == results
        assert len(_search(upgraded)) == len(results)
        if ordered is not None:
            assert _search(upgraded) == ordered
        ledger = [tuple(row) for row in upgraded.execute("SELECT * FROM schema_migrations")]
        assert [row[0] for row in ledger] == [1, 2]
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 2
        snapshot = list(upgraded.iterdump())
    finally:
        upgraded.close()
    reopened = open_local_database(profile)
    try:
        assert list(reopened.iterdump()) == snapshot
        assert [tuple(row) for row in reopened.execute("SELECT * FROM schema_migrations")] == ledger
        current_shape = reopened.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    finally:
        reopened.close()
    fresh = open_local_database(compile_single_user_local(tmp_path / "fresh", starter_spaces=()))
    try:
        assert (
            fresh.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            == current_shape
        )
    finally:
        fresh.close()


@pytest.mark.parametrize("era", ("w3", "w4"))
def test_w2_upgraded_search_declaration_is_normalized(tmp_path: Path, era: str) -> None:
    profile = _fixture(tmp_path / era, era, nullable=True)
    assert inspect_phase1_state(profile).state == "pre_ledger"
    connection = open_local_database(profile)
    try:
        assert connection.execute("PRAGMA table_info(search_documents)").fetchone()[3] == 1
        assert classify_local_schema(connection).state == "current"
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement",
    (
        "PRAGMA user_version=3",
        "PRAGMA user_version=2",
        "CREATE TABLE unexpected (id TEXT)",
        "DROP INDEX route_identity_idx",
        "ALTER TABLE captures ADD COLUMN unexpected TEXT",
        "CREATE TRIGGER unexpected AFTER INSERT ON captures BEGIN SELECT 1; END",
        "UPDATE search_documents SET result_id=NULL",
        "UPDATE search_documents SET capture_id='orphan'",
        "UPDATE captures SET source_reference=''",
    ),
)
@pytest.mark.parametrize("wal", (False, True))
def test_invalid_or_newer_input_is_refused_before_mutation(
    tmp_path: Path, statement: str, wal: bool
) -> None:
    profile = _fixture(tmp_path / "brain", "w2")
    database = profile.root / PHASE1_STATE_DATABASE
    held = sqlite3.connect(database, isolation_level=None)
    try:
        if wal:
            held.execute("PRAGMA journal_mode=WAL")
        held.execute(statement)
        database.chmod(0o644)
        before = database.read_bytes()
        logical = list(held.iterdump())
        with pytest.raises(SchemaError):
            open_local_database(profile)
        assert database.read_bytes() == before
        assert list(held.iterdump()) == logical
        assert database.stat().st_mode & 0o777 == 0o644
    finally:
        held.close()


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE schema_migrations SET checksum='wrong'",
        "UPDATE schema_migrations SET version=2",
        "UPDATE schema_migrations SET version=3",
        "UPDATE schema_migrations SET name='events'",
        "UPDATE schema_migrations SET applied_at='yesterday'",
        "DELETE FROM schema_migrations",
        "PRAGMA user_version=0",
    ),
)
def test_bad_ledger_is_never_repaired(tmp_path: Path, statement: str) -> None:
    profile = _fixture(tmp_path / "brain", "ledger-v1")
    database = profile.root / PHASE1_STATE_DATABASE
    with sqlite3.connect(database) as connection:
        connection.execute(statement)
    before = database.read_bytes()
    assert inspect_phase1_state(profile).state in {"invalid", "newer"}
    with pytest.raises(SchemaError):
        open_local_database(profile)
    assert database.read_bytes() == before


@pytest.mark.parametrize("point", ("ddl", "ledger", "clear", "backfill", "commit"))
def test_migration_failure_rolls_back_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    profile = _fixture(tmp_path / "brain", "w2")
    database = profile.root / PHASE1_STATE_DATABASE
    before = database.read_bytes()
    original = storage_sqlite._connect_from_parent
    fired = False

    def instrument(parent: int, name: str) -> sqlite3.Connection:
        connection = original(parent, name)

        def deny(
            action: int, table: str | None, column: str | None, db: str | None, trigger: str | None
        ) -> int:
            nonlocal fired
            target = {
                "ddl": (sqlite3.SQLITE_CREATE_TABLE, "markdown_import_roots"),
                "ledger": (sqlite3.SQLITE_INSERT, "schema_migrations"),
                "clear": (sqlite3.SQLITE_DELETE, "search_documents_fts"),
                "backfill": (sqlite3.SQLITE_INSERT, "search_documents_fts"),
                "commit": (sqlite3.SQLITE_TRANSACTION, "COMMIT"),
            }[point]
            if not fired and (action, table) == target:
                fired = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(storage_sqlite, "_connect_from_parent", instrument)
        with pytest.raises(SchemaError):
            open_local_database(profile)
    assert fired
    assert database.read_bytes() == before
    assert inspect_phase1_state(profile).state == "pre_ledger"
    open_local_database(profile).close()
    assert inspect_phase1_state(profile).state == "current"


def test_backfill_projects_text_and_rollback_covers_callback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import local_schema

    profile = _fixture(tmp_path / "brain", "w2")
    database = profile.root / PHASE1_STATE_DATABASE
    protected = "synthetic-protected-reference"
    protected_prefix = "/" + "/".join(("Users", "synthetic"))
    raw = (
        "Cafe\u0301 nebula "
        + protected
        + " token=migration-secret "
        + protected_prefix
        + "/private "
        + "a" * 64
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE captures SET source_reference=?, "
            "provenance_json=json_set(provenance_json, '$.source_ref', ?)",
            (protected, protected),
        )
        connection.execute("UPDATE search_documents SET title=?,body=?", (raw, raw))
    before = database.read_bytes()

    def fail(text: object, reference: object) -> str:
        raise ValueError("synthetic callback failure")

    with monkeypatch.context() as patch:
        patch.setattr(local_schema, "_public_text", fail)
        with pytest.raises(SchemaError, match="migration failed"):
            open_local_database(profile)
    assert database.read_bytes() == before
    connection = open_local_database(profile)
    try:
        for table in ("search_documents", "search_documents_fts"):
            for row in connection.execute(f"SELECT title,body FROM {table}"):
                for text in row:
                    assert "Café nebula" in text
                    for unsafe in (protected, "migration-secret", protected_prefix, "a" * 64):
                        assert unsafe not in text
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("SELECT local_public_search_text('x','y')")
    finally:
        connection.close()


def test_catalog_checksums_are_frozen() -> None:
    assert {m.name: m.checksum for m in LOCAL_MIGRATIONS} == json.loads(
        (FIXTURES / "catalog-checksums.json").read_text()
    )


def test_concurrent_upgraders_observe_one_committed_ledger(tmp_path: Path) -> None:
    profile = _fixture(tmp_path / "brain", "w4")
    script = """import sys
from open_brain.profile import open_existing_single_user_local
from open_brain_engine.engine.local_schema import open_local_database
from pathlib import Path
open_local_database(open_existing_single_user_local(Path(sys.argv[1]))).close()
"""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(profile.root)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            _, stderr = worker.communicate(timeout=20)
            assert worker.returncode == 0, stderr.decode()
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
    connection = open_local_database(profile)
    try:
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
    finally:
        connection.close()


def test_reader_keeps_validated_snapshot_when_another_writer_changes_version(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine.local_store import _LocalStore

    profile = _fixture(tmp_path / "brain", "w3")
    store = _LocalStore(profile)
    reader = store.connect()
    try:
        assert reader.in_transaction
        with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as writer:
            writer.execute("PRAGMA user_version=3")
            writer.execute("DELETE FROM search_documents")
        assert reader.execute("PRAGMA user_version").fetchone()[0] == 2
        assert len(_search(reader)) == 2
    finally:
        reader.close()
    with pytest.raises(SchemaError, match="newer"):
        store.connect()


def test_write_revalidates_after_open_before_application_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import local_store

    profile = _fixture(tmp_path / "brain", "w3")
    store = local_store._LocalStore(profile)
    original = open_local_database

    def raced(profile: LocalEngineContext, **kwargs: Any) -> sqlite3.Connection:
        connection = original(profile, **kwargs)
        with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as writer:
            writer.execute("PRAGMA user_version=3")
        return connection

    monkeypatch.setattr(local_store, "open_local_database", raced)
    with pytest.raises(SchemaError, match="changed before write"), store.transaction():
        pytest.fail("application SQL must not run")


def test_invalid_locked_recheck_does_not_change_journal_mode_or_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _fixture(tmp_path / "brain", "w2")
    database = profile.root / PHASE1_STATE_DATABASE
    original = storage_sqlite._connect_from_parent
    changed_bytes = b""

    def raced(parent: int, name: str) -> sqlite3.Connection:
        nonlocal changed_bytes
        connection = original(parent, name)
        connection.execute("PRAGMA user_version=3")
        changed_bytes = database.read_bytes()
        return connection

    database.chmod(0o644)
    monkeypatch.setattr(storage_sqlite, "_connect_from_parent", raced)
    with pytest.raises(SchemaError, match="newer"):
        open_local_database(profile)
    assert database.read_bytes() == changed_bytes
    assert database.stat().st_mode & 0o777 == 0o644
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_configuration_failure_after_commit_is_retryable_without_reapplying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _fixture(tmp_path / "brain", "w2")

    def fail(parent: int, name: str) -> None:
        raise OSError("synthetic configuration failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage_sqlite, "_restrict_existing_file", fail)
        with pytest.raises(SchemaError, match="database connection failed"):
            open_local_database(profile)
    assert inspect_phase1_state(profile).state == "current"
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        ledger = connection.execute("SELECT * FROM schema_migrations").fetchall()
    reopened = open_local_database(profile)
    try:
        assert [tuple(row) for row in reopened.execute("SELECT * FROM schema_migrations")] == ledger
    finally:
        reopened.close()


@pytest.mark.parametrize("payload", (b"", b"synthetic corrupt SQLite"))
def test_existing_empty_or_corrupt_file_is_not_bootstrap(tmp_path: Path, payload: bytes) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    database = profile.root / PHASE1_STATE_DATABASE
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(payload)
    with pytest.raises(SchemaError, match="invalid"):
        open_local_database(profile)
    assert database.read_bytes() == payload


def _crash_upgrade(profile: LocalEngineContext, *, spill: bool = False) -> None:
    script = """import os, sqlite3, sys
from pathlib import Path
from open_brain.profile import open_existing_single_user_local
from open_brain_engine.engine.local_schema import open_local_database
from open_brain_engine.storage import sqlite as storage
original = storage._connect_from_parent
def instrument(parent, name):
    connection = original(parent, name)
    if sys.argv[2] == "spill":
        connection.execute("PRAGMA cache_size=1")
    def interrupt(action, table, column, db, trigger):
        if action == sqlite3.SQLITE_INSERT and table == 'search_documents_fts':
            os._exit(91)
        return sqlite3.SQLITE_OK
    connection.set_authorizer(interrupt)
    return connection
storage._connect_from_parent = instrument
open_local_database(open_existing_single_user_local(Path(sys.argv[1])))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(profile.root), "spill" if spill else "normal"],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 91, result.stderr.decode()


def test_interrupted_wal_upgrade_reopens_old_state_then_retries(tmp_path: Path) -> None:
    profile = _fixture(tmp_path / "brain", "w4")
    database = profile.root / PHASE1_STATE_DATABASE
    held = sqlite3.connect(database, isolation_level=None)
    try:
        held.execute("PRAGMA journal_mode=WAL")
        before = list(held.iterdump())
        _crash_upgrade(profile)
        assert list(held.iterdump()) == before
        assert inspect_phase1_state(profile).state == "pre_ledger"
        open_local_database(profile).close()
        assert inspect_phase1_state(profile).state == "current"
    finally:
        held.close()


def test_hot_rollback_journal_recovers_before_migration_retry(tmp_path: Path) -> None:
    profile = _fixture(tmp_path / "brain", "w4")
    database = profile.root / PHASE1_STATE_DATABASE
    database.chmod(0o600)
    with sqlite3.connect(database) as connection:
        before = _rows(connection)
    _crash_upgrade(profile, spill=True)
    journal = database.with_name(database.name + "-journal")
    assert journal.stat().st_mode & 0o777 == 0o600
    damaged_bytes = database.read_bytes()
    assert inspect_phase1_state(profile).state == "recovery_required"
    assert database.read_bytes() == damaged_bytes
    with pytest.raises(SchemaError, match="recovery"):
        open_local_database_read_only(profile)
    recovered = open_local_database(profile)
    try:
        assert classify_local_schema(recovered).state == "current"
        assert _rows(recovered) == before
        assert recovered.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        recovered.close()


def test_unsafe_hot_journal_is_refused_without_recovery(tmp_path: Path) -> None:
    profile = _fixture(tmp_path / "brain", "w4")
    database = profile.root / PHASE1_STATE_DATABASE
    _crash_upgrade(profile, spill=True)
    journal = database.with_name(database.name + "-journal")
    journal.chmod(0o644)
    before = (database.read_bytes(), journal.read_bytes())
    assert inspect_phase1_state(profile).state == "invalid"
    with pytest.raises(SchemaError, match="invalid"):
        open_local_database(profile)
    assert (database.read_bytes(), journal.read_bytes()) == before


def test_pending_import_reservation_survives_upgrade(tmp_path: Path) -> None:
    profile = _fixture(tmp_path / "brain", "w4")
    database = profile.root / PHASE1_STATE_DATABASE
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO markdown_import_files
            (file_id, root_id, relative_path, last_observed_scan_id)
            SELECT 'pending-file', root_id, 'pending.md', last_complete_scan_id
            FROM markdown_import_roots"""
        )
        connection.execute(
            """INSERT INTO markdown_import_revisions
            (revision_id, file_id, content_sha256, delivery_id, request_sha256, first_observed_at)
            VALUES ('pending-revision', 'pending-file', ?, 'pending-delivery', ?,
                    '2026-09-09T00:00:00Z')""",
            ("b" * 64, "c" * 64),
        )
        before = _rows(connection)
    upgraded = open_local_database(profile)
    try:
        assert _rows(upgraded) == before
        assert len(_search(upgraded)) == 3
    finally:
        upgraded.close()


def test_upgrade_writer_contention_is_bounded_and_retry_safe(tmp_path: Path) -> None:
    profile = _fixture(tmp_path / "brain", "w3")
    held = sqlite3.connect(profile.root / PHASE1_STATE_DATABASE, isolation_level=None)
    try:
        held.execute("PRAGMA journal_mode=WAL")
        held.execute("BEGIN IMMEDIATE")
        before = list(held.iterdump())
        start = time.monotonic()
        with pytest.raises(DatabaseBusyError, match="database busy"):
            open_local_database(profile)
        assert 4 <= time.monotonic() - start < 10
        assert list(held.iterdump()) == before
        held.execute("ROLLBACK")
    finally:
        held.close()
    open_local_database(profile).close()


def test_migration_uses_injected_clock_and_preserves_old_ledger_timestamp(tmp_path: Path) -> None:
    profile = _fixture(tmp_path / "brain", "ledger-v1")
    now = datetime(2026, 9, 10, 1, 2, 3, tzinfo=UTC)
    connection = open_local_database(profile, clock=lambda: now)
    try:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT applied_at FROM schema_migrations ORDER BY version"
            )
        ] == ["2026-09-09T00:00:00.000000Z", "2026-09-10T01:02:03.000000Z"]
    finally:
        connection.close()


def test_sql_records_match_the_shared_semantic_dataset(tmp_path: Path) -> None:
    dataset = json.loads((FIXTURES / "dataset.json").read_text())
    profile = _fixture(tmp_path / "brain", "w4")
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT search_text FROM captures WHERE payload_family='text' ORDER BY delivery_id"
            )
        ] == dataset["captures"]
        assert {
            row[0].decode("utf-8")
            for row in connection.execute(
                "SELECT file_bytes FROM captures WHERE file_bytes IS NOT NULL"
            )
        } == {*dataset["markdown"].values(), dataset["changed"]}


def test_w4_reviewed_import_preserves_portable_bytes_and_search_trust_across_upgrade(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine import BrainEngine, DecisionOutcome, ProposalDraft

    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    engine = BrainEngine.open(profile)
    vault = tmp_path / "vault"
    vault.mkdir()
    dataset = json.loads((FIXTURES / "dataset.json").read_text())
    for name, text in dataset["markdown"].items():
        (vault / name).write_text(text)
    engine.markdown_import.import_directory(str(vault), confirm=lambda _: True)
    capture = engine.retrieval.search("active nebula")[0]
    engine.inbox.route(
        capture.capture_id, engine.inbox.spaces()[0].space_id, delivery_id="schema.review.route"
    )
    proposal = engine.review.propose(
        capture.capture_id,
        (ProposalDraft("Reviewed migration", "canonical nebula proof"),),
        delivery_id="schema.review.propose",
    )[0]
    engine.review.decide(
        proposal.proposal_id, DecisionOutcome.APPROVED, delivery_id="schema.review.decide"
    )
    before = tmp_path / "before"
    engine.portability.export(before, export_id="export_00000000-0000-4000-8000-000000000991")
    engine.portability.validate(before)
    expected_results = [
        (result.result_id, result.trust, result.title)
        for result in engine.retrieval.search("nebula")
    ]
    with sqlite3.connect(profile.root / PHASE1_STATE_DATABASE) as connection:
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("PRAGMA user_version=1")
    reopened = BrainEngine.open(profile)
    assert [
        (result.result_id, result.trust, result.title)
        for result in reopened.retrieval.search("nebula")
    ] == expected_results
    assert reopened.retrieval.search("canonical nebula")[0].trust == "unverified"
    after = tmp_path / "after"
    reopened.portability.export(after, export_id="export_00000000-0000-4000-8000-000000000992")
    reopened.portability.validate(after)

    def records(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and path.name != "portable-manifest.json"
        }

    assert records(after) == records(before)
    assert json.loads((after / "portable-manifest.json").read_text())["schema_version"] == 1
    assert not any("sqlite" in name for name in records(after))


@pytest.mark.parametrize(
    "unsafe", ("symlink", "hardlink", "owner", "zero", "short", "large", "fifo")
)
def test_recovery_candidate_checks_are_confined_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    profile = _fixture(tmp_path / "brain", "w4")
    database = profile.root / PHASE1_STATE_DATABASE
    journal = database.with_name(database.name + "-journal")
    journal.write_bytes(b"synthetic-journal" + b"x" * 512)
    journal.chmod(0o600)

    def candidate() -> bool:
        return storage_sqlite.has_private_rollback_journal(
            root=profile.root,
            database_name=PHASE1_STATE_DATABASE,
            expected_root_identity=profile.root_identity,
        )

    assert candidate()
    if unsafe == "symlink":
        target = journal.with_name("synthetic-target")
        journal.rename(target)
        journal.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(journal, journal.with_name("synthetic-link"))
    elif unsafe == "owner":
        different_owner = os.geteuid() + 1
        monkeypatch.setattr(os, "geteuid", lambda: different_owner)
    elif unsafe == "zero":
        journal.write_bytes(b"\0" * 1024)
    elif unsafe == "short":
        journal.write_bytes(b"x" * 512)
    elif unsafe == "large":
        journal.write_bytes(b"x" * (2 * database.stat().st_size + 1_048_577))
    else:
        journal.unlink()
        os.mkfifo(journal)
    before = database.read_bytes()
    assert not candidate()
    assert database.read_bytes() == before
