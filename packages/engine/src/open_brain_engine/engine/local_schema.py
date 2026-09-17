"""Recognition and guarded opening of the local phase1 database."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from functools import cache, lru_cache, partial

from open_brain_engine.storage.migrations import (
    _SCHEMA_MIGRATIONS_SQL,
    SchemaError,
    apply_migrations,
)
from open_brain_engine.storage.sqlite import (
    DatabaseBusyError,
    begin_immediate,
    connect_database,
    connect_database_read_only,
    has_private_rollback_journal,
    is_database_busy,
    restore_busy_timeout,
)

from .contracts import LocalEngineContext
from .local_schema_catalog import (
    BASELINE,
    IMPORT_SCHEMA,
    LIVE_SEARCH_SCHEMA,
    LOCAL_MIGRATIONS,
    MANAGED_RECOVERY_SCHEMA,
    MANAGED_WORKSPACE_SCHEMA,
    REVIEW_SCHEMA,
    RUNTIME_COMPATIBILITY_SCHEMA,
    SEARCH_SCHEMA,
)
from .normalization import _MAX_FILE_BYTES, _MAX_TEXT, _utc_now
from .search_projection import _durable_source_origin, public_search_text

PHASE1_STATE_DATABASE = ".open-brain/state/phase1.sqlite3"
PHASE1_STATE_SCHEMA_VERSION = 6


class LocalRecoveryRequiredError(SchemaError):
    """A read-only observer cannot roll back an interrupted SQLite transaction."""


class LocalSchemaUpgradeCommittedError(SchemaError):
    """A schema upgrade committed before the guarded opener failed."""


@dataclass(frozen=True, slots=True)
class SchemaState:
    state: str
    version: int | None

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state, "version": self.version}


@lru_cache(maxsize=128)
def _normalized_sql(sql: str) -> str:
    # Compare fixed known DDL forms, preserving every character inside SQL string literals.
    tokens = re.findall(r"'(?:[^']|'')*'|\"[^\"]*\"|\w+|[^\s]", sql)
    normalized = [token if token.startswith("'") else token.strip('"').lower() for token in tokens]
    return " ".join(normalized).replace(" if not exists ", " ").rstrip(" ;")


def _shape(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (str(row[0]), str(row[1]), _normalized_sql(str(row[2] or "")))
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        )
    )


@cache
def _expected_shape(era: int, nullable: bool, ledger: bool) -> tuple[tuple[str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in BASELINE:
            if "CREATE TABLE IF NOT EXISTS search_documents (" in statement and not nullable:
                statement = SEARCH_SCHEMA[0]
            connection.execute(statement)
        if era >= 3:
            for statement in LIVE_SEARCH_SCHEMA:
                connection.execute(statement)
        if era >= 4:
            for statement in IMPORT_SCHEMA:
                connection.execute(statement)
        if era >= 5:
            for statement in MANAGED_WORKSPACE_SCHEMA:
                connection.execute(statement)
        if era >= 6:
            for statement in RUNTIME_COMPATIBILITY_SCHEMA:
                connection.execute(statement)
        if era >= 7:
            for statement in REVIEW_SCHEMA:
                connection.execute(statement)
        if era >= 8:
            for statement in MANAGED_RECOVERY_SCHEMA:
                connection.execute(statement)
        if ledger:
            connection.execute(_SCHEMA_MIGRATIONS_SQL)
        return _shape(connection)
    finally:
        connection.close()


def classify_local_schema(connection: sqlite3.Connection) -> SchemaState:
    """Classify within the caller's read or write transaction, without repairing state."""
    version = None
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > PHASE1_STATE_SCHEMA_VERSION:
            return SchemaState("newer", version)
        shape = _shape(connection)
        ledger = any(name == "schema_migrations" for _, name, _ in shape)
        if ledger:
            rows = connection.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            if any(type(row[0]) is int and row[0] > PHASE1_STATE_SCHEMA_VERSION for row in rows):
                return SchemaState("newer", version)
            if version not in (1, 2, 3, 4, 5, 6) or len(rows) != version:
                return SchemaState("invalid", version)
            for row, migration in zip(rows, LOCAL_MIGRATIONS[:version], strict=True):
                if tuple(row[:3]) != (migration.version, migration.name, migration.checksum):
                    return SchemaState("invalid", version)
                timestamp = datetime.strptime(row[3], "%Y-%m-%dT%H:%M:%S.%fZ")
                if timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != row[3]:
                    return SchemaState("invalid", version)
            expected = {
                1: _expected_shape(2, True, True),
                2: _expected_shape(4, False, True),
                3: _expected_shape(5, False, True),
                4: _expected_shape(6, False, True),
                5: _expected_shape(7, False, True),
                6: _expected_shape(8, False, True),
            }[version]
            if shape == expected:
                if version >= 4:
                    compatibility = connection.execute(
                        "SELECT singleton, minimum_runtime_session_version, state_schema_version "
                        "FROM runtime_compatibility"
                    ).fetchall()
                    if [tuple(row) for row in compatibility] != [(1, 1, version)]:
                        return SchemaState("invalid", version)
                return SchemaState(
                    "current" if version == PHASE1_STATE_SCHEMA_VERSION else "supported_old",
                    version,
                )
        elif version in (0, 1):
            eras = (2,) if version == 0 else (2, 3, 4, 5, 6)
            for era in eras:
                for nullable in (True,) if era == 2 else (True, False):
                    if shape == _expected_shape(era, nullable, False):
                        return SchemaState("legacy" if version == 0 else "pre_ledger", version)
    except sqlite3.Error as error:
        if is_database_busy(error):
            raise DatabaseBusyError("database busy") from None
        if getattr(error, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_READONLY:
            raise LocalRecoveryRequiredError("local state requires SQLite recovery") from None
    except TypeError, ValueError, OverflowError:
        pass
    return SchemaState("invalid", version)


def _require_supported(state: SchemaState, *, current_only: bool = False) -> None:
    if state.state in {"invalid", "newer"} or current_only and state.state != "current":
        raise SchemaError(f"local state schema is {state.state}")


def inspect_phase1_state(
    profile: LocalEngineContext, *, timeout_seconds: float = 5.0, busy_timeout_ms: int = 5000
) -> SchemaState:
    if not isinstance(profile, LocalEngineContext):
        raise ValueError("invalid local profile")
    try:
        connection = open_local_database_read_only(
            profile,
            inspect_only=True,
            timeout_seconds=timeout_seconds,
            busy_timeout_ms=busy_timeout_ms,
        )
    except DatabaseBusyError:
        raise
    except SchemaError:
        try:
            (profile.root / PHASE1_STATE_DATABASE).lstat()
        except FileNotFoundError:
            return SchemaState("absent", None)
        except OSError:
            pass
        return SchemaState("invalid", None)
    try:
        connection.execute("BEGIN")
        return classify_local_schema(connection)
    except LocalRecoveryRequiredError:
        recoverable = has_private_rollback_journal(
            root=profile.root,
            database_name=PHASE1_STATE_DATABASE,
            expected_root_identity=profile.root_identity,
        )
        return SchemaState("recovery_required" if recoverable else "invalid", None)
    finally:
        connection.close()


def open_local_database_read_only(
    profile: LocalEngineContext,
    *,
    inspect_only: bool = False,
    allow_old: bool = False,
    timeout_seconds: float = 5.0,
    busy_timeout_ms: int = 5000,
) -> sqlite3.Connection:
    connection = connect_database_read_only(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
        timeout_seconds=timeout_seconds,
        busy_timeout_ms=busy_timeout_ms,
    )
    if inspect_only:
        return connection
    try:
        connection.execute("BEGIN")
        _require_supported(classify_local_schema(connection), current_only=not allow_old)
        return connection
    except BaseException:
        connection.close()
        raise


def _validate_upgrade_data(connection: sqlite3.Connection) -> None:
    if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise SchemaError("local state integrity is invalid")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise SchemaError("local state references are invalid")
    compatibility_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_compatibility'"
    ).fetchone()
    if compatibility_table is not None:
        compatibility = connection.execute(
            "SELECT singleton, minimum_runtime_session_version, state_schema_version "
            "FROM runtime_compatibility"
        ).fetchall()
        state_version = connection.execute("PRAGMA user_version").fetchone()[0]
        compatibility_version = state_version if state_version in (5, 6) else 4
        if [tuple(row) for row in compatibility] != [(1, 1, compatibility_version)]:
            raise SchemaError("local runtime compatibility floor is invalid")
    if (
        connection.execute(
            """SELECT 1 FROM search_documents AS d LEFT JOIN captures AS c USING (capture_id)
        WHERE d.result_id IS NULL OR typeof(d.result_id) != 'text'
        OR c.capture_id IS NULL OR typeof(c.source_reference) != 'text'
        OR length(c.source_reference) = 0 OR length(c.source_reference) > ?
        OR typeof(d.title) != 'text' OR typeof(d.body) != 'text' LIMIT 1""",
            (_MAX_TEXT,),
        ).fetchone()
        is not None
    ):
        raise SchemaError("local search projection is invalid")
    for capture in connection.execute(
        """SELECT c.source_reference, c.source_origin, c.provenance_json FROM captures c
        WHERE EXISTS (SELECT 1 FROM search_documents d WHERE d.capture_id = c.capture_id)"""
    ):
        try:
            _durable_source_origin(capture)
        except ValueError:
            raise SchemaError("local search projection provenance is invalid") from None
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'markdown_import_revisions'"
        ).fetchone()
        is not None
        and connection.execute(
            """SELECT 1 FROM markdown_import_revisions r JOIN captures c USING (capture_id)
            WHERE r.delivery_id != c.delivery_id OR r.request_sha256 != c.request_sha256
            UNION ALL
            SELECT 1 FROM markdown_import_files f JOIN markdown_import_revisions r
            ON r.revision_id = f.active_revision_id WHERE r.capture_id IS NULL
            UNION ALL
            SELECT 1 FROM search_documents d JOIN markdown_import_revisions r USING (capture_id)
            JOIN markdown_import_files f USING (file_id)
            WHERE d.record_type = 'source' AND r.revision_id IS NOT f.active_revision_id
            LIMIT 1"""
        ).fetchone()
        is not None
    ):
        raise SchemaError("local import references are invalid")


def _public_text(text: object, reference: object) -> str:
    # Four bytes/characters of headroom per bounded source byte covers NFC expansion.
    if (
        not isinstance(text, str)
        or len(text) > 4 * _MAX_FILE_BYTES
        or not isinstance(reference, str)
        or not 0 < len(reference) <= _MAX_TEXT
    ):
        raise ValueError("invalid local search projection")
    return public_search_text(text, protected_source_reference=reference)


def _validate_backfill(connection: sqlite3.Connection) -> None:
    counts = [
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("search_documents", "search_fts_identity", "search_documents_fts")
    ]
    if (
        len(set(counts)) != 1
        or connection.execute(
            """SELECT 1 FROM search_documents d
        LEFT JOIN search_fts_identity i USING (result_id)
        LEFT JOIN search_documents_fts f ON f.rowid = i.fts_rowid
        WHERE i.result_id IS NULL OR f.result_id IS NOT d.result_id
        OR f.title IS NOT d.title OR f.body IS NOT d.body LIMIT 1"""
        ).fetchone()
        is not None
    ):
        raise SchemaError("local search backfill is invalid")


@dataclass(frozen=True)
class _MigrationClock:
    callback: Callable[[], datetime]

    def now(self) -> datetime:
        return self.callback()


def _prepare_local_schema(
    connection: sqlite3.Connection,
    created: bool,
    setup_required: bool,
    *,
    clock: Callable[[], datetime] = _utc_now,
    on_upgrade_committed: Callable[[], None] | None = None,
) -> None:
    try:
        if not setup_required:
            connection.execute("BEGIN")
            state = classify_local_schema(connection)
            connection.execute("COMMIT")
            restore_busy_timeout(connection)
            if state.state == "current":
                return
        begin_immediate(connection)
        state = classify_local_schema(connection)
        empty = created and state.version == 0 and not _shape(connection)
        if not empty:
            _require_supported(state)
            if state.state == "current":
                connection.execute("COMMIT")
                restore_busy_timeout(connection)
                return
            _validate_upgrade_data(connection)
        connection.create_function("local_public_search_text", 2, _public_text, deterministic=True)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS,
            schema_version=PHASE1_STATE_SCHEMA_VERSION,
        )
        _require_supported(classify_local_schema(connection), current_only=True)
        _validate_upgrade_data(connection)
        _validate_backfill(connection)
        connection.execute("COMMIT")
        if not empty and on_upgrade_committed is not None:
            on_upgrade_committed()
        restore_busy_timeout(connection)
    except BaseException as error:
        with suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        with suppress(sqlite3.Error):
            restore_busy_timeout(connection)
        if is_database_busy(error):
            raise DatabaseBusyError("database busy") from None
        if isinstance(error, SchemaError):
            raise
        if isinstance(error, (ValueError, TypeError, sqlite3.Error)):
            raise SchemaError("local state migration failed") from None
        raise
    finally:
        connection.create_function("local_public_search_text", 2, None)


def open_local_database(
    profile: LocalEngineContext, *, clock: Callable[[], datetime] = _utc_now
) -> sqlite3.Connection:
    try:
        state = inspect_phase1_state(profile, timeout_seconds=0.05, busy_timeout_ms=50)
    except DatabaseBusyError:
        state = SchemaState("busy", None)
    if state.state not in {"absent", "busy"}:
        _require_supported(state)
    upgrade_committed = False

    def record_upgrade() -> None:
        nonlocal upgrade_committed
        upgrade_committed = True

    try:
        return connect_database(
            root=profile.root,
            database_name=PHASE1_STATE_DATABASE,
            expected_root_identity=profile.root_identity,
            prepare=partial(
                _prepare_local_schema, clock=clock, on_upgrade_committed=record_upgrade
            ),
        )
    except Exception as error:
        if upgrade_committed:
            raise LocalSchemaUpgradeCommittedError(str(error)) from None
        raise


def live_search_schema_is_available(connection: sqlite3.Connection) -> bool:
    actual = {name: sql for _, name, sql in _shape(connection)}
    expected = {name: sql for _, name, sql in _expected_shape(3, False, False)}
    return all(
        actual.get(name) == sql
        for name, sql in expected.items()
        if name.startswith("search_documents_fts")
        or name in {"search_fts_identity", "search_documents_result_id_immutable"}
    )
