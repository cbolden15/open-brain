"""SQLite storage boundary for the local engine."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .search_projection import upsert_search_document

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    capture_id TEXT NOT NULL UNIQUE,
    accepted_receipt_id TEXT NOT NULL UNIQUE,
    payload_family TEXT NOT NULL,
    payload_json BLOB NOT NULL,
    search_text TEXT NOT NULL,
    file_bytes BLOB,
    source_origin TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    space_id TEXT,
    intent TEXT,
    capture_why TEXT,
    action TEXT NOT NULL,
    title TEXT,
    accepted_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0,
    source_path TEXT,
    canonical_path TEXT,
    auto_proposal_id TEXT UNIQUE,
    auto_proposal_receipt_id TEXT UNIQUE,
    auto_decision_id TEXT UNIQUE,
    auto_decision_receipt_id TEXT UNIQUE,
    page_id TEXT UNIQUE,
    publication_id TEXT UNIQUE,
    publication_path TEXT,
    enrichment_state TEXT NOT NULL DEFAULT 'pending_enrichment',
    actor_id TEXT,
    role_claim_json TEXT,
    privacy_json TEXT,
    provenance_json TEXT,
    submission_path TEXT
);
CREATE TABLE IF NOT EXISTS spaces (
    space_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS space_operations (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    operation TEXT NOT NULL,
    space_id TEXT NOT NULL,
    name TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS route_operations (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    route_id TEXT NOT NULL UNIQUE,
    supersedes_route_id TEXT,
    capture_id TEXT NOT NULL,
    space_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proposal_sets (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    set_delivery_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    proposed_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    proposed_bytes BLOB NOT NULL,
    supplied_reason TEXT,
    space_id TEXT,
    receipt_id TEXT NOT NULL UNIQUE,
    page_id TEXT,
    canonical_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    terminal_decision_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS decisions (
    delivery_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    decision_receipt_id TEXT NOT NULL UNIQUE,
    proposal_id TEXT NOT NULL UNIQUE,
    outcome TEXT NOT NULL,
    effective_bytes BLOB,
    recorded_at TEXT NOT NULL,
    page_id TEXT,
    publication_id TEXT UNIQUE,
    canonical_path TEXT,
    publication_path TEXT,
    stage INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS search_documents (
    result_id TEXT PRIMARY KEY NOT NULL,
    capture_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    payload_family TEXT NOT NULL,
    space_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    trust TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    canonical_path TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS search_capture_idx ON search_documents (capture_id);
"""

_LIVE_SEARCH_TABLES = frozenset({"search_fts_identity", "search_documents_fts"})
_LIVE_SEARCH_TRIGGERS = frozenset(
    {
        "search_documents_result_id_immutable",
        "search_documents_fts_insert",
        "search_documents_fts_update",
        "search_documents_fts_delete",
    }
)
_LIVE_SEARCH_SCHEMA = (
    """
    CREATE TABLE search_fts_identity (
        fts_rowid INTEGER PRIMARY KEY,
        result_id TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE VIRTUAL TABLE search_documents_fts USING fts5(
        result_id UNINDEXED,
        title,
        body,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TRIGGER search_documents_result_id_immutable
    BEFORE UPDATE OF result_id ON search_documents
    WHEN new.result_id IS NOT old.result_id
    BEGIN
        SELECT RAISE(ABORT, 'search result identity is immutable');
    END
    """,
    """
    CREATE TRIGGER search_documents_fts_insert
    AFTER INSERT ON search_documents
    BEGIN
        INSERT INTO search_fts_identity(result_id) VALUES (new.result_id);
        INSERT INTO search_documents_fts(rowid, result_id, title, body)
        SELECT fts_rowid, new.result_id, new.title, new.body
        FROM search_fts_identity
        WHERE result_id = new.result_id;
    END
    """,
    """
    CREATE TRIGGER search_documents_fts_update
    AFTER UPDATE OF title, body ON search_documents
    BEGIN
        DELETE FROM search_documents_fts
        WHERE rowid = (
            SELECT fts_rowid FROM search_fts_identity WHERE result_id = old.result_id
        );
        INSERT INTO search_documents_fts(rowid, result_id, title, body)
        SELECT fts_rowid, new.result_id, new.title, new.body
        FROM search_fts_identity
        WHERE result_id = new.result_id;
    END
    """,
    """
    CREATE TRIGGER search_documents_fts_delete
    AFTER DELETE ON search_documents
    BEGIN
        DELETE FROM search_documents_fts
        WHERE rowid = (
            SELECT fts_rowid FROM search_fts_identity WHERE result_id = old.result_id
        );
        DELETE FROM search_fts_identity WHERE result_id = old.result_id;
    END
    """,
)


class _LocalStore:
    def __init__(self, profile: LocalEngineContext) -> None:
        self.profile = profile
        self.root = profile.root
        connection = self.connect()
        try:
            connection.executescript(_SCHEMA)
            _add_capture_submission_columns(connection)
            _add_route_operation_columns(connection)
            _adopt_live_search(connection)
        finally:
            connection.close()

    def connect(self) -> sqlite3.Connection:
        return connect_database(
            root=self.root,
            database_name=".open-brain/state/phase1.sqlite3",
            expected_root_identity=self.profile.root_identity,
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def _add_capture_submission_columns(connection: sqlite3.Connection) -> None:
    existing = {str(row["name"]) for row in connection.execute("PRAGMA table_info(captures)")}
    for name, declaration in (
        ("actor_id", "TEXT"),
        ("role_claim_json", "TEXT"),
        ("privacy_json", "TEXT"),
        ("provenance_json", "TEXT"),
        ("submission_path", "TEXT"),
    ):
        if name not in existing:
            connection.execute(f"ALTER TABLE captures ADD COLUMN {name} {declaration}")


def _add_route_operation_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(route_operations)")
    }
    if "route_id" not in existing:
        connection.execute("ALTER TABLE route_operations ADD COLUMN route_id TEXT")
    if "supersedes_route_id" not in existing:
        connection.execute("ALTER TABLE route_operations ADD COLUMN supersedes_route_id TEXT")
    latest_by_capture: dict[str, str] = {}
    rows = tuple(
        connection.execute(
            "SELECT rowid, * FROM route_operations ORDER BY capture_id, recorded_at, rowid"
        )
    )
    for row in rows:
        capture_id = str(row["capture_id"])
        route_id = row["route_id"]
        if route_id is None:
            receipt_id = str(row["receipt_id"])
            route_id = "route_" + receipt_id.removeprefix("receipt_")
            connection.execute(
                """
                UPDATE route_operations
                SET route_id = ?, supersedes_route_id = ?, stage = 0
                WHERE rowid = ?
                """,
                (route_id, latest_by_capture.get(capture_id), row["rowid"]),
            )
        latest_by_capture[capture_id] = str(route_id)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS route_identity_idx ON route_operations (route_id)"
    )


def live_search_schema_is_available(connection: sqlite3.Connection) -> bool:
    """Return whether the complete W3 live-search schema is present."""
    objects = {
        (str(row["type"]), str(row["name"])): str(row["sql"] or "")
        for row in connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE name IN (?, ?, ?, ?, ?, ?)
            """,
            (
                *_LIVE_SEARCH_TABLES,
                *_LIVE_SEARCH_TRIGGERS,
            ),
        )
    }
    if not all(("table", name) in objects for name in _LIVE_SEARCH_TABLES):
        return False
    if not all(("trigger", name) in objects for name in _LIVE_SEARCH_TRIGGERS):
        return False
    fts_sql = " ".join(objects[("table", "search_documents_fts")].split()).casefold()
    return "using fts5" in fts_sql and "unicode61 remove_diacritics 2" in fts_sql


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


def _adopt_live_search(connection: sqlite3.Connection) -> None:
    present = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'search_documents_fts%' "
            "OR name = 'search_fts_identity' OR name LIKE 'search_documents_result_id_%'"
        )
    }
    required = _LIVE_SEARCH_TABLES | _LIVE_SEARCH_TRIGGERS
    if present & required:
        if live_search_schema_is_available(connection):
            return
        raise ValueError("invalid live search schema")

    try:
        connection.execute("BEGIN IMMEDIATE")
        legacy_rows = tuple(
            connection.execute(
                """
                SELECT d.*, c.source_reference
                FROM search_documents AS d
                LEFT JOIN captures AS c ON c.capture_id = d.capture_id
                ORDER BY d.result_id
                """
            )
        )
        for row in legacy_rows:
            source_reference = row["source_reference"]
            if not isinstance(source_reference, str) or not source_reference:
                raise ValueError("search projection capture is unavailable")
            upsert_search_document(
                connection,
                result_id=str(row["result_id"]),
                capture_id=str(row["capture_id"]),
                record_type=str(row["record_type"]),
                payload_family=str(row["payload_family"]),
                space_id=None if row["space_id"] is None else str(row["space_id"]),
                title=str(row["title"]),
                body=str(row["body"]),
                canonical_path=(
                    None if row["canonical_path"] is None else str(row["canonical_path"])
                ),
                updated_at=str(row["updated_at"]),
            )
        for statement in _LIVE_SEARCH_SCHEMA[:2]:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO search_fts_identity(result_id)
            SELECT result_id FROM search_documents ORDER BY result_id
            """
        )
        rebuild_live_search_fts(connection)
        for statement in _LIVE_SEARCH_SCHEMA[2:]:
            connection.execute(statement)
        connection.execute("COMMIT")
    except BaseException:
        with suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
