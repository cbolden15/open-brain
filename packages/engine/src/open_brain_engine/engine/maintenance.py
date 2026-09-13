"""Read-only diagnostics for the foreground local runtime."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from open_brain_engine.storage.sqlite import SchemaError, connect_database_read_only

from .contracts import LocalEngineContext
from .local_schema import (
    PHASE1_STATE_DATABASE as PHASE1_STATE_DATABASE,
)
from .local_schema import (
    PHASE1_STATE_SCHEMA_VERSION as PHASE1_STATE_SCHEMA_VERSION,
)
from .local_schema import (
    SchemaState as SchemaState,
)
from .local_schema import (
    classify_local_schema,
    open_local_database_read_only,
)
from .local_schema import (
    inspect_phase1_state as inspect_phase1_state,
)
from .local_store import live_search_schema_is_available

SEARCH_INDEX_DATABASE = ".open-brain/indexes/search.sqlite3"


@dataclass(frozen=True, slots=True)
class IndexState:
    state: str
    generation: int | None
    document_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "generation": self.generation,
            "document_count": self.document_count,
            "authoritative": False,
            "freshness": "potentially_stale",
        }


@dataclass(frozen=True, slots=True)
class LiveSearchState:
    state: str
    projection_count: int
    identity_count: int
    fts_count: int
    result_ids_agree: bool
    contents_agree: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "projection_count": self.projection_count,
            "identity_count": self.identity_count,
            "fts_count": self.fts_count,
            "result_ids_agree": self.result_ids_agree,
            "contents_agree": self.contents_agree,
            "authoritative": True,
        }


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    schema: SchemaState
    live_search: LiveSearchState
    index: IndexState

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema.to_dict(),
            "live_search": self.live_search.to_dict(),
            "index": self.index.to_dict(),
        }


def read_maintenance_snapshot(profile: LocalEngineContext) -> MaintenanceSnapshot:
    """Read bounded schema and search-index evidence."""
    if not isinstance(profile, LocalEngineContext):
        raise ValueError("invalid local profile")
    return MaintenanceSnapshot(
        schema=inspect_phase1_state(profile),
        live_search=inspect_live_search(profile),
        index=_inspect_index(profile),
    )


def inspect_live_search(profile: LocalEngineContext) -> LiveSearchState:
    """Inspect the authoritative projection, identity map, and FTS rows in one snapshot."""
    if not isinstance(profile, LocalEngineContext):
        raise ValueError("invalid local profile")
    try:
        connection = open_local_database_read_only(profile, inspect_only=True)
    except SchemaError:
        database = profile.root / PHASE1_STATE_DATABASE
        return LiveSearchState(
            state="absent" if not database.exists() else "invalid",
            projection_count=0,
            identity_count=0,
            fts_count=0,
            result_ids_agree=False,
            contents_agree=False,
        )
    try:
        connection.execute("BEGIN")
        tables = {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name IN ('search_documents', 'search_fts_identity', 'search_documents_fts')
                """
            )
        }
        if tables != {"search_documents", "search_fts_identity", "search_documents_fts"}:
            return LiveSearchState(
                state="invalid",
                projection_count=0,
                identity_count=0,
                fts_count=0,
                result_ids_agree=False,
                contents_agree=False,
            )
        schema_available = classify_local_schema(
            connection
        ).state == "current" and live_search_schema_is_available(connection)
        projection_count = int(
            connection.execute("SELECT count(*) FROM search_documents").fetchone()[0]
        )
        identity_count = int(
            connection.execute("SELECT count(*) FROM search_fts_identity").fetchone()[0]
        )
        fts_count = int(
            connection.execute("SELECT count(*) FROM search_documents_fts").fetchone()[0]
        )
        set_mismatch = int(
            connection.execute(
                """
                SELECT EXISTS(
                    SELECT result_id FROM search_documents
                    EXCEPT SELECT result_id FROM search_fts_identity
                ) OR EXISTS(
                    SELECT result_id FROM search_fts_identity
                    EXCEPT SELECT result_id FROM search_documents
                ) OR EXISTS(
                    SELECT result_id FROM search_documents_fts
                    EXCEPT SELECT result_id FROM search_documents
                ) OR EXISTS(
                    SELECT result_id FROM search_documents
                    EXCEPT SELECT result_id FROM search_documents_fts
                )
                """
            ).fetchone()[0]
        )
        content_mismatch = int(
            connection.execute(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM search_documents AS d
                    LEFT JOIN search_fts_identity AS i USING (result_id)
                    LEFT JOIN search_documents_fts AS f ON f.rowid = i.fts_rowid
                    WHERE i.fts_rowid IS NULL
                       OR f.rowid IS NULL
                       OR f.result_id IS NOT d.result_id
                       OR f.title IS NOT d.title
                       OR f.body IS NOT d.body
                    LIMIT 1
                ) OR EXISTS(
                    SELECT 1
                    FROM search_documents_fts AS f
                    LEFT JOIN search_fts_identity AS i ON i.fts_rowid = f.rowid
                    WHERE i.fts_rowid IS NULL OR i.result_id IS NOT f.result_id
                    LIMIT 1
                )
                """
            ).fetchone()[0]
        )
        connection.execute("COMMIT")
    except TypeError, ValueError, sqlite3.Error:
        with suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        return LiveSearchState(
            state="invalid",
            projection_count=0,
            identity_count=0,
            fts_count=0,
            result_ids_agree=False,
            contents_agree=False,
        )
    finally:
        with suppress(Exception):
            connection.close()
    result_ids_agree = not set_mismatch and projection_count == identity_count == fts_count
    contents_agree = not content_mismatch
    return LiveSearchState(
        state=(
            "current" if schema_available and result_ids_agree and contents_agree else "invalid"
        ),
        projection_count=projection_count,
        identity_count=identity_count,
        fts_count=fts_count,
        result_ids_agree=result_ids_agree,
        contents_agree=contents_agree,
    )


def live_search_is_healthy(profile: LocalEngineContext) -> bool:
    state = inspect_live_search(profile)
    return state.state == "current" and _fts5_canary()


def _fts5_canary() -> bool:
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute(
                """
                CREATE VIRTUAL TABLE search_canary USING fts5(
                    title,
                    body,
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
            connection.execute(
                "INSERT INTO search_canary(title, body) VALUES (?, ?)",
                ("Café title", "synthetic canary phrase"),
            )
            row = connection.execute(
                """
                SELECT bm25(search_canary, 10.0, 1.0),
                       snippet(search_canary, -1, '[', ']', '…', 24)
                FROM search_canary
                WHERE search_canary MATCH ?
                """,
                ('"cafe"',),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None and isinstance(row[0], float) and "[Café]" in str(row[1])


def _inspect_index(profile: LocalEngineContext) -> IndexState:
    try:
        connection = connect_database_read_only(
            root=profile.root,
            database_name=SEARCH_INDEX_DATABASE,
            expected_root_identity=profile.root_identity,
        )
    except SchemaError:
        database = profile.root / SEARCH_INDEX_DATABASE
        try:
            database.lstat()
        except FileNotFoundError:
            return IndexState(state="absent", generation=None, document_count=0)
        except OSError:
            pass
        return IndexState(state="invalid", generation=None, document_count=0)
    try:
        row = connection.execute(
            "SELECT generation FROM index_metadata WHERE singleton = 1"
        ).fetchone()
        document_count = int(
            connection.execute("SELECT count(*) FROM search_documents").fetchone()[0]
        )
    except TypeError, ValueError, sqlite3.Error:
        return IndexState(state="invalid", generation=None, document_count=0)
    finally:
        with suppress(Exception):
            connection.close()
    generation = None if row is None else cast(int, row["generation"])
    if generation is None:
        return IndexState(state="invalid", generation=None, document_count=document_count)
    return IndexState(state="current", generation=generation, document_count=document_count)
