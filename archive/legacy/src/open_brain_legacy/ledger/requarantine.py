"""Durable, idempotent replay for private quarantined ledger leaves."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.storage.sqlite import connect_database

from .merge import TrustedCitation
from .sanitize import (
    LedgerSection,
    QuarantineReason,
    SanitizedLeaf,
    sanitize_leaf,
)

_MAX_TEXT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class DurableQuarantineEntry:
    record_id: str
    item_id: str
    section: LedgerSection
    text: str
    reason: QuarantineReason
    citations: tuple[TrustedCitation, ...]

    @classmethod
    def create(
        cls,
        *,
        item_id: str,
        section: LedgerSection,
        text: str,
        reason: QuarantineReason,
        citations: tuple[TrustedCitation, ...],
    ) -> DurableQuarantineEntry:
        if (
            not isinstance(item_id, str)
            or not item_id
            or any(ord(character) < 32 for character in item_id)
            or not isinstance(section, LedgerSection)
            or not isinstance(text, str)
            or not text
            or len(text.encode("utf-8")) > _MAX_TEXT_BYTES
            or not isinstance(reason, QuarantineReason)
            or not isinstance(citations, tuple)
            or not citations
            or any(not isinstance(citation, TrustedCitation) for citation in citations)
        ):
            raise ValueError("invalid durable quarantine entry")
        ordered = tuple(sorted(citations, key=lambda citation: citation.citation_id))
        for citation in ordered:
            citation.validate()
        if len({citation.citation_id for citation in ordered}) != len(ordered):
            raise ValueError("invalid durable quarantine citations")
        identity = {
            "citations": [
                {
                    "citation_id": citation.citation_id,
                    "destination": citation.destination,
                }
                for citation in ordered
            ],
            "item_id": item_id,
            "reason": reason.value,
            "section": section.value,
            "text": text,
        }
        return cls(
            record_id="quarantine_" + sha256(canonical_json_bytes(identity)).hexdigest(),
            item_id=item_id,
            section=section,
            text=text,
            reason=reason,
            citations=ordered,
        )

    def validate(self) -> None:
        recreated = DurableQuarantineEntry.create(
            item_id=self.item_id,
            section=self.section,
            text=self.text,
            reason=self.reason,
            citations=self.citations,
        )
        if recreated != self:
            raise ValueError("durable quarantine entry binding mismatch")

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(
            {
                "citations": [
                    {
                        "citation_id": citation.citation_id,
                        "destination": citation.destination,
                    }
                    for citation in self.citations
                ],
                "item_id": self.item_id,
                "reason": self.reason.value,
                "record_id": self.record_id,
                "section": self.section.value,
                "text": self.text,
            }
        )


@dataclass(frozen=True, slots=True)
class RestoredLeaf:
    section: LedgerSection
    leaf: SanitizedLeaf
    citations: tuple[TrustedCitation, ...]


class RequarantineDisposition(StrEnum):
    HELD = "held"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class RequarantineResult:
    record_id: str
    disposition: RequarantineDisposition


class SqliteQuarantineStore:
    """One confined SQLite transaction owns held state and restored citation dedupe."""

    def __init__(self, *, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("invalid quarantine root")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        self._connection = connect_database(root=root, database_name="ledger-quarantine.sqlite3")
        self._initialize()

    def put(self, entry: DurableQuarantineEntry) -> None:
        entry.validate()
        payload = entry.canonical_bytes()
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT entry_json FROM quarantine_entries WHERE record_id = ?",
                (entry.record_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO quarantine_entries(record_id, entry_json, state) "
                    "VALUES (?, ?, 'held')",
                    (entry.record_id, payload),
                )
            elif bytes(row["entry_json"]) != payload:
                raise ValueError("quarantine immutable entry conflict")
            connection.commit()
        except ValueError:
            self._rollback()
            raise
        except sqlite3.Error:
            self._rollback()
            raise ValueError("quarantine persistence failed") from None

    def entries(self, *, limit: int) -> tuple[DurableQuarantineEntry, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("invalid requarantine limit")
        rows = self._connection.execute(
            "SELECT entry_json FROM quarantine_entries ORDER BY sequence LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(self._decode_entry(bytes(row["entry_json"])) for row in rows)

    def restore(self, *, entry: DurableQuarantineEntry, leaf: SanitizedLeaf) -> None:
        entry.validate()
        leaf.validate()
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT entry_json FROM quarantine_entries WHERE record_id = ?",
                (entry.record_id,),
            ).fetchone()
            if row is None or bytes(row["entry_json"]) != entry.canonical_bytes():
                raise ValueError("quarantine immutable entry conflict")
            restored = connection.execute(
                "SELECT text FROM restored_leaves WHERE section = ? AND normalized_key = ?",
                (entry.section.value, leaf.normalized_key),
            ).fetchone()
            if restored is None:
                connection.execute(
                    "INSERT INTO restored_leaves(section, normalized_key, text) VALUES (?, ?, ?)",
                    (entry.section.value, leaf.normalized_key, leaf.text),
                )
            elif str(restored["text"]) != leaf.text:
                raise ValueError("restored ledger leaf conflict")
            for citation in entry.citations:
                citation_row = connection.execute(
                    "SELECT destination FROM restored_citations "
                    "WHERE section = ? AND normalized_key = ? AND citation_id = ?",
                    (entry.section.value, leaf.normalized_key, citation.citation_id),
                ).fetchone()
                if citation_row is None:
                    connection.execute(
                        "INSERT INTO restored_citations(" 
                        "section, normalized_key, citation_id, destination" 
                        ") VALUES (?, ?, ?, ?)",
                        (
                            entry.section.value,
                            leaf.normalized_key,
                            citation.citation_id,
                            citation.destination,
                        ),
                    )
                elif str(citation_row["destination"]) != citation.destination:
                    raise ValueError("restored ledger citation conflict")
            connection.execute(
                "UPDATE quarantine_entries SET state = 'restored' WHERE record_id = ?",
                (entry.record_id,),
            )
            connection.commit()
        except ValueError:
            self._rollback()
            raise
        except sqlite3.Error:
            self._rollback()
            raise ValueError("requarantine restore failed") from None

    def held_count(self) -> int:
        return self._state_count("held")

    def restored_count(self) -> int:
        return self._state_count("restored")

    def restored_leaves(self) -> tuple[RestoredLeaf, ...]:
        rows = self._connection.execute(
            "SELECT section, normalized_key, text FROM restored_leaves "
            "ORDER BY section, normalized_key"
        ).fetchall()
        values: list[RestoredLeaf] = []
        for row in rows:
            citation_rows = self._connection.execute(
                "SELECT citation_id, destination FROM restored_citations "
                "WHERE section = ? AND normalized_key = ? ORDER BY citation_id",
                (row["section"], row["normalized_key"]),
            ).fetchall()
            values.append(
                RestoredLeaf(
                    section=LedgerSection(str(row["section"])),
                    leaf=SanitizedLeaf(
                        text=str(row["text"]),
                        normalized_key=str(row["normalized_key"]),
                    ),
                    citations=tuple(
                        TrustedCitation.create(
                            citation_id=str(citation["citation_id"]),
                            destination=str(citation["destination"]),
                        )
                        for citation in citation_rows
                    ),
                )
            )
        return tuple(values)

    def _state_count(self, state: str) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM quarantine_entries WHERE state = ?", (state,)
            ).fetchone()[0]
        )

    def _initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS quarantine_entries (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                entry_json BLOB NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('held', 'restored'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS restored_leaves (
                section TEXT NOT NULL,
                normalized_key TEXT NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY (section, normalized_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS restored_citations (
                section TEXT NOT NULL,
                normalized_key TEXT NOT NULL,
                citation_id TEXT NOT NULL,
                destination TEXT NOT NULL,
                PRIMARY KEY (section, normalized_key, citation_id),
                FOREIGN KEY (section, normalized_key)
                    REFERENCES restored_leaves(section, normalized_key)
            )
            """,
        )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                self._connection.execute(statement)
            self._connection.commit()
        except sqlite3.Error:
            self._rollback()
            raise ValueError("quarantine schema unavailable") from None

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            self._connection.rollback()

    @staticmethod
    def _decode_entry(payload: bytes) -> DurableQuarantineEntry:
        try:
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            citations_value = value["citations"]
            if not isinstance(citations_value, list):
                raise ValueError
            entry = DurableQuarantineEntry.create(
                item_id=str(value["item_id"]),
                section=LedgerSection(str(value["section"])),
                text=str(value["text"]),
                reason=QuarantineReason(str(value["reason"])),
                citations=tuple(
                    TrustedCitation.create(
                        citation_id=citation["citation_id"],
                        destination=citation["destination"],
                    )
                    for citation in citations_value
                    if isinstance(citation, dict)
                ),
            )
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise ValueError("invalid durable quarantine entry") from None
        if entry.record_id != value.get("record_id") or entry.canonical_bytes() != payload:
            raise ValueError("invalid durable quarantine entry")
        return entry


class RequarantineService:
    def __init__(self, *, store: SqliteQuarantineStore) -> None:
        if not isinstance(store, SqliteQuarantineStore):
            raise ValueError("durable quarantine store required")
        self._store = store

    def replay(
        self, *, limit: int, dry_run: bool
    ) -> tuple[RequarantineResult, ...]:
        if not isinstance(dry_run, bool):
            raise ValueError("invalid requarantine dry-run flag")
        results: list[RequarantineResult] = []
        for entry in self._store.entries(limit=limit):
            sanitized = sanitize_leaf(
                item_id=entry.item_id,
                section=entry.section,
                text=entry.text,
            )
            if sanitized.leaf is None:
                results.append(
                    RequarantineResult(entry.record_id, RequarantineDisposition.HELD)
                )
                continue
            if not dry_run:
                self._store.restore(entry=entry, leaf=sanitized.leaf)
            results.append(
                RequarantineResult(entry.record_id, RequarantineDisposition.RESTORED)
            )
        return tuple(results)
