"""Lifecycle predicate shared by current projections and retained owner history."""

from __future__ import annotations

import sqlite3

from .capture_lifecycle_contracts import LifecycleError
from .capture_lifecycle_journal import LifecycleJournal


def lifecycle_permits_capture(
    connection: sqlite3.Connection, capture_id: str, *, history: bool = False
) -> bool:
    """Fail closed on damaged lifecycle evidence; never grant source authority."""
    owned_snapshot = not connection.in_transaction
    try:
        if owned_snapshot:
            connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < 18:
            return True
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        if identity is None:
            return False
        journal = LifecycleJournal.for_engine(
            connection, brain_id=identity[0], issuer_epoch=identity[1]
        )
        return journal.permits_capture(capture_id, history=history)
    except (LifecycleError, sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        if owned_snapshot and connection.in_transaction:
            connection.execute("ROLLBACK")
