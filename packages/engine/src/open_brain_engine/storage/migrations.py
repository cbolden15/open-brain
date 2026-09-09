"""Dependency-free migration values and caller-owned atomic catalog execution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import Clock

from .filesystem import StorageError


class SchemaError(StorageError):
    """The SQLite schema is unavailable or inconsistent."""


class NewerSchemaError(SchemaError):
    """The database schema is newer than this application."""


class MigrationChecksumError(SchemaError):
    """An applied migration differs from the in-code migration."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    checksum: str
    statements: tuple[str, ...]


_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
""".strip()


def _migration_checksum(version: int, name: str, statements: tuple[str, ...]) -> str:
    return sha256(
        canonical_json_bytes({"version": version, "name": name, "statements": list(statements)})
    ).hexdigest()


def _migration(version: int, name: str, statements: tuple[str, ...]) -> Migration:
    return Migration(
        version=version,
        name=name,
        checksum=_migration_checksum(version, name, statements),
        statements=statements,
    )


def _format_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SchemaError("migration clock returned invalid timestamp")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _validate_migration_set(migrations: tuple[Migration, ...], schema_version: int) -> None:
    if (
        type(schema_version) is not int
        or schema_version < 1
        or not isinstance(migrations, tuple)
        or len(migrations) != schema_version
    ):
        raise SchemaError("invalid migration set")
    for expected_version, migration in enumerate(migrations, start=1):
        if (
            not isinstance(migration, Migration)
            or migration.version != expected_version
            or not migration.name
            or not isinstance(migration.statements, tuple)
            or not migration.statements
            or any(
                not isinstance(statement, str) or not statement
                for statement in migration.statements
            )
            or migration.checksum
            != _migration_checksum(migration.version, migration.name, migration.statements)
        ):
            raise SchemaError("invalid migration set")


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    clock: Clock,
    migrations: tuple[Migration, ...],
    schema_version: int,
) -> int:
    """Apply a catalog inside the caller's transaction; never commit or acquire a lock."""
    _validate_migration_set(migrations, schema_version)
    if not connection.in_transaction:
        raise SchemaError("migration transaction is required")
    connection.execute(_SCHEMA_MIGRATIONS_SQL)
    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    versions = [int(row["version"]) for row in rows]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaError("database migration versions are not contiguous")
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version > schema_version or versions and versions[-1] > schema_version:
        raise NewerSchemaError("database schema is newer than supported")
    for row in rows:
        migration = migrations[int(row["version"]) - 1]
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise MigrationChecksumError("database migration checksum mismatch")
    for migration in migrations[len(rows) :]:
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                migration.checksum,
                _format_timestamp(clock.now()),
            ),
        )
    connection.execute(f"PRAGMA user_version = {schema_version}")
    return schema_version
