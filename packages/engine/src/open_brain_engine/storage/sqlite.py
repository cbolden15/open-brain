from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from open_brain_engine.core.ports import Clock

from .filesystem import (
    RootConfinementError,
    RootIdentity,
    StorageUnsupportedPlatformError,
    _open_parent,
    _open_root,
    _validated_parts,
)
from .migrations import (
    Migration as Migration,
)
from .migrations import (
    MigrationChecksumError as MigrationChecksumError,
)
from .migrations import (
    NewerSchemaError as NewerSchemaError,
)
from .migrations import (
    SchemaError as SchemaError,
)
from .migrations import (
    _migration,
    _validate_migration_set,
    apply_migrations,
)

SCHEMA_VERSION = 1
_SQLITE_OPEN_LOCK = threading.Lock()


class DatabaseBusyError(SchemaError):
    """SQLite could not acquire a lock within the configured busy timeout."""


def is_database_busy(error: BaseException) -> bool:
    return isinstance(error, DatabaseBusyError) or (
        isinstance(error, sqlite3.Error)
        and getattr(error, "sqlite_errorcode", 0) & 255 in (
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
        )
    )


@dataclass(frozen=True, slots=True)
class SchemaInspection:
    version: int
    valid: bool

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 0 or type(self.valid) is not bool:
            raise SchemaError("invalid schema inspection")


_EVENT_STATEMENTS = (
    """
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    capture_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    privacy_tier TEXT NOT NULL,
    privacy_reason TEXT NOT NULL,
    privacy_policy_version TEXT NOT NULL,
    privacy_confirmation_ref TEXT,
    cloud_allowed INTEGER NOT NULL CHECK (cloud_allowed IN (0, 1)),
    egress_allowed INTEGER NOT NULL CHECK (egress_allowed IN (0, 1)),
    redaction_policy_version TEXT NOT NULL,
    redaction_source_sha256 TEXT NOT NULL CHECK (length(redaction_source_sha256) = 64),
    redaction_output_sha256 TEXT NOT NULL CHECK (length(redaction_output_sha256) = 64),
    redaction_findings_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    created_at TEXT NOT NULL,
    CHECK (length(event_id) > 0),
    CHECK (length(capture_id) > 0),
    CHECK (length(event_type) > 0)
)
""".strip(),
    "CREATE INDEX events_capture_sequence_idx ON events (capture_id, sequence)",
)


MIGRATIONS = (_migration(1, "events", _EVENT_STATEMENTS),)


def _open_database_file(parent_fd: int, name: str, *, defer_setup: bool = False) -> bool:
    created = False
    try:
        if defer_setup:
            try:
                database_fd = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                database_fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
        else:
            database_fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
    except OSError:
        raise RootConfinementError("unsafe database path") from None
    try:
        if not stat.S_ISREG(os.fstat(database_fd).st_mode):
            raise RootConfinementError("unsafe database path")
        if not defer_setup:
            os.fchmod(database_fd, 0o600)
    finally:
        os.close(database_fd)
    return created


def _restrict_existing_file(parent_fd: int, name: str) -> None:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError:
        raise RootConfinementError("unsafe database path") from None
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise RootConfinementError("unsafe database path")
        if stat.S_IMODE(os.fstat(file_fd).st_mode) != 0o600:
            os.fchmod(file_fd, 0o600)
    finally:
        os.close(file_fd)


def _require_private_directory(directory_fd: int) -> None:
    if stat.S_IMODE(os.fstat(directory_fd).st_mode) & 0o022:
        raise RootConfinementError("unsafe database path")


def _connect_from_parent(parent_fd: int, name: str) -> sqlite3.Connection:
    if not hasattr(os, "fchdir"):
        raise StorageUnsupportedPlatformError("storage platform unsupported")
    original_directory_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with _SQLITE_OPEN_LOCK:
            os.fchdir(parent_fd)
            try:
                return sqlite3.connect(name, timeout=5.0, isolation_level=None)
            finally:
                os.fchdir(original_directory_fd)
    finally:
        os.close(original_directory_fd)


def connect_database(
    *,
    root: Path,
    database_name: str | PurePosixPath,
    expected_root_identity: RootIdentity | None = None,
    prepare: Callable[[sqlite3.Connection, bool, bool], None] | None = None,
) -> sqlite3.Connection:
    raw_database_name = str(database_name)
    if "%" in raw_database_name:
        raise RootConfinementError("unsafe database path")
    parts = _validated_parts(raw_database_name)
    root_fd = _open_root(root, expected_root_identity)
    parent_fd = -1
    connection = None
    try:
        _require_private_directory(root_fd)
        for depth in range(1, len(parts)):
            component_fd = _open_parent(root_fd, parts[:depth], create=True)
            try:
                _require_private_directory(component_fd)
            finally:
                os.close(component_fd)
        parent_fd = _open_parent(root_fd, parts[:-1], create=True)
        created = _open_database_file(parent_fd, parts[-1], defer_setup=prepare is not None)
        old_umask = os.umask(0o077)
        try:
            connection = _connect_from_parent(parent_fd, parts[-1])
        finally:
            os.umask(old_umask)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        setup_required = str(journal_mode).lower() != "wal"
        for suffix in ("", "-wal", "-shm"):
            try:
                mode = os.stat(parts[-1] + suffix, dir_fd=parent_fd, follow_symlinks=False).st_mode
                setup_required |= not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600
            except FileNotFoundError:
                pass
        if prepare is not None:
            prepare(connection, created, setup_required)
        if prepare is None or setup_required:
            if str(journal_mode).lower() != "wal":
                journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            for suffix in ("", "-wal", "-shm"):
                _restrict_existing_file(parent_fd, parts[-1] + suffix)
        settings = (
            connection.execute("PRAGMA foreign_keys").fetchone()[0],
            connection.execute("PRAGMA busy_timeout").fetchone()[0],
            connection.execute("PRAGMA synchronous").fetchone()[0],
        )
        if str(journal_mode).lower() != "wal" or settings != (1, 5000, 2):
            connection.close()
            raise SchemaError("required database settings unavailable")
        return connection
    except RootConfinementError, SchemaError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        if is_database_busy(error):
            raise DatabaseBusyError("database busy") from None
        raise SchemaError("database connection failed") from None
    except BaseException:
        if connection is not None:
            connection.close()
        raise
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)


def connect_database_read_only(
    *,
    root: Path,
    database_name: str | PurePosixPath,
    expected_root_identity: RootIdentity | None = None,
) -> sqlite3.Connection:
    """Open an existing root-confined SQLite database without creating or migrating it."""
    parts = _validated_parts(str(database_name))
    root_fd = _open_root(root, expected_root_identity)
    parent_fd = -1
    database_fd = -1
    try:
        _require_private_directory(root_fd)
        try:
            parent_fd = _open_parent(root_fd, parts[:-1], create=False)
            _require_private_directory(parent_fd)
            database_fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            raise SchemaError("database unavailable") from None
        metadata = os.fstat(database_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RootConfinementError("unsafe database path")
        original_directory_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            with _SQLITE_OPEN_LOCK:
                os.fchdir(parent_fd)
                try:
                    uri = f"file:{quote(parts[-1], safe='')}?mode=ro"
                    connection = sqlite3.connect(
                        uri,
                        uri=True,
                        timeout=5.0,
                        isolation_level=None,
                    )
                finally:
                    os.fchdir(original_directory_fd)
        finally:
            os.close(original_directory_fd)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection
    except RootConfinementError, SchemaError:
        raise
    except (OSError, sqlite3.Error) as error:
        if is_database_busy(error):
            raise DatabaseBusyError("database busy") from None
        raise SchemaError("database read-only connection failed") from None
    finally:
        if database_fd >= 0:
            os.close(database_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)


def has_private_rollback_journal(
    *, root: Path, database_name: str, expected_root_identity: RootIdentity
) -> bool:
    """Inspect a confined recovery candidate; SQLite still validates and rolls it back."""
    parts = _validated_parts(database_name)
    root_fd = _open_root(root, expected_root_identity)
    parent_fd = journal_fd = database_fd = -1
    try:
        parent_fd = _open_parent(root_fd, parts[:-1], create=False)
        _require_private_directory(parent_fd)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        database_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        journal_fd = os.open(parts[-1] + "-journal", flags, dir_fd=parent_fd)
        database = os.fstat(database_fd)
        journal = os.fstat(journal_fd)
        return (
            stat.S_ISREG(database.st_mode)
            and stat.S_ISREG(journal.st_mode)
            and journal.st_uid == os.geteuid()
            and journal.st_nlink == 1
            and stat.S_IMODE(journal.st_mode) & 0o077 == 0
            and 512 < journal.st_size <= 2 * database.st_size + 1_048_576
            and any(os.read(journal_fd, 8))
        )
    except OSError:
        return False
    finally:
        for descriptor in (journal_fd, database_fd, parent_fd, root_fd):
            if descriptor >= 0:
                os.close(descriptor)


def inspect_event_schema(
    *,
    root: Path,
    database_name: str | PurePosixPath,
) -> SchemaInspection:
    """Inspect the event schema and migration checksums without running migrations."""
    connection = connect_database_read_only(root=root, database_name=database_name)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        try:
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.Error:
            return SchemaInspection(version=version, valid=False)
        valid = (
            version == SCHEMA_VERSION
            and len(rows) == len(MIGRATIONS)
            and all(
                int(row["version"]) == migration.version
                and row["name"] == migration.name
                and row["checksum"] == migration.checksum
                for row, migration in zip(rows, MIGRATIONS, strict=True)
            )
        )
        return SchemaInspection(version=version, valid=valid)
    except TypeError, ValueError, sqlite3.Error:
        raise SchemaError("event schema inspection failed") from None
    finally:
        connection.close()


def migrate(
    connection: sqlite3.Connection,
    *,
    clock: Clock,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    schema_version: int = SCHEMA_VERSION,
) -> int:
    _validate_migration_set(migrations, schema_version)
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = apply_migrations(
            connection, clock=clock, migrations=migrations, schema_version=schema_version
        )
        connection.commit()
        return version
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if isinstance(error, SchemaError):
            raise
        if isinstance(error, (IndexError, KeyError, TypeError, ValueError, sqlite3.Error)):
            raise SchemaError("database migration failed") from None
        raise
