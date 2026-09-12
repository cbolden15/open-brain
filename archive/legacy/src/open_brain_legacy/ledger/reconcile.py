"""Explicit crash reconciliation for receipt-bound ledger publication."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from open_brain_engine.core.ports import PutDisposition, PutResult
from open_brain_engine.storage.frontmatter import AtomicMarkdownReader, rendered_markdown_bytes

from .service import PreparedLedgerApply
from .store import LedgerStoreError, SqliteLedgerStore


class ReconcileDisposition(StrEnum):
    ROLLED_FORWARD = "rolled_forward"
    SAFE_RESET = "safe_reset"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    disposition: ReconcileDisposition
    stage_digest_sha256: str


class LedgerReconciler:
    """Classify one prepared apply without guessing across crash boundaries."""

    def __init__(self, *, store: SqliteLedgerStore, reader: AtomicMarkdownReader) -> None:
        if not isinstance(store, SqliteLedgerStore) or type(reader) is not AtomicMarkdownReader:
            raise ValueError("approved ledger reconciliation dependencies required")
        self._store = store
        self._reader = reader

    def reconcile(self, *, prepared: PreparedLedgerApply) -> ReconcileResult:
        try:
            prepared.validate()
            snapshot = self._store.reconciliation_snapshot(prepared)
        except (LedgerStoreError, TypeError, ValueError):
            return ReconcileResult(
                ReconcileDisposition.CONFLICT,
                getattr(prepared, "stage_digest_sha256", "invalid"),
            )
        documents = (prepared.capture_document, prepared.ledger_document)
        expected = tuple(rendered_markdown_bytes(document) for document in documents)
        try:
            persisted = tuple(
                self._reader.read_back(document.document_id) for document in documents
            )
        except Exception:
            return ReconcileResult(
                ReconcileDisposition.CONFLICT,
                prepared.stage_digest_sha256,
            )
        if any(
            value is not None and value != wanted
            for value, wanted in zip(persisted, expected, strict=True)
        ):
            return ReconcileResult(
                ReconcileDisposition.CONFLICT,
                prepared.stage_digest_sha256,
            )
        present = tuple(value is not None for value in persisted)
        if all(present):
            if snapshot.row_state not in {"prepared", "applied", "slimmed"}:
                return ReconcileResult(
                    ReconcileDisposition.CONFLICT,
                    prepared.stage_digest_sha256,
                )
            receipts = tuple(
                PutResult(
                    disposition=PutDisposition.DUPLICATE,
                    record_id=document.document_id,
                    digest_sha256=sha256(payload).hexdigest(),
                )
                for document, payload in zip(documents, expected, strict=True)
            )
            try:
                self._store.finalize(prepared, reader=self._reader, receipts=receipts)
            except LedgerStoreError:
                return ReconcileResult(
                    ReconcileDisposition.CONFLICT,
                    prepared.stage_digest_sha256,
                )
            return ReconcileResult(
                ReconcileDisposition.ROLLED_FORWARD,
                prepared.stage_digest_sha256,
            )
        if any(present) or snapshot.row_state in {"applied", "slimmed"}:
            return ReconcileResult(
                ReconcileDisposition.CONFLICT,
                prepared.stage_digest_sha256,
            )
        try:
            self._store.reset_reconciliation(prepared)
        except LedgerStoreError:
            return ReconcileResult(
                ReconcileDisposition.CONFLICT,
                prepared.stage_digest_sha256,
            )
        return ReconcileResult(
            ReconcileDisposition.SAFE_RESET,
            prepared.stage_digest_sha256,
        )
