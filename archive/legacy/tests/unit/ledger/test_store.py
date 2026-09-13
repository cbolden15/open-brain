from __future__ import annotations

from pathlib import Path

from open_brain_engine.core.ports import RedactedMarkdownDocument
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    AtomicMarkdownSink,
    markdown_relative_path,
)

from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.sanitize import LedgerSection, sanitize_leaf
from open_brain_legacy.ledger.service import (
    CaptureCitationResolver,
    LedgerService,
    PreparedLedgerApply,
)
from open_brain_legacy.ledger.stage import LedgerStage, stage_scan_record
from open_brain_legacy.ledger.store import SqliteLedgerStore, inspect_published_references

from .test_stage import _record


def _stage() -> LedgerStage:
    from .test_scan import _taxonomy

    return stage_scan_record(record=_record(), taxonomy=_taxonomy())


def _resolver(stage: LedgerStage) -> CaptureCitationResolver:
    citation_id = "cite-synthetic"
    citation = TrustedCitation.create(
        citation_id=citation_id,
        destination=markdown_relative_path(f"capture_ref_{citation_id}").as_posix(),
    )
    return CaptureCitationResolver(
        citations={(str(stage.binding.capture_id), stage.binding.event_id): citation}
    )


def _prepared(service: LedgerService, stage: LedgerStage) -> PreparedLedgerApply:
    sanitized = sanitize_leaf(
        item_id="synthetic-item", section=LedgerSection.SUMMARY, text="Synthetic finding"
    )
    assert sanitized.leaf is not None
    return service.prepare(stage=stage, section=LedgerSection.SUMMARY, leaf=sanitized.leaf)


def test_private_inflight_journal_contains_metadata_only_and_prepares_one_immutable_row(
    tmp_path: Path,
) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path)
    service = LedgerService(store=store, citations=_resolver(stage))
    prepared = _prepared(service, stage)

    store.journal(prepared)
    entries = store.inflight_metadata()

    assert entries == (
        {
            "citation_id": "cite-synthetic",
            "document_digest_sha256": prepared.document_digest_sha256,
            "document_ids": prepared.document_ids,
            "event_digest_sha256": stage.binding.event_digest_sha256,
            "stage_digest_sha256": stage.stage_digest_sha256,
            "state": "journaled",
            "target_logical_key": prepared.ledger_document.logical_key,
        },
    )
    assert "Synthetic finding" not in repr(entries)

    store.prepare(prepared)

    assert store.record_count() == 1
    assert store.inflight_count() == 1


def test_applied_row_is_the_visibility_manifest_for_the_complete_document_set(
    tmp_path: Path,
) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path)
    service = LedgerService(store=store, citations=_resolver(stage))
    prepared = _prepared(service, stage)
    store.journal(prepared)
    store.prepare(prepared)

    assert store.published_document_set(stage.stage_digest_sha256) is None

    markdown_root = tmp_path / "markdown"
    markdown_root.mkdir(mode=0o700)
    sink = AtomicMarkdownSink(root=markdown_root)
    reader = AtomicMarkdownReader(root=markdown_root)
    documents: tuple[RedactedMarkdownDocument, ...] = (
        prepared.capture_document,
        prepared.ledger_document,
    )
    receipts = tuple(sink.write_if_absent(document) for document in documents)
    store.finalize(prepared, reader=reader, receipts=receipts)

    published = store.published_document_set(stage.stage_digest_sha256)
    assert published is not None
    assert published.document_ids == prepared.document_ids
    assert published.sink_digests == tuple(receipt.digest_sha256 for receipt in receipts)


def test_published_reference_inspection_detects_missing_physical_document(
    tmp_path: Path,
) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path)
    service = LedgerService(store=store, citations=_resolver(stage))
    prepared = _prepared(service, stage)
    store.journal(prepared)
    store.prepare(prepared)
    markdown_root = tmp_path / "markdown"
    markdown_root.mkdir(mode=0o700)
    sink = AtomicMarkdownSink(root=markdown_root)
    reader = AtomicMarkdownReader(root=markdown_root)
    documents = (prepared.capture_document, prepared.ledger_document)
    receipts = tuple(sink.write_if_absent(document) for document in documents)
    store.finalize(prepared, reader=reader, receipts=receipts)

    healthy = inspect_published_references(
        metadata_root=tmp_path,
        database_name="ledger.sqlite3",
        content_root=markdown_root,
    )
    (markdown_root / markdown_relative_path(prepared.document_ids[0])).unlink()
    stale = inspect_published_references(
        metadata_root=tmp_path,
        database_name="ledger.sqlite3",
        content_root=markdown_root,
    )

    assert healthy.reference_count == 2
    assert healthy.stale_count == 0
    assert stale.reference_count == 2
    assert stale.stale_count == 1
