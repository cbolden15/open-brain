"""Lifecycle predicate shared by current projections and retained owner history."""

from __future__ import annotations

import sqlite3

from .capture_lifecycle_contracts import LifecycleError


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
        from .capture_lifecycle import open_lifecycle_journal

        journal = open_lifecycle_journal(connection)
        if not journal.permits_capture(capture_id, history=history):
            return False
        if version >= 20:
            from .consumer_sources import consumer_group_sources_visible

            return consumer_group_sources_visible(
                connection, capture_id, history=history, journal=journal
            )
        return True
    except (LifecycleError, sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        if owned_snapshot and connection.in_transaction:
            connection.execute("ROLLBACK")
