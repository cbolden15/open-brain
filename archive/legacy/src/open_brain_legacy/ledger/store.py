"""Private durable metadata for the receipt-bound ledger apply path."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import PutDisposition, PutResult, RedactedMarkdownDocument
from open_brain_engine.storage.filesystem import StorageError
from open_brain_engine.storage.frontmatter import AtomicMarkdownReader, rendered_markdown_bytes
from open_brain_engine.storage.sqlite import connect_database, connect_database_read_only

if TYPE_CHECKING:
    from .service import PreparedLedgerApply


_LEDGER_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS ledger_rows (
        stage_digest_sha256 TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        event_digest_sha256 TEXT NOT NULL,
        target_logical_key TEXT NOT NULL,
        citation_id TEXT NOT NULL,
        document_ids_json TEXT NOT NULL,
        document_digest_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('prepared', 'applied', 'slimmed')),
        sink_digests_json TEXT,
        archive_digest_sha256 TEXT,
        successor_id TEXT,
        successor_digest_sha256 TEXT
    )
    """,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublishedDocumentSet:
    """The sole reader boundary for a complete, durably visible document set."""

    row_identity: LedgerRowIdentity
    document_ids: tuple[str, ...]
    sink_digests: tuple[str, ...]
    citation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LedgerRowIdentity:
    stage_digest_sha256: str
    row_digest_sha256: str


@dataclass(frozen=True, slots=True)
class DurableSlimState:
    row_identity: LedgerRowIdentity
    source_id: str
    citation_ids: tuple[str, ...]
    slimmed: bool
    archive_digest_sha256: str | None
    successor_id: str | None
    successor_digest_sha256: str | None


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    inflight: bool
    row_state: str | None


@dataclass(frozen=True, slots=True)
class PublishedReferenceInspection:
    reference_count: int
    stale_count: int

    def __post_init__(self) -> None:
        if (
            type(self.reference_count) is not int
            or self.reference_count < 0
            or type(self.stale_count) is not int
            or not 0 <= self.stale_count <= self.reference_count
        ):
            raise LedgerStoreError("invalid published reference inspection")


_JOURNAL_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS inflight_journal (
        stage_digest_sha256 TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        event_digest_sha256 TEXT NOT NULL,
        target_logical_key TEXT NOT NULL,
        citation_id TEXT NOT NULL,
        document_ids_json TEXT NOT NULL,
        document_digest_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state = 'journaled')
    )
    """,
)


class LedgerStoreError(StorageError):
    """Ledger metadata was invalid or conflicted with an immutable prior apply."""


class SqliteLedgerStore:
    """Two root-confined SQLite stores: immutable rows and metadata-only inflight state."""

    def __init__(self, *, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            raise LedgerStoreError("private ledger root unavailable") from None
        self._connection = connect_database(root=root, database_name="ledger.sqlite3")
        self._journal_connection = connect_database(
            root=root, database_name="ledger-inflight.sqlite3"
        )
        self._initialize(self._connection, _LEDGER_SCHEMA)
        self._initialize(self._journal_connection, _JOURNAL_SCHEMA)

    def close(self) -> None:
        self._connection.close()
        self._journal_connection.close()

    def __enter__(self) -> SqliteLedgerStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction or self._journal_connection.in_transaction

    def journal(self, prepared: PreparedLedgerApply) -> None:
        values = self._values(prepared)
        connection = self._journal_connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inflight_journal WHERE stage_digest_sha256 = ?",
                (values["stage_digest_sha256"],),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO inflight_journal(
                        stage_digest_sha256, source_id, event_digest_sha256, target_logical_key,
                        citation_id, document_ids_json, document_digest_sha256, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'journaled')
                    """,
                    self._metadata_tuple(values),
                )
            elif not self._row_matches(row, values):
                raise LedgerStoreError("ledger inflight conflict")
            connection.commit()
        except LedgerStoreError:
            self._rollback(connection)
            raise
        except sqlite3.Error:
            self._rollback(connection)
            raise LedgerStoreError("ledger inflight persistence failed") from None

    def prepare(self, prepared: PreparedLedgerApply) -> bool:
        """Insert or verify the immutable row. Return whether it was already applied."""
        values = self._values(prepared)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ledger_rows WHERE stage_digest_sha256 = ?",
                (values["stage_digest_sha256"],),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO ledger_rows(
                        stage_digest_sha256, source_id, event_digest_sha256, target_logical_key,
                        citation_id, document_ids_json, document_digest_sha256, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared')
                    """,
                    self._row_tuple(values),
                )
                applied = False
            else:
                if not self._row_matches(row, values):
                    raise LedgerStoreError("ledger immutable row conflict")
                applied = row["state"] == "applied"
            connection.commit()
            return applied
        except LedgerStoreError:
            self._rollback(connection)
            raise
        except sqlite3.Error:
            self._rollback(connection)
            raise LedgerStoreError("ledger prepare failed") from None

    def finalize(
        self,
        prepared: PreparedLedgerApply,
        *,
        reader: AtomicMarkdownReader,
        receipts: tuple[PutResult, ...],
    ) -> None:
        values = self._values(prepared)
        verified_digests = self._verify_persistence(
            prepared=prepared,
            reader=reader,
            receipts=receipts,
        )
        encoded_digests = self._json(verified_digests)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ledger_rows WHERE stage_digest_sha256 = ?",
                (values["stage_digest_sha256"],),
            ).fetchone()
            if row is None or not self._row_matches(row, values):
                raise LedgerStoreError("ledger finalize conflict")
            if (
                row["state"] in {"applied", "slimmed"}
                and row["sink_digests_json"] != encoded_digests
            ):
                raise LedgerStoreError("ledger sink digest conflict")
            if row["state"] == "prepared":
                connection.execute(
                    "UPDATE ledger_rows SET state = 'applied', sink_digests_json = ? "
                    "WHERE stage_digest_sha256 = ?",
                    (encoded_digests, values["stage_digest_sha256"]),
                )
            connection.commit()
            self._clear_journal(values["stage_digest_sha256"])
        except LedgerStoreError:
            self._rollback(connection)
            raise
        except sqlite3.Error:
            self._rollback(connection)
            raise LedgerStoreError("ledger finalize failed") from None

    def clear_journal(self, prepared: PreparedLedgerApply) -> None:
        self._clear_journal(self._values(prepared)["stage_digest_sha256"])

    def reconciliation_snapshot(
        self, prepared: PreparedLedgerApply
    ) -> ReconciliationSnapshot:
        """Verify exact immutable metadata before an explicit reconciliation decision."""
        values = self._values(prepared)
        journal_row = self._journal_connection.execute(
            "SELECT * FROM inflight_journal WHERE stage_digest_sha256 = ?",
            (values["stage_digest_sha256"],),
        ).fetchone()
        durable_row = self._row(values["stage_digest_sha256"])
        if journal_row is not None and not self._row_matches(journal_row, values):
            raise LedgerStoreError("ledger inflight conflict")
        if durable_row is not None and not self._row_matches(durable_row, values):
            raise LedgerStoreError("ledger immutable row conflict")
        return ReconciliationSnapshot(
            inflight=journal_row is not None,
            row_state=None if durable_row is None else str(durable_row["state"]),
        )

    def reset_reconciliation(self, prepared: PreparedLedgerApply) -> None:
        """Clear only an exact inflight intent; immutable prepared rows remain replayable."""
        snapshot = self.reconciliation_snapshot(prepared)
        if snapshot.row_state in {"applied", "slimmed"}:
            raise LedgerStoreError("published ledger row cannot be reset")
        if snapshot.inflight:
            self.clear_journal(prepared)

    def published_document_set(self, stage_digest_sha256: str) -> PublishedDocumentSet | None:
        """Expose documents only when one durable row verifies every intended sink digest."""
        row = self._row(stage_digest_sha256)
        if row is None or row["state"] not in {"applied", "slimmed"}:
            return None
        identity = self._identity(row)
        document_ids = self._strings(row["document_ids_json"])
        sink_digests = self._digests(
            self._strings(row["sink_digests_json"]), count=len(document_ids)
        )
        return PublishedDocumentSet(
            row_identity=identity,
            document_ids=document_ids,
            sink_digests=sink_digests,
            citation_ids=(str(row["citation_id"]),),
        )

    def applied_row_identity(self, stage_digest_sha256: str) -> LedgerRowIdentity | None:
        published = self.published_document_set(stage_digest_sha256)
        return None if published is None else published.row_identity

    def slim_state(self, identity: LedgerRowIdentity) -> DurableSlimState | None:
        if not isinstance(identity, LedgerRowIdentity):
            return None
        row = self._row(identity.stage_digest_sha256)
        if row is None or row["state"] not in {"applied", "slimmed"}:
            return None
        verified_identity = self._identity(row)
        if verified_identity != identity:
            return None
        self._digests(
            self._strings(row["sink_digests_json"]),
            count=len(self._strings(row["document_ids_json"])),
        )
        return DurableSlimState(
            row_identity=identity,
            source_id=str(row["source_id"]),
            citation_ids=(str(row["citation_id"]),),
            slimmed=row["state"] == "slimmed",
            archive_digest_sha256=row["archive_digest_sha256"],
            successor_id=row["successor_id"],
            successor_digest_sha256=row["successor_digest_sha256"],
        )

    def finalize_slim(
        self,
        identity: LedgerRowIdentity,
        *,
        archive_digest_sha256: str,
        successor_id: str,
        successor_digest_sha256: str,
    ) -> None:
        archive_digest = self._digest(archive_digest_sha256)
        successor_digest = self._digest(successor_digest_sha256)
        if not successor_id:
            raise LedgerStoreError("invalid slim successor")
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ledger_rows WHERE stage_digest_sha256 = ?",
                (identity.stage_digest_sha256,),
            ).fetchone()
            if row is None or row["state"] not in {"applied", "slimmed"}:
                raise LedgerStoreError("slim row unavailable")
            if self._identity(row) != identity:
                raise LedgerStoreError("slim row identity conflict")
            final_values = (archive_digest, successor_id, successor_digest)
            if row["state"] == "slimmed":
                if (
                    row["archive_digest_sha256"],
                    row["successor_id"],
                    row["successor_digest_sha256"],
                ) != final_values:
                    raise LedgerStoreError("slim finalization conflict")
            else:
                connection.execute(
                    """
                    UPDATE ledger_rows
                    SET state = 'slimmed', archive_digest_sha256 = ?, successor_id = ?,
                        successor_digest_sha256 = ?
                    WHERE stage_digest_sha256 = ?
                    """,
                    (*final_values, identity.stage_digest_sha256),
                )
            connection.commit()
        except LedgerStoreError:
            self._rollback(connection)
            raise
        except sqlite3.Error:
            self._rollback(connection)
            raise LedgerStoreError("slim finalization failed") from None

    def record_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM ledger_rows").fetchone()[0])

    def inflight_count(self) -> int:
        return int(
            self._journal_connection.execute("SELECT COUNT(*) FROM inflight_journal").fetchone()[0]
        )

    def inflight_metadata(self) -> tuple[dict[str, object], ...]:
        rows = self._journal_connection.execute(
            """
            SELECT citation_id, document_digest_sha256, document_ids_json, event_digest_sha256,
                   stage_digest_sha256, state, target_logical_key
            FROM inflight_journal ORDER BY stage_digest_sha256
            """
        ).fetchall()
        return tuple(
            {
                "citation_id": str(row["citation_id"]),
                "document_digest_sha256": str(row["document_digest_sha256"]),
                "document_ids": tuple(json.loads(str(row["document_ids_json"]))),
                "event_digest_sha256": str(row["event_digest_sha256"]),
                "stage_digest_sha256": str(row["stage_digest_sha256"]),
                "state": str(row["state"]),
                "target_logical_key": str(row["target_logical_key"]),
            }
            for row in rows
        )

    @staticmethod
    def _initialize(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        except sqlite3.Error:
            SqliteLedgerStore._rollback(connection)
            raise LedgerStoreError("ledger schema unavailable") from None

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.rollback()

    @staticmethod
    def _json(value: tuple[str, ...]) -> str:
        return canonical_json_bytes(list(value)).decode("utf-8")

    @classmethod
    def _values(cls, prepared: PreparedLedgerApply) -> dict[str, str]:
        prepared.validate()
        return {
            "citation_id": prepared.citation_id,
            "document_digest_sha256": prepared.document_digest_sha256,
            "document_ids_json": cls._json(prepared.document_ids),
            "event_digest_sha256": prepared.event_digest_sha256,
            "source_id": prepared.source_id,
            "stage_digest_sha256": prepared.stage_digest_sha256,
            "target_logical_key": prepared.ledger_document.logical_key,
        }

    @staticmethod
    def _metadata_tuple(values: Mapping[str, str]) -> tuple[str, ...]:
        return (
            values["stage_digest_sha256"],
            values["source_id"],
            values["event_digest_sha256"],
            values["target_logical_key"],
            values["citation_id"],
            values["document_ids_json"],
            values["document_digest_sha256"],
        )

    @staticmethod
    def _row_tuple(values: Mapping[str, str]) -> tuple[str, ...]:
        return (
            values["stage_digest_sha256"],
            values["source_id"],
            values["event_digest_sha256"],
            values["target_logical_key"],
            values["citation_id"],
            values["document_ids_json"],
            values["document_digest_sha256"],
        )

    @staticmethod
    def _row_matches(row: sqlite3.Row, values: Mapping[str, str]) -> bool:
        return all(row[key] == value for key, value in values.items())

    def _row(self, stage_digest_sha256: str) -> sqlite3.Row | None:
        if not isinstance(stage_digest_sha256, str) or not _DIGEST.fullmatch(stage_digest_sha256):
            return None
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                "SELECT * FROM ledger_rows WHERE stage_digest_sha256 = ?",
                (stage_digest_sha256,),
            ).fetchone(),
        )

    @classmethod
    def _identity(cls, row: sqlite3.Row) -> LedgerRowIdentity:
        sink_digests = cls._strings(row["sink_digests_json"])
        value = {
            "citation_id": str(row["citation_id"]),
            "document_digest_sha256": str(row["document_digest_sha256"]),
            "document_ids": cls._strings(row["document_ids_json"]),
            "event_digest_sha256": str(row["event_digest_sha256"]),
            "sink_digests": sink_digests,
            "source_id": str(row["source_id"]),
            "stage_digest_sha256": str(row["stage_digest_sha256"]),
            "target_logical_key": str(row["target_logical_key"]),
        }
        return LedgerRowIdentity(
            stage_digest_sha256=str(row["stage_digest_sha256"]),
            row_digest_sha256=sha256(canonical_json_bytes(value)).hexdigest(),
        )

    @staticmethod
    def _strings(value: object) -> tuple[str, ...]:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise LedgerStoreError("invalid durable ledger metadata") from None
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise LedgerStoreError("invalid durable ledger metadata")
        return tuple(decoded)

    @staticmethod
    def _digest(value: str) -> str:
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise LedgerStoreError("invalid ledger digest")
        return value

    @classmethod
    def _digests(cls, values: tuple[str, ...], *, count: int) -> tuple[str, ...]:
        if len(values) != count:
            raise LedgerStoreError("incomplete ledger document set")
        return tuple(cls._digest(value) for value in values)

    @staticmethod
    def _verify_persistence(
        *,
        prepared: PreparedLedgerApply,
        reader: AtomicMarkdownReader,
        receipts: tuple[PutResult, ...],
    ) -> tuple[str, ...]:
        documents: tuple[RedactedMarkdownDocument, ...] = (
            prepared.capture_document,
            prepared.ledger_document,
        )
        if (
            type(reader) is not AtomicMarkdownReader
            or not isinstance(receipts, tuple)
            or len(receipts) != len(documents)
        ):
            raise LedgerStoreError("incomplete ledger persistence proof")
        digests: list[str] = []
        for document, receipt in zip(documents, receipts, strict=True):
            expected_bytes = rendered_markdown_bytes(document)
            expected_digest = sha256(expected_bytes).hexdigest()
            if (
                type(receipt) is not PutResult
                or type(receipt.disposition) is not PutDisposition
                or receipt.disposition not in {PutDisposition.CREATED, PutDisposition.DUPLICATE}
                or receipt.record_id != document.document_id
                or receipt.digest_sha256 != expected_digest
            ):
                raise LedgerStoreError("invalid ledger sink receipt")
            try:
                persisted_bytes = reader.read_back(document.document_id)
            except Exception:
                raise LedgerStoreError("ledger sink read-back failed") from None
            if type(persisted_bytes) is not bytes or persisted_bytes != expected_bytes:
                raise LedgerStoreError("ledger sink read-back mismatch")
            digests.append(expected_digest)
        return tuple(digests)

    def _clear_journal(self, stage_digest_sha256: str) -> None:
        connection = self._journal_connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM inflight_journal WHERE stage_digest_sha256 = ?",
                (stage_digest_sha256,),
            )
            connection.commit()
        except sqlite3.Error:
            self._rollback(connection)
            raise LedgerStoreError("ledger journal clear failed") from None


def inspect_published_references(
    *,
    metadata_root: Path,
    database_name: str,
    content_root: Path,
) -> PublishedReferenceInspection:
    """Verify published document references through read-only metadata and content readers."""
    connection = connect_database_read_only(
        root=metadata_root,
        database_name=database_name,
    )
    reader = AtomicMarkdownReader(root=content_root)
    try:
        rows = connection.execute(
            "SELECT document_ids_json, sink_digests_json "
            "FROM ledger_rows WHERE state IN ('applied', 'slimmed') "
            "ORDER BY stage_digest_sha256"
        ).fetchall()
        reference_count = 0
        stale_count = 0
        for row in rows:
            document_ids = SqliteLedgerStore._strings(row["document_ids_json"])
            sink_digests = SqliteLedgerStore._digests(
                SqliteLedgerStore._strings(row["sink_digests_json"]),
                count=len(document_ids),
            )
            reference_count += len(document_ids)
            for document_id, expected_digest in zip(
                document_ids,
                sink_digests,
                strict=True,
            ):
                payload = reader.read_back(document_id)
                if payload is None or sha256(payload).hexdigest() != expected_digest:
                    stale_count += 1
        return PublishedReferenceInspection(reference_count, stale_count)
    except (LedgerStoreError, sqlite3.Error):
        raise LedgerStoreError("published reference inspection failed") from None
    finally:
        connection.close()
