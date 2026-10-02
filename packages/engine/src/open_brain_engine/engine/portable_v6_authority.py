"""Snapshot serialization and transactional restore of Portable v6 authority."""

from __future__ import annotations

import base64
import sqlite3
from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.portable.v6 import (
    AUTHORITY_COLUMNS,
    SOURCE_ADMISSION_PATH,
    SOURCE_LIFECYCLE_PATH,
)


def source_authority_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    if (
        not connection.in_transaction
        or connection.execute("PRAGMA user_version").fetchone()[0] != 11
    ):
        raise ValueError("Portable v6 requires an active schema-eleven snapshot")
    value: dict[str, Any] = {
        "schema_version": 1,
        "control_epoch": connection.execute(
            "SELECT control_epoch FROM engine_generations"
        ).fetchone()[0],
    }
    for table, columns in AUTHORITY_COLUMNS.items():
        names = [column.split(":")[0] for column in columns]
        actual = "source_revisions" if table == "revision_admission" else table
        order = "delivery_id" if table == "source_intakes" else names[0]
        rows = []
        for row in connection.execute(
            f"SELECT {','.join(names)} FROM {actual} ORDER BY {order} COLLATE BINARY"  # noqa: S608
        ):
            result = dict(row)
            for column in columns:
                name, kind = column.split(":")
                if kind == "b":
                    result[name] = base64.b64encode(result[name]).decode("ascii")
            rows.append(result)
        value[table] = rows
    return value


def source_authority_sidecars(connection: sqlite3.Connection) -> dict[str, bytes]:
    value = source_authority_metadata(connection)
    lifecycle = {
        "schema_version": 1,
        "source_lifecycle_state": value.pop("source_lifecycle_state"),
        "source_lifecycle_operations": value.pop("source_lifecycle_operations"),
    }
    return {
        SOURCE_LIFECYCLE_PATH: portable_canonical_json_bytes(lifecycle),
        SOURCE_ADMISSION_PATH: portable_canonical_json_bytes(value),
    }


def restore_source_authority(connection: sqlite3.Connection, value: dict[str, Any]) -> None:
    """Install only previously validated archive rows within base restore's transaction."""
    for table, columns in AUTHORITY_COLUMNS.items():
        names = [column.split(":")[0] for column in columns]
        for row in value[table]:
            if table == "revision_admission":
                connection.execute(
                    "UPDATE source_revisions SET request_sha256=?,revision_key=?,ordering_json=? "
                    "WHERE capture_id=?",
                    (
                        row["request_sha256"],
                        row["revision_key"],
                        row["ordering_json"],
                        row["capture_id"],
                    ),
                )
            elif table == "source_lifecycle_state":
                connection.execute(
                    "UPDATE source_lifecycle_state SET lifecycle_version=? WHERE source_id=?",
                    (row["lifecycle_version"], row["source_id"]),
                )
            else:
                values = [
                    base64.b64decode(row[name], validate=True)
                    if column.endswith(":b")
                    else row[name]
                    for name, column in zip(names, columns, strict=True)
                ]
                connection.execute(
                    f"INSERT INTO {table} ({','.join(names)}) "  # noqa: S608
                    f"VALUES ({','.join('?' for _ in names)})",
                    values,
                )
    connection.execute("UPDATE engine_generations SET control_epoch=?", (value["control_epoch"],))


__all__ = ["restore_source_authority", "source_authority_sidecars"]
