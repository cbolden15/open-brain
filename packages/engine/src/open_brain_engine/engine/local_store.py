"""SQLite storage boundary for the local engine."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import TYPE_CHECKING

from open_brain_engine.storage.sqlite import SchemaError, begin_immediate, restore_busy_timeout

from .contracts import LocalEngineContext
from .local_schema import classify_local_schema, open_local_database, open_local_database_read_only
from .local_schema import live_search_schema_is_available as live_search_schema_is_available
from .normalization import _utc_now

if TYPE_CHECKING:
    from .portable_v5_restore import ValidatedV5IssuerSeed


class _LocalStore:
    def __init__(
        self,
        profile: LocalEngineContext,
        *,
        clock: Callable[[], datetime] = _utc_now,
        schema_version: int | None = None,
        issuer_seed: ValidatedV5IssuerSeed | None = None,
        initialize: bool = True,
    ) -> None:
        self.profile = profile
        self.root = profile.root
        self._clock = clock
        self._schema_version = schema_version
        if initialize:
            open_local_database(
                profile, clock=clock, schema_version=schema_version, issuer_seed=issuer_seed
            ).close()
        else:
            # A schema-10 engine may open while another canonical writer owns
            # the fence so it can still accept a short journal transaction.
            # The caller has already classified the existing schema; this
            # read-only open revalidates it without running setup writes.
            open_local_database_read_only(profile).close()

    def connect(self) -> sqlite3.Connection:
        connection = open_local_database_read_only(
            self.profile, allow_old=self._schema_version == 6
        )
        if (
            self._schema_version is not None
            and connection.execute("PRAGMA user_version").fetchone()[0] != self._schema_version
        ):
            connection.close()
            raise SchemaError("local state compatibility target mismatch")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = open_local_database(
            self.profile, clock=self._clock, schema_version=self._schema_version
        )
        try:
            begin_immediate(connection)
            state = classify_local_schema(connection)
            if state.state != "current" and not (
                self._schema_version == 6 and state.state == "supported_old" and state.version == 6
            ):
                raise SchemaError("local state schema changed before write")
            yield connection
            from .source_store import (
                publish_source_metadata,
                register_completed_captures,
                register_publication_members,
            )

            source_history = connection.execute("PRAGMA user_version").fetchone()[0] >= 7
            if source_history:
                register_completed_captures(connection, self.profile)
                register_publication_members(connection, self.profile)
            connection.execute("COMMIT")
            if source_history:
                publish_source_metadata(connection, self.profile)
            restore_busy_timeout(connection)
        except BaseException:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            with suppress(sqlite3.Error):
                restore_busy_timeout(connection)
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
