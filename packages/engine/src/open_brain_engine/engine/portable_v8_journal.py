"""Snapshot/hidden-stage installation of exact journal rows, not live SQL replay."""

import base64
import sqlite3
from typing import Any

from open_brain_engine.portable.v8_custody import JOURNAL_TABLES, SEQUENCE_TABLES

from .contracts import JournalEnvelope, LocalEngineContext


def journal_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    if (
        not connection.in_transaction
        or connection.execute("PRAGMA user_version").fetchone()[0] != 13
    ):
        raise ValueError("capture custody requires one schema13 read snapshot")
    state: dict[str, Any] = {"schema_version": 1}
    for table, columns in JOURNAL_TABLES.items():
        state[table] = [
            dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {columns[0]}")
        ]
    for row in state["capture_ingestion_payloads"]:
        row["envelope_bytes"] = base64.b64encode(row["envelope_bytes"]).decode("ascii")
    state["sequences"] = {
        row["name"]: row["seq"]
        for row in connection.execute("SELECT name,seq FROM sqlite_sequence")
        if row["name"] in SEQUENCE_TABLES
    }
    return state


def install_journal_snapshot(
    connection: sqlite3.Connection,
    state: dict[str, Any],
    profile: LocalEngineContext,
) -> None:
    for table in JOURNAL_TABLES:
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise ValueError("capture custody restore requires empty hidden stage")
    for row in state["capture_ingestion_payloads"]:
        JournalEnvelope.from_bytes(row["envelope_bytes"]).submission.validate_profile(profile)
    for table, columns in JOURNAL_TABLES.items():
        placeholders = ",".join("?" for _ in columns)
        connection.executemany(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
            [tuple(row[key] for key in columns) for row in state[table]],
        )
    for table, sequence in state["sequences"].items():
        connection.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (table, sequence))
