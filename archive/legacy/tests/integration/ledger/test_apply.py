from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import pytest
from open_brain_engine.capture.models import DistillationWorkItem
from open_brain_engine.core.models import (
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyTier,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy
from open_brain_engine.core.ports import (
    EventRecord,
    PutDisposition,
    PutResult,
    RedactedMarkdownDocument,
    RedactionReceipt,
)
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    AtomicMarkdownSink,
    markdown_relative_path,
    rendered_markdown_bytes,
)

from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.models import LedgerRoute, LedgerScanRecord, LedgerTaxonomy
from open_brain_legacy.ledger.reconcile import LedgerReconciler, ReconcileDisposition
from open_brain_legacy.ledger.sanitize import LedgerSection, sanitize_leaf
from open_brain_legacy.ledger.scan import scan_distillation_work_item
from open_brain_legacy.ledger.service import (
    ApplyBoundary,
    CaptureCitationResolver,
    LedgerService,
    LedgerServiceError,
    MarkdownWriter,
    PreparedLedgerApply,
)
from open_brain_legacy.ledger.stage import LedgerStage, stage_scan_record
from open_brain_legacy.ledger.store import SqliteLedgerStore


class _Stop(RuntimeError):
    pass


class _SecondWriteFailure(RuntimeError):
    pass


class _ObservedSink:
    def __init__(self, *, root: Path, store: SqliteLedgerStore) -> None:
        root.mkdir(mode=0o700, exist_ok=True)
        self._sink = AtomicMarkdownSink(root=root)
        self.reader = AtomicMarkdownReader(root=root)
        self._store = store
        self.transaction_states: list[bool] = []

    def write_if_absent(self, document: RedactedMarkdownDocument) -> PutResult:
        self.transaction_states.append(self._store.in_transaction)
        return self._sink.write_if_absent(document)


class _FailSecondSink(_ObservedSink):
    def write_if_absent(self, document: RedactedMarkdownDocument) -> PutResult:
        if len(self.transaction_states) == 1:
            self.transaction_states.append(self._store.in_transaction)
            raise _SecondWriteFailure
        return super().write_if_absent(document)


_AttackMode = Literal[
    "wrong_type",
    "wrong_id",
    "wrong_digest",
    "invalid_disposition",
    "false_duplicate",
    "missing_receipt",
    "swapped_receipts",
    "read_back_mismatch",
    "memory_self_attestation",
]


class _ReceiptAttackSink:
    def __init__(self, mode: _AttackMode, *, root: Path) -> None:
        self._mode = mode
        self._write_index = 0
        self._root = root
        self._root.mkdir(mode=0o700, exist_ok=True)
        self._memory: dict[str, bytes] = {}
        self.documents: tuple[RedactedMarkdownDocument, RedactedMarkdownDocument] | None = None

    def write_if_absent(self, document: RedactedMarkdownDocument) -> PutResult:
        if self._mode == "wrong_type":
            return cast(PutResult, object())
        if self._mode == "missing_receipt":
            return cast(PutResult, None)
        assert self.documents is not None
        index = self._write_index
        self._write_index += 1
        receipt_document = (
            self.documents[1 - index] if self._mode == "swapped_receipts" else document
        )
        disposition = (
            PutDisposition.DUPLICATE if self._mode == "false_duplicate" else PutDisposition.CREATED
        )
        if self._mode == "invalid_disposition":
            disposition = cast(PutDisposition, "invalid")
        record_id = (
            "wrong-document-id" if self._mode == "wrong_id" else receipt_document.document_id
        )
        digest = sha256(rendered_markdown_bytes(receipt_document)).hexdigest()
        if self._mode == "wrong_digest":
            digest = "0" * 64
        if self._mode == "memory_self_attestation":
            self._memory[document.document_id] = rendered_markdown_bytes(document)
        if self._mode == "read_back_mismatch":
            relative = markdown_relative_path(document.document_id)
            path = self._root / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(b"synthetic mismatched persisted bytes")
        return PutResult(disposition=disposition, record_id=record_id, digest_sha256=digest)

    def read_back(self, document_id: str) -> bytes | None:
        if self._mode == "memory_self_attestation":
            return self._memory.get(document_id)
        if self._mode == "read_back_mismatch":
            return b"synthetic mismatched persisted bytes"
        return None


def _stage() -> LedgerStage:
    return stage_scan_record(record=_record(), taxonomy=_taxonomy())


def _taxonomy() -> LedgerTaxonomy:
    return LedgerTaxonomy.create(
        version="synthetic-v1",
        routes=(
            LedgerRoute.create(
                path_prefix=("professional", "research"),
                topic_id="research",
                topic_label="Research",
                privacy_tier=PrivacyTier.WORK,
            ),
        ),
    )


def _record() -> LedgerScanRecord:
    payload = {
        "text": "synthetic extracted text",
        "capture_why": "Keep this for the research backlog",
        "capture_source": CaptureSource.CLI.value,
        "source_type": SourceType.WEB.value,
        "content_kind": ContentKind.ARTICLE.value,
        "provenance": {
            "source_ref": "https://example.test/synthetic-source",
            "content_origin": ContentOrigin.THIRD_PARTY.value,
            "owner_context": CaptureWhyOrigin.OWNER_AUTHORED.value,
        },
    }
    event = EventRecord.create(
        event_id="evt_synthetic",
        stream_id="cap_" + "a" * 64,
        event_type="capture.extracted",
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
        privacy_decision=classify_privacy(PrivacyTier.WORK, policy_version="policy-v1"),
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256="b" * 64,
            output_digest_sha256=EventRecord.output_digest_sha256(payload),
            policy_version="redaction-v1",
        ),
    )
    item = DistillationWorkItem.create(
        capture_id=event.stream_id,
        event_id=event.event_id,
        redacted_event_digest_sha256=sha256(event.canonical_bytes()).hexdigest(),
    )
    return scan_distillation_work_item(
        item=item,
        event=event,
        taxonomy=_taxonomy(),
        source_locator=PurePosixPath("professional/research/note"),
    )


def _resolver(stage: LedgerStage) -> CaptureCitationResolver:
    citation_id = "cite-synthetic"
    return CaptureCitationResolver(
        citations={
            (str(stage.binding.capture_id), stage.binding.event_id): TrustedCitation.create(
                citation_id=citation_id,
                destination=markdown_relative_path(f"capture_ref_{citation_id}").as_posix(),
            )
        }
    )


def _prepared(service: LedgerService, stage: LedgerStage) -> PreparedLedgerApply:
    sanitized = sanitize_leaf(
        item_id="synthetic-item", section=LedgerSection.SUMMARY, text="Synthetic finding"
    )
    assert sanitized.leaf is not None
    return service.prepare(stage=stage, section=LedgerSection.SUMMARY, leaf=sanitized.leaf)


def _service(
    *,
    store: SqliteLedgerStore,
    sink: MarkdownWriter,
    reader: AtomicMarkdownReader,
    stage: LedgerStage,
    after_transition: Callable[[ApplyBoundary], None] | None = None,
) -> LedgerService:
    return LedgerService(
        store=store,
        sink=sink,
        reader=reader,
        citations=_resolver(stage),
        after_transition=after_transition,
    )


@pytest.mark.parametrize("boundary", tuple(ApplyBoundary))
def test_apply_reconciles_each_crash_boundary_without_duplicate_documents(
    tmp_path: Path, boundary: ApplyBoundary
) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path / "private")
    sink = _ObservedSink(root=tmp_path / "markdown", store=store)

    def stop_after(current: ApplyBoundary) -> None:
        if current is boundary:
            raise _Stop

    interrupted = _service(
        store=store,
        sink=sink,
        reader=sink.reader,
        stage=stage,
        after_transition=stop_after,
    )
    prepared = _prepared(interrupted, stage)
    with pytest.raises(_Stop):
        interrupted.apply(stage=stage, prepared=prepared)

    replay = _service(store=store, sink=sink, reader=sink.reader, stage=stage)
    result = replay.apply(stage=stage, prepared=prepared)

    assert result.status == "applied"
    assert store.record_count() == 1
    assert store.inflight_count() == 0
    assert len(tuple((tmp_path / "markdown").rglob("*.md"))) == 2
    assert sink.transaction_states
    assert not any(sink.transaction_states)


def test_apply_commits_prepared_row_before_the_markdown_sink(tmp_path: Path) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path / "private")
    sink = _ObservedSink(root=tmp_path / "markdown", store=store)
    service = _service(store=store, sink=sink, reader=sink.reader, stage=stage)

    result = service.apply(stage=stage, prepared=_prepared(service, stage))

    assert result.status == "applied"
    assert store.record_count() == 1
    assert sink.transaction_states == [False, False]


def test_apply_replay_keeps_one_deterministic_document_set(tmp_path: Path) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path / "private")
    sink = _ObservedSink(root=tmp_path / "markdown", store=store)
    service = _service(store=store, sink=sink, reader=sink.reader, stage=stage)
    prepared = _prepared(service, stage)

    assert service.apply(stage=stage, prepared=prepared).status == "applied"
    assert service.apply(stage=stage, prepared=prepared).status == "applied"
    assert store.record_count() == 1
    assert store.inflight_count() == 0
    assert {
        path.relative_to(tmp_path / "markdown").as_posix()
        for path in (tmp_path / "markdown").rglob("*.md")
    } == {markdown_relative_path(document_id).as_posix() for document_id in prepared.document_ids}


def test_second_document_failure_keeps_partial_files_out_of_official_visibility(
    tmp_path: Path,
) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path / "private")
    failing_sink = _FailSecondSink(root=tmp_path / "markdown", store=store)
    interrupted = _service(
        store=store,
        sink=failing_sink,
        reader=failing_sink.reader,
        stage=stage,
    )
    prepared = _prepared(interrupted, stage)

    with pytest.raises(_SecondWriteFailure):
        interrupted.apply(stage=stage, prepared=prepared)

    assert len(tuple((tmp_path / "markdown").rglob("*.md"))) == 1
    assert store.published_document_set(stage.stage_digest_sha256) is None

    replay_sink = _ObservedSink(root=tmp_path / "markdown", store=store)
    replay = _service(
        store=store,
        sink=replay_sink,
        reader=replay_sink.reader,
        stage=stage,
    )
    assert replay.apply(stage=stage, prepared=prepared).status == "applied"
    published = store.published_document_set(stage.stage_digest_sha256)
    assert published is not None
    assert published.document_ids == prepared.document_ids
    assert len(published.sink_digests) == 2


def test_inter_document_crash_has_no_manifest_until_reconciliation(tmp_path: Path) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path / "private")
    sink = _ObservedSink(root=tmp_path / "markdown", store=store)

    def stop_after_first(current: ApplyBoundary) -> None:
        if current is ApplyBoundary.FIRST_DOCUMENT_WRITTEN:
            assert store.published_document_set(stage.stage_digest_sha256) is None
            raise _Stop

    interrupted = _service(
        store=store,
        sink=sink,
        reader=sink.reader,
        stage=stage,
        after_transition=stop_after_first,
    )
    prepared = _prepared(interrupted, stage)
    with pytest.raises(_Stop):
        interrupted.apply(stage=stage, prepared=prepared)

    assert store.published_document_set(stage.stage_digest_sha256) is None
    replay = _service(store=store, sink=sink, reader=sink.reader, stage=stage)
    replay.apply(stage=stage, prepared=prepared)
    assert store.published_document_set(stage.stage_digest_sha256) is not None


@pytest.mark.parametrize(
    ("boundary", "expected"),
    (
        (ApplyBoundary.JOURNALED, ReconcileDisposition.SAFE_RESET),
        (ApplyBoundary.FIRST_DOCUMENT_WRITTEN, ReconcileDisposition.CONFLICT),
        (ApplyBoundary.WRITTEN, ReconcileDisposition.ROLLED_FORWARD),
    ),
)
def test_explicit_reconcile_classifies_each_crash_state(
    tmp_path: Path,
    boundary: ApplyBoundary,
    expected: ReconcileDisposition,
) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path / "private")
    sink = _ObservedSink(root=tmp_path / "markdown", store=store)

    def stop_after(current: ApplyBoundary) -> None:
        if current is boundary:
            raise _Stop

    interrupted = _service(
        store=store,
        sink=sink,
        reader=sink.reader,
        stage=stage,
        after_transition=stop_after,
    )
    prepared = _prepared(interrupted, stage)
    with pytest.raises(_Stop):
        interrupted.apply(stage=stage, prepared=prepared)

    result = LedgerReconciler(store=store, reader=sink.reader).reconcile(prepared=prepared)

    assert result.disposition is expected
    if expected is ReconcileDisposition.ROLLED_FORWARD:
        assert store.published_document_set(stage.stage_digest_sha256) is not None
        assert store.inflight_count() == 0
        rerun = LedgerReconciler(store=store, reader=sink.reader).reconcile(prepared=prepared)
        assert rerun.disposition is ReconcileDisposition.ROLLED_FORWARD
    elif expected is ReconcileDisposition.SAFE_RESET:
        assert store.record_count() == 0
        assert store.inflight_count() == 0
    else:
        assert store.published_document_set(stage.stage_digest_sha256) is None
        assert store.inflight_count() == 1


@pytest.mark.parametrize(
    "mode",
    (
        "wrong_type",
        "wrong_id",
        "wrong_digest",
        "invalid_disposition",
        "false_duplicate",
        "missing_receipt",
        "swapped_receipts",
        "read_back_mismatch",
        "memory_self_attestation",
    ),
)
def test_malformed_or_unverifiable_sink_receipt_cannot_publish(
    tmp_path: Path, mode: _AttackMode
) -> None:
    stage = _stage()
    store = SqliteLedgerStore(root=tmp_path / "private")
    markdown_root = tmp_path / "markdown"
    sink = _ReceiptAttackSink(mode, root=markdown_root)
    reader = AtomicMarkdownReader(root=markdown_root)
    service = _service(store=store, sink=sink, reader=reader, stage=stage)
    prepared = _prepared(service, stage)
    sink.documents = (prepared.capture_document, prepared.ledger_document)

    with pytest.raises(LedgerServiceError):
        service.apply(stage=stage, prepared=prepared)

    assert store.record_count() == 1
    assert store.inflight_count() == 1
    assert store.published_document_set(stage.stage_digest_sha256) is None
    if mode == "memory_self_attestation":
        assert sink.read_back(prepared.capture_document.document_id) is not None
        assert not tuple(markdown_root.rglob("*.md"))
