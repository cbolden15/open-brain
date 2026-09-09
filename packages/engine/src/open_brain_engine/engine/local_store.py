"""SQLite storage boundary for the local engine."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime

from open_brain_engine.storage.sqlite import SchemaError

from .contracts import LocalEngineContext
from .local_schema import classify_local_schema, open_local_database, open_local_database_read_only
from .local_schema import live_search_schema_is_available as live_search_schema_is_available
from .normalization import _utc_now


class _LocalStore:
    def __init__(
        self, profile: LocalEngineContext, *, clock: Callable[[], datetime] = _utc_now
    ) -> None:
        self.profile = profile
        self.root = profile.root
        self._clock = clock
        open_local_database(profile, clock=clock).close()

    def connect(self) -> sqlite3.Connection:
        return open_local_database_read_only(self.profile)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = open_local_database(self.profile, clock=self._clock)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if classify_local_schema(connection).state != "current":
                raise SchemaError("local state schema changed before write")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def rebuild_live_search_fts(connection: sqlite3.Connection) -> None:
    """Rebuild only the derived live FTS rows inside the caller's transaction."""
    connection.execute("DELETE FROM search_documents_fts")
    connection.execute(
        """
        INSERT INTO search_documents_fts(rowid, result_id, title, body)
        SELECT i.fts_rowid, d.result_id, d.title, d.body
        FROM search_documents AS d
        JOIN search_fts_identity AS i USING (result_id)
        ORDER BY i.fts_rowid
        """
    )
