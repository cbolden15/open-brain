"""Deterministic ledger preparation and crash-safe apply orchestration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import (
    PutDisposition,
    PutResult,
    RedactedMarkdownDocument,
    RedactionReceipt,
)
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    markdown_relative_path,
    rendered_markdown_bytes,
)

from .merge import TrustedCitation, create_ledger_page, merge_leaf
from .sanitize import LedgerSection, SanitizedLeaf
from .stage import LedgerStage
from .store import LedgerStoreError, SqliteLedgerStore


class LedgerServiceError(LedgerStoreError):
    """A ledger operation could not safely produce or apply immutable output."""


class MarkdownWriter(Protocol):
    def write_if_absent(self, document: RedactedMarkdownDocument) -> PutResult: ...


class ApplyBoundary(StrEnum):
    JOURNALED = "journaled"
    PREPARED = "prepared"
    FIRST_DOCUMENT_WRITTEN = "first_document_written"
    WRITTEN = "written"
    FINALIZED = "finalized"


@dataclass(frozen=True, slots=True)
class CaptureCitationResolver:
    citations: Mapping[tuple[str, str], TrustedCitation]

    def resolve(self, stage: LedgerStage) -> TrustedCitation:
        stage.validate()
        citation = self.citations.get((str(stage.binding.capture_id), stage.binding.event_id))
        if not isinstance(citation, TrustedCitation):
            raise LedgerServiceError("citation unavailable")
        return citation


@dataclass(frozen=True, slots=True)
class PreparedLedgerApply:
    stage_digest_sha256: str
    source_id: str
    event_digest_sha256: str
    citation_id: str
    document_ids: tuple[str, str]
    document_digest_sha256: str
    capture_document: RedactedMarkdownDocument
    ledger_document: RedactedMarkdownDocument

    def validate(self) -> None:
        if (
            len(self.document_ids) != 2
            or self.document_ids
            != (self.capture_document.document_id, self.ledger_document.document_id)
            or self.document_digest_sha256
            != _documents_digest((self.capture_document, self.ledger_document))
        ):
            raise LedgerServiceError("prepared ledger binding mismatch")

    def validate_for(self, stage: LedgerStage) -> None:
        self.validate()
        stage.validate()
        if (
            self.stage_digest_sha256 != stage.stage_digest_sha256
            or self.source_id != str(stage.binding.capture_id)
            or self.event_digest_sha256 != stage.binding.event_digest_sha256
        ):
            raise LedgerServiceError("prepared ledger stage mismatch")


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status: str


class LedgerService:
    def __init__(
        self,
        *,
        store: SqliteLedgerStore,
        citations: CaptureCitationResolver,
        sink: MarkdownWriter | None = None,
        reader: AtomicMarkdownReader | None = None,
        after_transition: Callable[[ApplyBoundary], None] | None = None,
    ) -> None:
        self._store = store
        self._citations = citations
        self._sink = sink
        self._reader = reader
        self._after_transition = after_transition

    def prepare(
        self, *, stage: LedgerStage, section: LedgerSection, leaf: SanitizedLeaf
    ) -> PreparedLedgerApply:
        if not isinstance(section, LedgerSection) or not isinstance(leaf, SanitizedLeaf):
            raise LedgerServiceError("invalid ledger leaf")
        stage.validate()
        citation = self._citations.resolve(stage)
        capture_document = self._capture_document(stage=stage, citation=citation)
        page_result = create_ledger_page(stage=stage)
        if page_result.page is None:
            raise LedgerServiceError("ledger provenance unavailable")
        merge_result = merge_leaf(
            page=page_result.page, section=section, leaf=leaf, citation=citation
        )
        if merge_result.page is None:
            raise LedgerServiceError("ledger merge unavailable")
        ledger_document = self._ledger_document(
            stage=stage, citation=citation, body=merge_result.page.render()
        )
        prepared = PreparedLedgerApply(
            stage_digest_sha256=stage.stage_digest_sha256,
            source_id=str(stage.binding.capture_id),
            event_digest_sha256=stage.binding.event_digest_sha256,
            citation_id=citation.citation_id,
            document_ids=(capture_document.document_id, ledger_document.document_id),
            document_digest_sha256=_documents_digest((capture_document, ledger_document)),
            capture_document=capture_document,
            ledger_document=ledger_document,
        )
        prepared.validate_for(stage)
        return prepared

    def apply(self, *, stage: LedgerStage, prepared: PreparedLedgerApply) -> ApplyResult:
        sink = self._sink
        reader = self._reader
        if sink is None or type(reader) is not AtomicMarkdownReader or id(sink) == id(reader):
            raise LedgerServiceError("ledger sink unavailable")
        prepared.validate_for(stage)
        self._store.journal(prepared)
        self._transition(ApplyBoundary.JOURNALED)
        if self._store.prepare(prepared):
            self._store.clear_journal(prepared)
            self._transition(ApplyBoundary.FINALIZED)
            return ApplyResult(status="applied")
        self._transition(ApplyBoundary.PREPARED)
        receipts: list[PutResult] = []
        for index, document in enumerate((prepared.capture_document, prepared.ledger_document)):
            receipt = sink.write_if_absent(document)
            verified_receipt = _verify_receipt(document=document, receipt=receipt)
            try:
                persisted_bytes = reader.read_back(document.document_id)
            except Exception:
                raise LedgerServiceError("ledger sink read-back failed") from None
            if type(persisted_bytes) is not bytes or persisted_bytes != rendered_markdown_bytes(
                document
            ):
                raise LedgerServiceError("ledger sink read-back mismatch")
            receipts.append(verified_receipt)
            if index == 0:
                self._transition(ApplyBoundary.FIRST_DOCUMENT_WRITTEN)
        self._transition(ApplyBoundary.WRITTEN)
        self._store.finalize(prepared, reader=reader, receipts=tuple(receipts))
        self._transition(ApplyBoundary.FINALIZED)
        return ApplyResult(status="applied")

    @staticmethod
    def _capture_document(
        *, stage: LedgerStage, citation: TrustedCitation
    ) -> RedactedMarkdownDocument:
        document_id = f"capture_ref_{citation.citation_id}"
        if markdown_relative_path(document_id).as_posix() != citation.destination:
            raise LedgerServiceError("citation destination unavailable")
        frontmatter = {
            "citation_id": citation.citation_id,
            "event_digest_sha256": stage.binding.event_digest_sha256,
            "stage_digest_sha256": stage.stage_digest_sha256,
        }
        body = "# Capture reference\n"
        return _document(
            document_id=document_id,
            logical_key=document_id,
            stage=stage,
            frontmatter=frontmatter,
            body=body,
        )

    @staticmethod
    def _ledger_document(
        *, stage: LedgerStage, citation: TrustedCitation, body: str
    ) -> RedactedMarkdownDocument:
        document_id = (
            "ledger_doc_"
            + sha256(
                canonical_json_bytes(
                    {
                        "citation_id": citation.citation_id,
                        "stage_digest_sha256": stage.stage_digest_sha256,
                    }
                )
            ).hexdigest()
        )
        logical_key = "ledger_" + stage.stage_digest_sha256
        return _document(
            document_id=document_id,
            logical_key=logical_key,
            stage=stage,
            frontmatter={
                "citation_id": citation.citation_id,
                "event_digest_sha256": stage.binding.event_digest_sha256,
                "stage_digest_sha256": stage.stage_digest_sha256,
            },
            body=body,
        )

    def _transition(self, boundary: ApplyBoundary) -> None:
        if self._after_transition is not None:
            self._after_transition(boundary)


def _document(
    *,
    document_id: str,
    logical_key: str,
    stage: LedgerStage,
    frontmatter: Mapping[str, object],
    body: str,
) -> RedactedMarkdownDocument:
    output_digest = RedactedMarkdownDocument.output_digest_sha256(frontmatter, body)
    return RedactedMarkdownDocument.create(
        document_id=document_id,
        logical_key=logical_key,
        privacy_decision=stage.binding.privacy_decision,
        frontmatter=frontmatter,
        body=body,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256=stage.redaction_receipt.output_digest_sha256,
            output_digest_sha256=output_digest,
            policy_version="ledger-render-v1",
        ),
    )


def _documents_digest(documents: tuple[RedactedMarkdownDocument, RedactedMarkdownDocument]) -> str:
    return sha256(canonical_json_bytes([document.to_dict() for document in documents])).hexdigest()


def _verify_receipt(*, document: RedactedMarkdownDocument, receipt: object) -> PutResult:
    expected_digest = sha256(rendered_markdown_bytes(document)).hexdigest()
    if (
        type(receipt) is not PutResult
        or type(receipt.disposition) is not PutDisposition
        or receipt.disposition not in {PutDisposition.CREATED, PutDisposition.DUPLICATE}
        or receipt.record_id != document.document_id
        or receipt.digest_sha256 != expected_digest
    ):
        raise LedgerServiceError("invalid ledger sink receipt")
    return receipt
