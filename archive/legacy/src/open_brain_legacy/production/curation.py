"""Production assembly for approval-bound prior-day curation batches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.capture.models import DistillationWorkItem
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.ports import PutDisposition, PutResult
from open_brain_engine.events.store import SqliteEventStore
from open_brain_engine.review.models import ApprovedIntentRecord, ReviewAggregate
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    StorageError,
    WriteState,
    atomic_write_new,
    read_confined,
)
from open_brain_engine.storage.frontmatter import (
    AtomicMarkdownReader,
    AtomicMarkdownSink,
    markdown_relative_path,
)

from open_brain_legacy._compat.open_brain.config import AppConfig
from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.models import LedgerTaxonomy
from open_brain_legacy.ledger.sanitize import LedgerSection, sanitize_leaf
from open_brain_legacy.ledger.scan import scan_distillation_work_item
from open_brain_legacy.ledger.service import CaptureCitationResolver, LedgerService
from open_brain_legacy.ledger.stage import stage_scan_record
from open_brain_legacy.ledger.store import SqliteLedgerStore
from open_brain_legacy.operations.curation_runtime import (
    CurationBatch,
    CurationPromotion,
    CurationReviewItem,
    CurationWindow,
    ReviewQueueBoundary,
)
from open_brain_legacy.operations.writer_jobs import ApprovalBinding
from open_brain_legacy.review.store import SqliteReviewStore

_MAXIMUM_PENDING_OUTPUTS = 1_000


class ProductionCurationError(RuntimeError):
    """Durable curation inputs could not be bound without content residue."""


@dataclass(frozen=True, slots=True)
class ProductionCurationBatch:
    batch: CurationBatch
    approved_records: tuple[ApprovedIntentRecord, ...]
    output_ids: tuple[str, ...]


class FilesystemCurationReviewQueue(ReviewQueueBoundary):
    """Persist only approval and review digests for incomplete curation bindings."""

    def __init__(self, *, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ProductionCurationError("curation review queue unavailable")
        self._root = root
        self._expected: dict[str, CurationReviewItem] = {}

    def expect(self, item: CurationReviewItem) -> None:
        if not isinstance(item, CurationReviewItem):
            raise ProductionCurationError("invalid curation review item")
        existing = self._expected.get(item.review_item_id)
        if existing is not None and existing.to_dict() != item.to_dict():
            raise ProductionCurationError("curation review queue conflict")
        self._expected[item.review_item_id] = item

    def write_if_absent(self, item: CurationReviewItem) -> PutResult:
        self.expect(item)
        payload = item.canonical_bytes()
        relative = _review_item_path(item.review_item_id)
        try:
            state = atomic_write_new(root=self._root, relative=relative, data=payload)
            restored = read_confined(root=self._root, relative=relative)
        except (DuplicateConflictError, StorageError):
            raise ProductionCurationError("curation review queue conflict") from None
        if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS} or restored != payload:
            raise ProductionCurationError("curation review queue conflict")
        return PutResult(
            PutDisposition.CREATED
            if state is WriteState.CREATED
            else PutDisposition.DUPLICATE,
            item.review_item_id,
            item.digest_sha256(),
        )

    def get(self, review_item_id: str) -> CurationReviewItem | None:
        expected = self._expected.get(review_item_id)
        if expected is None:
            return None
        try:
            payload = read_confined(
                root=self._root,
                relative=_review_item_path(review_item_id),
            )
        except StorageError:
            raise ProductionCurationError("curation review queue conflict") from None
        if payload is None:
            return None
        if payload != expected.canonical_bytes():
            raise ProductionCurationError("curation review queue conflict")
        return expected


def build_production_curation_batch(
    *,
    config: AppConfig,
    taxonomy: LedgerTaxonomy,
    now: datetime,
    reviews: SqliteReviewStore,
    events: SqliteEventStore,
    ledger: SqliteLedgerStore,
) -> ProductionCurationBatch:
    """Build all due approved promotions without applying or deciding reviews."""
    current = _utc(now)
    if not isinstance(config, AppConfig) or not isinstance(taxonomy, LedgerTaxonomy):
        raise ProductionCurationError("invalid curation configuration")
    cutoff = (current - timedelta(days=1)).date()
    queue = FilesystemCurationReviewQueue(root=config.state_root)
    promotions: list[CurationPromotion] = []
    followups: list[CurationReviewItem] = []
    records: list[ApprovedIntentRecord] = []
    output_ids: list[str] = []
    try:
        pending_outputs = reviews.pending_outputs(limit=_MAXIMUM_PENDING_OUTPUTS)
        for pending in pending_outputs:
            approved = pending.approved_record
            if approved.approved_at.astimezone(UTC).date() > cutoff:
                continue
            aggregate = reviews.get(approved.review_id)
            if not isinstance(aggregate, ReviewAggregate):
                raise ProductionCurationError("curation review binding unavailable")
            target = reviews.get_curation_target(approved.review_id)
            matching_events = tuple(
                event
                for event in events.read(approved.capture_id)
                if event.event_type == "capture.extracted"
            )
            records.append(approved)
            output_ids.append(pending.output_id)
            if target is None or not matching_events:
                followups.append(_followup(approved, aggregate, queue))
                continue
            if len(matching_events) != 1:
                raise ProductionCurationError("curation event binding conflict")
            event = matching_events[0]
            route = taxonomy.route_for(target.page)
            if route is None or route.privacy_tier is not approved.privacy_tier:
                followups.append(_followup(approved, aggregate, queue))
                continue
            item = DistillationWorkItem.create(
                capture_id=str(approved.capture_id),
                event_id=event.event_id,
                redacted_event_digest_sha256=sha256(event.canonical_bytes()).hexdigest(),
            )
            record = scan_distillation_work_item(
                item=item,
                event=event,
                taxonomy=taxonomy,
                source_locator=target.page,
            )
            stage = stage_scan_record(record=record, taxonomy=taxonomy)
            sanitized = sanitize_leaf(
                item_id=approved.record_id,
                section=LedgerSection.SUMMARY,
                text=approved.owner_statement,
            )
            if sanitized.leaf is None:
                followups.append(_followup(approved, aggregate, queue))
                continue
            citation_id = "cite_" + sha256(
                canonical_json_bytes(
                    {
                        "capture_id": str(approved.capture_id),
                        "event_id": event.event_id,
                    }
                )
            ).hexdigest()
            resolver = CaptureCitationResolver(
                citations={
                    (str(approved.capture_id), event.event_id): TrustedCitation.create(
                        citation_id=citation_id,
                        destination=markdown_relative_path(
                            "capture_ref_" + citation_id
                        ).as_posix(),
                    )
                }
            )
            service = LedgerService(
                store=ledger,
                sink=AtomicMarkdownSink(root=config.work_root),
                reader=AtomicMarkdownReader(root=config.work_root),
                citations=resolver,
            )
            prepared = service.prepare(
                stage=stage,
                section=LedgerSection.SUMMARY,
                leaf=sanitized.leaf,
            )
            approval = ApprovalBinding.from_record(approved)
            promotions.append(
                CurationPromotion(
                    record_id=approved.record_id,
                    digest_sha256=sha256(
                        canonical_json_bytes(
                            {
                                "approval": approval.to_dict(),
                                "prepared_digest_sha256": prepared.document_digest_sha256,
                                "target_digest_sha256": target.digest_sha256(),
                            }
                        )
                    ).hexdigest(),
                    approval=approval,
                    approved_record=approved,
                    review=aggregate,
                    stage=stage,
                    prepared=prepared,
                    applier=service,
                    publication_store=ledger,
                )
            )
    except ProductionCurationError:
        raise
    except Exception:
        raise ProductionCurationError("curation batch assembly failed") from None
    for followup in followups:
        queue.expect(followup)
    return ProductionCurationBatch(
        batch=CurationBatch(
            window=CurationWindow.PRIOR_DAY,
            promotions=tuple(promotions),
            review_items=tuple(followups),
        ),
        approved_records=tuple(records),
        output_ids=tuple(output_ids),
    )


def _followup(
    approved: ApprovedIntentRecord,
    aggregate: ReviewAggregate,
    queue: FilesystemCurationReviewQueue,
) -> CurationReviewItem:
    approval = ApprovalBinding.from_record(approved)
    return CurationReviewItem(
        review_item_id="curation_followup_"
        + sha256(canonical_json_bytes(approval.to_dict())).hexdigest(),
        approval=approval,
        approved_record=approved,
        review=aggregate,
        queue=queue,
    )


def _review_item_path(review_item_id: str) -> PurePosixPath:
    digest = sha256(review_item_id.encode("utf-8")).hexdigest()
    return PurePosixPath("curation-review", digest[:2], digest + ".json")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProductionCurationError("invalid curation time")
    return value.astimezone(UTC)
