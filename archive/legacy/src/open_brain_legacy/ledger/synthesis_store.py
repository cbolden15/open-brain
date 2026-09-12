"""Private atomic persistence for validated evaluating syntheses."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from open_brain_engine.core.models import PrivacyDecision
from open_brain_engine.core.ports import RedactedMarkdownDocument, RedactionReceipt
from open_brain_engine.storage.filesystem import StorageError
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .synthesis import PreparedSynthesis


_SYNTHESIS_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS synthesis_records (
        request_id TEXT PRIMARY KEY,
        topic_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state = 'evaluating'),
        request_json BLOB NOT NULL,
        result_json BLOB NOT NULL,
        result_digest_sha256 TEXT NOT NULL,
        document_id TEXT NOT NULL UNIQUE,
        document_json BLOB NOT NULL,
        document_digest_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS synthesis_link_backs (
        request_id TEXT NOT NULL REFERENCES synthesis_records(request_id) ON DELETE CASCADE,
        source_id TEXT NOT NULL,
        PRIMARY KEY (request_id, source_id)
    )
    """,
)


class SynthesisStoreError(StorageError):
    """Validated synthesis state could not be persisted atomically."""


@dataclass(frozen=True, slots=True)
class DurableSynthesisRecord:
    request_id: str
    topic_id: str
    state: str
    request_digest_sha256: str
    result_digest_sha256: str
    document_digest_sha256: str
    document: RedactedMarkdownDocument
    link_back_source_ids: tuple[str, ...]

    @classmethod
    def create(
        cls,
        prepared: PreparedSynthesis,
        *,
        privacy: PrivacyDecision,
    ) -> DurableSynthesisRecord:
        prepared.validate()
        if not isinstance(privacy, PrivacyDecision):
            raise SynthesisStoreError("invalid synthesis privacy")
        document = _document(prepared=prepared, privacy=privacy)
        return cls(
            request_id=prepared.request.request_id,
            topic_id=prepared.request.topic_id,
            state=prepared.state,
            request_digest_sha256=sha256(prepared.request.canonical_bytes()).hexdigest(),
            result_digest_sha256=sha256(prepared.result.canonical_bytes()).hexdigest(),
            document_digest_sha256=sha256(document.canonical_bytes()).hexdigest(),
            document=document,
            link_back_source_ids=prepared.link_back_source_ids,
        )

    def validate_for(
        self,
        prepared: PreparedSynthesis,
        *,
        privacy: PrivacyDecision,
    ) -> None:
        if self != DurableSynthesisRecord.create(prepared, privacy=privacy):
            raise SynthesisStoreError("synthesis durable proof mismatch")


class SqliteSynthesisStore:
    """One root-confined SQLite transaction for row, page, and trusted links."""

    def __init__(self, *, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            raise SynthesisStoreError("private synthesis root unavailable") from None
        self._connection = connect_database(
            root=root,
            database_name="ledger-synthesis.sqlite3",
        )
        self._initialize()

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def persist(
        self,
        prepared: PreparedSynthesis,
        *,
        privacy: PrivacyDecision,
    ) -> DurableSynthesisRecord:
        durable = DurableSynthesisRecord.create(prepared, privacy=privacy)
        request_bytes = prepared.request.canonical_bytes()
        result_bytes = prepared.result.canonical_bytes()
        document_bytes = durable.document.canonical_bytes()
        row_values: tuple[object, ...] = (
            durable.request_id,
            durable.topic_id,
            durable.state,
            request_bytes,
            result_bytes,
            durable.result_digest_sha256,
            durable.document.document_id,
            document_bytes,
            durable.document_digest_sha256,
        )
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM synthesis_records WHERE request_id = ?",
                (prepared.request.request_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO synthesis_records(
                        request_id, topic_id, state, request_json, result_json,
                        result_digest_sha256, document_id, document_json,
                        document_digest_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row_values,
                )
                connection.executemany(
                    "INSERT INTO synthesis_link_backs(request_id, source_id) VALUES (?, ?)",
                    (
                        (prepared.request.request_id, source_id)
                        for source_id in prepared.link_back_source_ids
                    ),
                )
            else:
                durable_values = tuple(
                    row[key]
                    for key in (
                        "request_id",
                        "topic_id",
                        "state",
                        "request_json",
                        "result_json",
                        "result_digest_sha256",
                        "document_id",
                        "document_json",
                        "document_digest_sha256",
                    )
                )
                links = self._link_back_source_ids(prepared.request.request_id)
                if durable_values != row_values or links != prepared.link_back_source_ids:
                    raise SynthesisStoreError("synthesis persistence conflict")
            connection.commit()
        except SynthesisStoreError:
            self._rollback()
            raise
        except sqlite3.Error:
            self._rollback()
            raise SynthesisStoreError("synthesis persistence failed") from None
        read_back = self.get(prepared.request.request_id)
        if read_back is None:
            raise SynthesisStoreError("synthesis persistence unavailable")
        read_back.validate_for(prepared, privacy=privacy)
        return read_back

    def get(self, request_id: str) -> DurableSynthesisRecord | None:
        if not isinstance(request_id, str) or not request_id:
            return None
        row = self._connection.execute(
            "SELECT * FROM synthesis_records WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            request_bytes = bytes(row["request_json"])
            result_bytes = bytes(row["result_json"])
            document_bytes = bytes(row["document_json"])
            document = RedactedMarkdownDocument.from_canonical_bytes(document_bytes)
        except (TypeError, ValueError):
            raise SynthesisStoreError("invalid durable synthesis document") from None
        request_digest = sha256(request_bytes).hexdigest()
        result_digest = sha256(result_bytes).hexdigest()
        document_digest = sha256(document_bytes).hexdigest()
        if (
            document.document_id != row["document_id"]
            or result_digest != row["result_digest_sha256"]
            or document_digest != row["document_digest_sha256"]
        ):
            raise SynthesisStoreError("invalid durable synthesis document")
        return DurableSynthesisRecord(
            request_id=str(row["request_id"]),
            topic_id=str(row["topic_id"]),
            state=str(row["state"]),
            request_digest_sha256=request_digest,
            result_digest_sha256=result_digest,
            document_digest_sha256=document_digest,
            document=document,
            link_back_source_ids=self._link_back_source_ids(request_id),
        )

    def record_count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM synthesis_records").fetchone()[0])

    def page_count(self) -> int:
        return int(
            self._connection.execute("SELECT COUNT(document_id) FROM synthesis_records").fetchone()[
                0
            ]
        )

    def link_count(self) -> int:
        return int(
            self._connection.execute("SELECT COUNT(*) FROM synthesis_link_backs").fetchone()[0]
        )

    def _initialize(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for statement in _SYNTHESIS_SCHEMA:
                self._connection.execute(statement)
            self._connection.commit()
        except sqlite3.Error:
            self._rollback()
            raise SynthesisStoreError("synthesis schema unavailable") from None

    def _link_back_source_ids(self, request_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            """
            SELECT source_id FROM synthesis_link_backs
            WHERE request_id = ? ORDER BY source_id
            """,
            (request_id,),
        ).fetchall()
        return tuple(str(row["source_id"]) for row in rows)

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()


def _document(
    *,
    prepared: PreparedSynthesis,
    privacy: PrivacyDecision,
) -> RedactedMarkdownDocument:
    request_id = prepared.request.request_id
    document_id = "synthesis_doc_" + sha256(request_id.encode("utf-8")).hexdigest()
    frontmatter = {
        "request_id": request_id,
        "source_ids": list(prepared.link_back_source_ids),
        "state": prepared.state,
        "topic_id": prepared.request.topic_id,
    }
    citations = {
        source.source_id: source.citation for source in prepared.request.sources
    }
    claim_lines = tuple(
        "- "
        + claim.text
        + " (confidence: "
        + claim.confidence.value
        + "; sources: "
        + ", ".join(
            f"[{source_id}](<{citations[source_id].destination}>)"
            for source_id in claim.source_ids
        )
        + ")"
        for claim in prepared.result.claims
    )
    body = "# Synthesis\n\n## Claims\n" + "\n".join(claim_lines) + "\n"
    output_digest = RedactedMarkdownDocument.output_digest_sha256(frontmatter, body)
    return RedactedMarkdownDocument.create(
        document_id=document_id,
        logical_key=document_id,
        privacy_decision=privacy,
        frontmatter=frontmatter,
        body=body,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256=sha256(prepared.result.canonical_bytes()).hexdigest(),
            output_digest_sha256=output_digest,
            policy_version="ledger-synthesis-render-v1",
        ),
    )
