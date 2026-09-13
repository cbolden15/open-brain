from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.capture.models import DistillationWorkItem
from open_brain_engine.core.ids import CaptureId, ReviewId
from open_brain_engine.core.models import (
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    Intent,
    PrivacyTier,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy
from open_brain_engine.core.ports import EventRecord, PutDisposition, PutResult, RedactionReceipt
from open_brain_engine.engine import LockScope
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ApprovedIntentRecord,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
)
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    AtomicMarkdownSink,
    markdown_relative_path,
)

from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.models import LedgerRoute, LedgerScanRecord, LedgerTaxonomy
from open_brain_legacy.ledger.sanitize import LedgerSection, sanitize_leaf
from open_brain_legacy.ledger.scan import scan_distillation_work_item
from open_brain_legacy.ledger.service import (
    CaptureCitationResolver,
    LedgerService,
    PreparedLedgerApply,
)
from open_brain_legacy.ledger.stage import LedgerStage, stage_scan_record
from open_brain_legacy.ledger.store import SqliteLedgerStore
from open_brain_legacy.operations.curation_runtime import (
    CurationBatch,
    CurationEffectCapability,
    CurationPromotion,
    CurationReviewItem,
    CurationRuntimeApplication,
    CurationWindow,
    ReviewQueueBoundary,
    SharedWriterAuthority,
)
from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.writer_jobs import (
    ApprovalBinding,
    JobRunDisposition,
    WriterJobError,
    run_writer_job,
)
from tests.unit.storage._factories import FixedClock


class MemoryReviewReader:
    def __init__(self, aggregates: tuple[ReviewAggregate, ...]) -> None:
        self._aggregates = {aggregate.proposal.review_id: aggregate for aggregate in aggregates}

    def get(self, review_id: ReviewId) -> ReviewAggregate | None:
        return self._aggregates.get(review_id)


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class MemoryReviewQueue(ReviewQueueBoundary):
    def __init__(self, *, fail_once_after_write: bool = False) -> None:
        self._items: dict[str, CurationReviewItem] = {}
        self.write_calls = 0
        self._fail_once_after_write = fail_once_after_write

    def write_if_absent(self, item: CurationReviewItem) -> PutResult:
        self.write_calls += 1
        existing = self._items.get(item.review_item_id)
        if existing is None:
            self._items[item.review_item_id] = item
            disposition = PutDisposition.CREATED
        elif existing.to_dict() == item.to_dict():
            disposition = PutDisposition.DUPLICATE
        else:
            raise AssertionError("conflicting review queue replay")
        if self._fail_once_after_write:
            self._fail_once_after_write = False
            raise RuntimeError("synthetic interruption after review queue")
        return PutResult(
            disposition=disposition,
            record_id=item.review_item_id,
            digest_sha256=item.digest_sha256(),
        )

    def get(self, review_item_id: str) -> CurationReviewItem | None:
        return self._items.get(review_item_id)


def _approved_review(
    suffix: str,
    *,
    privacy_tier: PrivacyTier = PrivacyTier.WORK,
) -> tuple[ReviewAggregate, ApprovedIntentRecord]:
    proposal = ReviewProposal.create(
        capture_id=CaptureId("cap_" + suffix * 64),
        source_ref=f"https://example.invalid/{suffix}",
        privacy_tier=privacy_tier,
        proposed_intent=Intent.ACTION_CANDIDATE,
        proposal_reason="Synthetic curation proposal",
        capture_why="Synthetic owner curation statement",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        created_by=Actor(kind=ActorKind.SYSTEM, label="synthetic-router"),
    )
    decided = ReviewAggregate.create(proposal).decide(
        ReviewDecisionCommand.create(
            decision_id=f"decision-{suffix}",
            target_state=ReviewState.APPLIED,
            reason="Synthetic owner approval",
            occurred_at=datetime(2026, 8, 13, 1, tzinfo=UTC),
            actor=Actor(kind=ActorKind.OWNER, label="owner"),
        )
    )
    assert decided.approved_record is not None
    return decided.aggregate, decided.approved_record


def _taxonomy(*, privacy_tier: PrivacyTier) -> LedgerTaxonomy:
    return LedgerTaxonomy.create(
        version="synthetic-v1",
        routes=(
            LedgerRoute.create(
                path_prefix=("professional", "research"),
                topic_id="research",
                topic_label="Research",
                privacy_tier=privacy_tier,
            ),
        ),
    )


def _record(*, approved: ApprovedIntentRecord) -> LedgerScanRecord:
    payload = {
        "text": "synthetic extracted text",
        "capture_why": approved.owner_statement,
        "capture_source": CaptureSource.CLI.value,
        "source_type": SourceType.WEB.value,
        "content_kind": ContentKind.ARTICLE.value,
        "provenance": {
            "source_ref": approved.source_ref,
            "content_origin": ContentOrigin.THIRD_PARTY.value,
            "owner_context": CaptureWhyOrigin.OWNER_AUTHORED.value,
        },
    }
    event = EventRecord.create(
        event_id="evt_" + approved.record_id[-16:],
        stream_id=str(approved.capture_id),
        event_type="capture.extracted",
        occurred_at=datetime(2026, 8, 13, tzinfo=UTC),
        privacy_decision=classify_privacy(approved.privacy_tier, policy_version="policy-v1"),
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256="b" * 64,
            output_digest_sha256=EventRecord.output_digest_sha256(payload),
            policy_version="redaction-v1",
        ),
    )
    item = DistillationWorkItem.create(
        capture_id=str(approved.capture_id),
        event_id=event.event_id,
        redacted_event_digest_sha256=sha256(event.canonical_bytes()).hexdigest(),
    )
    return scan_distillation_work_item(
        item=item,
        event=event,
        taxonomy=_taxonomy(privacy_tier=approved.privacy_tier),
        source_locator=PurePosixPath("professional/research/note"),
    )


def _stage(*, approved: ApprovedIntentRecord) -> LedgerStage:
    return stage_scan_record(
        record=_record(approved=approved),
        taxonomy=_taxonomy(privacy_tier=approved.privacy_tier),
    )


def _resolver(stage: LedgerStage) -> CaptureCitationResolver:
    citation_id = "cite-" + stage.binding.event_id
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
        item_id="synthetic-item",
        section=LedgerSection.SUMMARY,
        text="Synthetic finding",
    )
    assert sanitized.leaf is not None
    return service.prepare(stage=stage, section=LedgerSection.SUMMARY, leaf=sanitized.leaf)


def _ledger_service(
    *,
    private_root: Path,
    markdown_root: Path,
    approved: ApprovedIntentRecord,
    after_transition: Callable[[object], None] | None = None,
) -> tuple[LedgerService, SqliteLedgerStore]:
    stage = _stage(approved=approved)
    store = SqliteLedgerStore(root=private_root)
    sink = AtomicMarkdownSink(root=markdown_root)
    reader = AtomicMarkdownReader(root=markdown_root)
    service = LedgerService(
        store=store,
        sink=sink,
        reader=reader,
        citations=_resolver(stage),
        after_transition=after_transition,
    )
    return service, store


def _promotion(
    *,
    approved: ApprovedIntentRecord,
    aggregate: ReviewAggregate,
    private_root: Path,
    markdown_root: Path,
) -> tuple[CurationPromotion, SqliteLedgerStore]:
    stage = _stage(approved=approved)
    store = SqliteLedgerStore(root=private_root)
    markdown_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    sink = AtomicMarkdownSink(root=markdown_root)
    reader = AtomicMarkdownReader(root=markdown_root)
    service = LedgerService(
        store=store,
        sink=sink,
        reader=reader,
        citations=_resolver(stage),
    )
    prepared = _prepared(service, stage)
    promotion = CurationPromotion(
        record_id="curated_record",
        digest_sha256="a" * 64,
        approval=ApprovalBinding.from_record(approved),
        approved_record=approved,
        review=aggregate,
        stage=stage,
        prepared=prepared,
        applier=service,
        publication_store=store,
    )
    return promotion, store


def test_curation_runtime_promotes_approved_inputs_through_public_ledger_and_replays(
    tmp_path: Path,
) -> None:
    aggregate, approved = _approved_review("a")
    promotion, store = _promotion(
        approved=approved,
        aggregate=aggregate,
        private_root=tmp_path / "private-ledger",
        markdown_root=tmp_path / "markdown",
    )
    queue = MemoryReviewQueue()
    batch = CurationBatch(
        window=CurationWindow.PRIOR_DAY,
        promotions=(promotion,),
        review_items=(
            CurationReviewItem(
                review_item_id="review_followup",
                approval=ApprovalBinding.from_record(approved),
                approved_record=approved,
                review=aggregate,
                queue=queue,
            ),
        ),
    )
    capability = CurationEffectCapability(
        root=tmp_path,
        batch=batch,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )
    lease = RecordingLease()
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        first = run_writer_job(
            job_id="JOB-012",
            root=tmp_path,
            replay_key="curation-2026-08-16",
            approved_records=(approved,),
            review_reader=MemoryReviewReader((aggregate,)),
            journal=journal,
            application=CurationRuntimeApplication(batch),
            effect_capability=capability,
            lease=lease,
        )
        replay = run_writer_job(
            job_id="JOB-012",
            root=tmp_path,
            replay_key="curation-2026-08-16",
            approved_records=(approved,),
            review_reader=MemoryReviewReader((aggregate,)),
            journal=journal,
            application=CurationRuntimeApplication(batch),
            effect_capability=capability,
            lease=lease,
        )

    assert first.disposition is JobRunDisposition.APPLIED
    assert first.approved_inputs_applied == 1
    assert first.review_items_queued == 1
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert store.published_document_set(promotion.stage.stage_digest_sha256) is not None
    assert len(tuple((tmp_path / "markdown").rglob("*.md"))) == 2
    assert queue.write_calls == 1
    assert lease.scopes == [LockScope.SHARED_WRITER, LockScope.SHARED_WRITER]


def test_curation_runtime_rejects_unapproved_binding_before_io(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("a")
    other_aggregate, other_approved = _approved_review("b")
    promotion, store = _promotion(
        approved=other_approved,
        aggregate=other_aggregate,
        private_root=tmp_path / "private-ledger",
        markdown_root=tmp_path / "markdown",
    )
    batch = CurationBatch(
        window=CurationWindow.PRIOR_DAY,
        promotions=(
            CurationPromotion(
                record_id=promotion.record_id,
                digest_sha256=promotion.digest_sha256,
                approval=ApprovalBinding.from_record(other_approved),
                approved_record=other_approved,
                review=other_aggregate,
                stage=promotion.stage,
                prepared=promotion.prepared,
                applier=promotion.applier,
                publication_store=promotion.publication_store,
            ),
        ),
    )
    capability = CurationEffectCapability(
        root=tmp_path,
        batch=batch,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )

    with (
        SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal,
        pytest.raises(WriterJobError, match="unapproved approval binding"),
    ):
        run_writer_job(
            job_id="JOB-012",
            root=tmp_path,
            replay_key="curation-mismatch",
            approved_records=(approved,),
            review_reader=MemoryReviewReader((aggregate, other_aggregate)),
            journal=journal,
            application=CurationRuntimeApplication(batch),
            effect_capability=capability,
            lease=RecordingLease(),
        )

    assert store.published_document_set(promotion.stage.stage_digest_sha256) is None
    assert not tuple((tmp_path / "markdown").rglob("*.md"))


def test_curation_runtime_rejects_privacy_invalid_records_before_io(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("c", privacy_tier=PrivacyTier.PERSONAL)
    promotion, store = _promotion(
        approved=approved,
        aggregate=aggregate,
        private_root=tmp_path / "private-ledger",
        markdown_root=tmp_path / "markdown",
    )
    queue = MemoryReviewQueue()
    batch = CurationBatch(
        window=CurationWindow.PRIOR_DAY,
        promotions=(promotion,),
        review_items=(
            CurationReviewItem(
                review_item_id="review_followup",
                approval=ApprovalBinding.from_record(approved),
                approved_record=approved,
                review=aggregate,
                queue=queue,
            ),
        ),
    )
    capability = CurationEffectCapability(
        root=tmp_path,
        batch=batch,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )

    with (
        SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal,
        pytest.raises(WriterJobError, match="privacy-invalid approved intent record"),
    ):
        run_writer_job(
            job_id="JOB-012",
            root=tmp_path,
            replay_key="curation-personal",
            approved_records=(approved,),
            review_reader=MemoryReviewReader((aggregate,)),
            journal=journal,
            application=CurationRuntimeApplication(batch),
            effect_capability=capability,
            lease=RecordingLease(),
        )

    assert store.published_document_set(promotion.stage.stage_digest_sha256) is None
    assert queue.write_calls == 0
    assert not tuple((tmp_path / "markdown").rglob("*.md"))


def test_curation_runtime_recovers_reserved_receipt_after_interruption(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("a")
    promotion, store = _promotion(
        approved=approved,
        aggregate=aggregate,
        private_root=tmp_path / "private-ledger",
        markdown_root=tmp_path / "markdown",
    )
    queue = MemoryReviewQueue(fail_once_after_write=True)
    batch = CurationBatch(
        window=CurationWindow.PRIOR_DAY,
        promotions=(promotion,),
        review_items=(
            CurationReviewItem(
                review_item_id="review_followup",
                approval=ApprovalBinding.from_record(approved),
                approved_record=approved,
                review=aggregate,
                queue=queue,
            ),
        ),
    )
    capability = CurationEffectCapability(
        root=tmp_path,
        batch=batch,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )
    app = CurationRuntimeApplication(batch)
    lease = RecordingLease()
    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            run_writer_job(
                job_id="JOB-012",
                root=tmp_path,
                replay_key="curation-crash-retry",
                approved_records=(approved,),
                review_reader=MemoryReviewReader((aggregate,)),
                journal=journal,
                application=app,
                effect_capability=capability,
                lease=lease,
            )
        recovered = run_writer_job(
            job_id="JOB-012",
            root=tmp_path,
            replay_key="curation-crash-retry",
            approved_records=(approved,),
            review_reader=MemoryReviewReader((aggregate,)),
            journal=journal,
            application=app,
            effect_capability=capability,
            lease=lease,
        )

    assert recovered.disposition is JobRunDisposition.REPLAYED
    assert store.published_document_set(promotion.stage.stage_digest_sha256) is not None
    assert queue.write_calls == 1


def test_curation_runtime_rejects_non_shared_writer_authority(tmp_path: Path) -> None:
    aggregate, approved = _approved_review("a")
    promotion, _store = _promotion(
        approved=approved,
        aggregate=aggregate,
        private_root=tmp_path / "private-ledger",
        markdown_root=tmp_path / "markdown",
    )
    batch = CurationBatch(
        window=CurationWindow.PRIOR_DAY,
        promotions=(promotion,),
    )

    with pytest.raises(WriterJobError, match="shared writer authority mismatch"):
        CurationEffectCapability(
            root=tmp_path,
            batch=batch,
            authority=SharedWriterAuthority(LockScope.INDEX),
        )
