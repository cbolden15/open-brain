"""Schema23 follow-up store: coordinates, tables, migration from schema22 and fencing."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.engine import (
    BrainEngine,
    LocalEngineContext,
    StateSchemaUnavailableError,
    TextPayload,
    local_schema,
)
from open_brain_engine.engine.followup_schema import (
    FOLLOWUP_MAX_ITEMS,
    FOLLOWUP_MIGRATION,
    FOLLOWUP_SCHEMA,
    followup_store_is_consistent,
    migrate_followups,
)
from open_brain_engine.engine.local_migration import coordinate_local_migration
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.storage.migrations import SchemaError

from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session

REPO = Path(__file__).resolve().parents[5]
_SHA = sha256(b"quote").hexdigest()
_AT = "2026-10-23T00:00:00.000000Z"


def test_runtime_session_coordinate_stays_sixteen() -> None:
    """The follow-up store changes no session registry or handshake contract (Task 1)."""
    assert local_runtime_session.RUNTIME_SESSION_VERSION == 16
    collector = (
        REPO / "packages/collector/src/open_brain_collector/source_metadata.py"
    ).read_text()
    assert "_RUNTIME_COMPATIBILITY: Final = 16" in collector
    plugin = (REPO / "packages/obsidian-plugin/src/contracts.ts").read_text()
    assert "export const OPEN_BRAIN_RUNTIME_SESSION_VERSION = 16;" in plugin
    desktop = (REPO / "packages/desktop/src-tauri/src/runtime.rs").read_text()
    assert re.search(r"const SUPPORTED_RUNTIME_SESSION: u64 = 16;", desktop)


def _memory() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    # The migration rebuilds runtime_compatibility and expects the predecessor table.
    connection.execute("CREATE TABLE runtime_compatibility (singleton, a, b)")
    for statement in FOLLOWUP_SCHEMA:
        connection.execute(statement)
    return connection


def _insert(connection: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    connection.execute(
        f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' * len(row))})",
        tuple(row.values()),
    )


def _item(connection: sqlite3.Connection, item_id: str = "item-1", **overrides: object) -> None:
    row: dict[str, object] = {
        "item_id": item_id,
        "kind": "commitment",
        "project": "synthetic",
        "summary": "Send the draft",
        "owner": "self",
        "owner_name": None,
        "owner_confirmed": 0,
        "due": None,
        "due_evidence_id": None,
        "state": "suggested",
        "snoozed_until": None,
        "snooze_return_state": None,
        "ticket_key": None,
        "ticket_url": None,
        "privacy_tier": "work",
        "superseded_by": None,
        "resolution": None,
        "evidence_retracted": 0,
        "new_evidence_since_decision": 0,
        "version": 1,
        "created_at": _AT,
        "updated_at": _AT,
    }
    row.update(overrides)
    _insert(connection, "followup_items", row)


def _evidence(
    connection: sqlite3.Connection, evidence_id: str, item_id: str, **overrides: object
) -> None:
    row: dict[str, object] = {
        "evidence_id": evidence_id,
        "item_id": item_id,
        "ordinal": 0,
        "role": "origin",
        "capture_id": "cap-1",
        "revision_id": "rev-1",
        "quote_start": 0,
        "quote_end": 5,
        "quote_sha256": _SHA,
        "recorded_at": _AT,
    }
    row.update(overrides)
    _insert(connection, "followup_evidence", row)


def _decision(
    connection: sqlite3.Connection, decision_id: str, item_id: str, **overrides: object
) -> None:
    row: dict[str, object] = {
        "decision_id": decision_id,
        "item_id": item_id,
        "sequence": 1,
        "operation_id": decision_id,
        "request_sha256": _SHA,
        "principal_id": "owner",
        "action": "propose",
        "prior_state": None,
        "new_state": "suggested",
        "prior_version": 0,
        "new_version": 1,
        "evidence_ids_json": '["ev-1"]',
        "receipt_json": "{}",
        "recorded_at": _AT,
    }
    row.update(overrides)
    _insert(connection, "followup_decisions", row)


def test_followup_migration_is_the_twenty_third() -> None:
    assert (FOLLOWUP_MIGRATION.version, FOLLOWUP_MIGRATION.name) == (23, "followup-items")
    assert FOLLOWUP_MIGRATION.statements == FOLLOWUP_SCHEMA
    assert FOLLOWUP_MAX_ITEMS == 10_000
    with _memory() as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM runtime_compatibility")
        ] == [(1, 16, 23)]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'followup_%'"
            )
        }
        assert tables == {"followup_items", "followup_evidence", "followup_decisions"}


def test_consistent_store_has_one_origin_and_version_equal_to_decisions() -> None:
    with _memory() as connection:
        assert followup_store_is_consistent(connection)
        _item(connection)
        assert not followup_store_is_consistent(connection)  # no origin, no decision
        _evidence(connection, "ev-1", "item-1")
        assert not followup_store_is_consistent(connection)  # version 1 but no decision row
        _decision(connection, "op-1", "item-1")
        assert followup_store_is_consistent(connection)


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "promise"},
        {"owner": "me"},
        {"owner": "other"},  # other requires owner_name
        {"owner": "self", "owner_name": "Someone"},  # name only with other
        {"state": "open"},
        {"state": "snoozed"},  # snoozed requires snoozed_until and return state
        {"state": "suggested", "snoozed_until": "2026-11-01"},
        {"state": "snoozed", "snoozed_until": "2026-11-01", "snooze_return_state": "dismissed"},
        {"state": "superseded"},  # requires superseded_by
        {"state": "resolved"},  # requires resolution
        {"state": "suggested", "resolution": "evidence"},
        {"summary": "x" * 281},
        {"summary": None},  # withheld only when evidence_retracted=1
        {"due": "tomorrow"},
        {"due": "2026-1-1"},
        {"due": "2026-11-30"},  # a stated date names the evidence row that states it
        {"due_evidence_id": "ev-1"},  # evidence row without a date
        {"privacy_tier": "WORK"},
        {"ticket_url": "https://example.invalid/t/1"},  # URL without key
        {"version": 0},
        {"owner_confirmed": 2},
    ],
)
def test_item_check_constraints_reject_invalid_rows(overrides: dict[str, object]) -> None:
    with _memory() as connection, pytest.raises(sqlite3.IntegrityError):
        _item(connection, "item-1", **overrides)


def test_item_check_constraints_accept_each_valid_shape() -> None:
    with _memory() as connection:
        _item(
            connection,
            "a",
            owner="other",
            owner_name="Dana",
            due="2026-11-30",
            due_evidence_id="ev-a",
        )
        _item(
            connection,
            "b",
            state="snoozed",
            snoozed_until="2026-11-01",
            snooze_return_state="confirmed",
        )
        _item(connection, "c", state="resolved", resolution="owner_statement")
        _item(connection, "d", state="superseded", superseded_by="a")
        _item(connection, "e", ticket_key="T-1", ticket_url="https://example.invalid/t/1")
        _item(connection, "f", ticket_key="T-2")
        _item(connection, "g", summary=None, evidence_retracted=1)
        _item(connection, "h", kind="question", owner="unknown")


def test_items_are_retained_and_version_moves_by_exactly_one() -> None:
    with _memory() as connection:
        _item(connection)
        with pytest.raises(sqlite3.DatabaseError, match="retained"):
            connection.execute("DELETE FROM followup_items")
        for statement in (
            "UPDATE followup_items SET version=3",  # skips a version
            "UPDATE followup_items SET version=1, state='confirmed'",  # no bump
            "UPDATE followup_items SET version=2, kind='question'",  # kind is fixed
            "UPDATE followup_items SET version=2, privacy_tier='personal'",  # tier is fixed
            "UPDATE followup_items SET version=2, created_at='2026-01-01T00:00:00.000000Z'",
            "UPDATE followup_items SET version=2, item_id='other'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)
        connection.execute(
            "UPDATE followup_items SET version=2, state='confirmed', owner_confirmed=1"
        )
        assert connection.execute("SELECT version,state FROM followup_items").fetchone() == (
            2,
            "confirmed",
        )


def test_evidence_is_immutable_bounded_and_origin_unique() -> None:
    with _memory() as connection:
        _item(connection)
        _item(connection, "item-2")
        _evidence(connection, "ev-1", "item-1")
        for statement in (
            "UPDATE followup_evidence SET quote_end=6",
            "DELETE FROM followup_evidence",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):  # second origin on the same item
            _evidence(connection, "ev-2", "item-1", ordinal=1)
        with pytest.raises(sqlite3.IntegrityError):  # same origin quote on another item
            _evidence(connection, "ev-3", "item-2")
        with pytest.raises(sqlite3.IntegrityError):  # ordinal collision
            _evidence(connection, "ev-4", "item-1", role="supporting")
        with pytest.raises(sqlite3.IntegrityError):  # 51st evidence row
            _evidence(connection, "ev-5", "item-1", ordinal=50, role="supporting")
        with pytest.raises(sqlite3.IntegrityError):  # empty quote
            _evidence(connection, "ev-6", "item-1", ordinal=1, role="supporting", quote_end=0)
        with pytest.raises(sqlite3.IntegrityError):  # bad hash length
            _evidence(
                connection, "ev-7", "item-1", ordinal=1, role="supporting", quote_sha256="abc"
            )
        with pytest.raises(sqlite3.IntegrityError):  # unknown item
            _evidence(connection, "ev-8", "missing")
        _evidence(
            connection,
            "ev-9",
            "item-1",
            ordinal=1,
            role="supporting",
            quote_start=5,
            quote_end=9,
        )
        _evidence(connection, "ev-10", "item-2", capture_id="cap-2")


def test_decisions_are_append_only_sequenced_and_replay_keyed() -> None:
    with _memory() as connection:
        _item(connection)
        _decision(connection, "op-1", "item-1")
        for statement in (
            "UPDATE followup_decisions SET action='dismiss'",
            "DELETE FROM followup_decisions",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):  # operation_id is global replay key
            _decision(connection, "d-2", "item-1", sequence=2, operation_id="op-1")
        with pytest.raises(sqlite3.IntegrityError):  # sequence collision
            _decision(connection, "op-2", "item-1", sequence=1)
        with pytest.raises(sqlite3.IntegrityError):  # version must move by one
            _decision(connection, "op-3", "item-1", sequence=2, prior_version=1, new_version=3)
        with pytest.raises(sqlite3.IntegrityError):  # unknown action
            _decision(connection, "op-4", "item-1", sequence=2, action="archive")
        _decision(
            connection,
            "op-5",
            "item-1",
            sequence=2,
            action="confirm",
            prior_state="suggested",
            new_state="confirmed",
            prior_version=1,
            new_version=2,
        )


def test_item_capacity_trigger_stops_the_ten_thousand_and_first_row() -> None:
    with _memory() as connection:
        connection.execute("DROP TRIGGER followup_items_capacity")
        connection.executemany(
            "INSERT INTO followup_items(item_id,kind,summary,owner,owner_confirmed,state,"
            "privacy_tier,evidence_retracted,new_evidence_since_decision,version,created_at,"
            "updated_at) VALUES(?,'commitment','s','self',0,'suggested','work',0,0,1,?,?)",
            ((f"bulk-{index}", _AT, _AT) for index in range(FOLLOWUP_MAX_ITEMS)),
        )
        connection.execute(
            next(
                statement
                for statement in FOLLOWUP_SCHEMA
                if statement.startswith("CREATE TRIGGER followup_items_capacity")
            )
        )
        with pytest.raises(sqlite3.DatabaseError, match="capacity"):
            _item(connection, "one-too-many")


def _clock() -> datetime:
    return datetime(2026, 10, 23, tzinfo=UTC)


def _connect(profile: LocalEngineContext) -> sqlite3.Connection:
    connection = sqlite3.connect(profile.root / local_schema.PHASE1_STATE_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def _schema22(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalEngineContext:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=())
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 22)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:22])
        engine = BrainEngine.open(profile)
        engine.capture.accept(TextPayload("Synthetic schema22 evidence"), delivery_id="prior.22")
    return profile


def test_fresh_schema23_has_an_empty_follow_up_store(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    assert local_schema.inspect_phase1_state(engine.profile) == local_schema.SchemaState(
        "current", 23
    )
    assert LOCAL_MIGRATIONS[22] == FOLLOWUP_MIGRATION
    assert len(LOCAL_MIGRATIONS) == 23
    with _connect(engine.profile) as connection:
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM runtime_compatibility")
        ] == [(1, 16, 23)]
        for table in ("followup_items", "followup_evidence", "followup_decisions"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert followup_store_is_consistent(connection)


def test_schema22_migrates_and_preserves_predecessor_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _schema22(tmp_path, monkeypatch)
    with _connect(profile) as connection:
        migrations = connection.execute("SELECT * FROM schema_migrations").fetchall()
        captures = connection.execute("SELECT * FROM captures").fetchall()
        policy = connection.execute("SELECT * FROM owner_egress_policy").fetchall()
        generations = connection.execute("SELECT * FROM engine_generations").fetchall()
    with pytest.raises((SchemaError, StateSchemaUnavailableError)):
        BrainEngine.open(profile)
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 23)
    with _connect(profile) as connection:
        assert [
            tuple(row)
            for row in connection.execute("SELECT * FROM schema_migrations").fetchall()[:22]
        ] == [tuple(row) for row in migrations]
        assert [tuple(row) for row in connection.execute("SELECT * FROM captures")] == [
            tuple(row) for row in captures
        ]
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM owner_egress_policy")
        ] == [tuple(row) for row in policy]
        # Follow-ups change no projection policy, unlike #96.
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM engine_generations")
        ] == [tuple(row) for row in generations]
        assert connection.execute("SELECT count(*) FROM followup_items").fetchone()[0] == 0
        assert [
            tuple(row) for row in connection.execute("SELECT * FROM runtime_compatibility")
        ] == [(1, 16, 23)]
    coordinate_local_migration(profile, clock=_clock)  # idempotent rerun
    BrainEngine.open(profile)


@pytest.mark.parametrize("opener", ["engine", "read_only"])
def test_schema22_engine_refuses_schema23_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    profile = _schema22(tmp_path, monkeypatch)
    coordinate_local_migration(profile, clock=_clock)
    database = profile.root / local_schema.PHASE1_STATE_DATABASE
    before = database.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 22)
        previous.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:22])
        with _connect(profile) as connection:
            assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
                "newer", 23
            )
        with pytest.raises((SchemaError, StateSchemaUnavailableError), match="schema is newer"):
            if opener == "engine":
                BrainEngine.open(profile)
            else:
                local_schema.open_local_database_read_only(profile)
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "stage,version",
    [
        ("followup_preflight", 22),
        ("followup_before_commit", 22),
        ("followup_after_commit", 23),
    ],
)
def test_interrupted_cutover_is_atomic_and_rerun_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, version: int
) -> None:
    profile = _schema22(tmp_path, monkeypatch)

    def interrupt(actual: str) -> None:
        if actual == stage:
            raise RuntimeError("synthetic follow-up interruption")

    expected = local_schema.LocalSchemaUpgradeCommittedError if version == 23 else RuntimeError
    with exclusive_runtime_admission(profile) as admission, pytest.raises(expected):
        migrate_followups(profile, admission=admission, clock=_clock, checkpoint=interrupt)
    assert local_schema.inspect_phase1_state(profile).version == version
    coordinate_local_migration(profile, clock=_clock)
    coordinate_local_migration(profile, clock=_clock)
    assert local_schema.inspect_phase1_state(profile) == local_schema.SchemaState("current", 23)
    with _connect(profile) as connection:
        rows = connection.execute("SELECT count(*) FROM schema_migrations WHERE version=23")
        assert rows.fetchone()[0] == 1


def test_classifier_refuses_inconsistent_follow_up_store(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain", starter_spaces=()))
    with _connect(engine.profile) as connection:
        _item(connection)  # no origin evidence, no decision row
        assert local_schema.classify_local_schema(connection) == local_schema.SchemaState(
            "invalid", 23
        )
