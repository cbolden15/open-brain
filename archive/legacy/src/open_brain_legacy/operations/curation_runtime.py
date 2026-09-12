from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Protocol

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.core.ports import PutDisposition, PutResult
from open_brain_engine.engine import LockScope
from open_brain_engine.review.models import ApprovedIntentRecord, ReviewAggregate, ReviewState
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    atomic_write_new,
    read_confined,
)

from open_brain_legacy.ledger.models import LedgerValidationError
from open_brain_legacy.ledger.service import ApplyResult, LedgerServiceError, PreparedLedgerApply
from open_brain_legacy.ledger.stage import LedgerStage
from open_brain_legacy.ledger.store import PublishedDocumentSet

from .writer_jobs import (
    ApprovalBinding,
    EffectCommand,
    EffectReceipt,
    EffectRecord,
    PreparedEffect,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
)

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "job_id",
        "replay_key",
        "request_digest_sha256",
        "effect",
        "effect_digest_sha256",
        "records",
        "review_item_ids",
        "approval_bindings",
    }
)
_APPROVAL_FIELDS = frozenset({"record_id", "review_id", "record_digest_sha256"})
_RECORD_FIELDS = frozenset({"record_id", "digest_sha256", "approval"})
_POINTER_FIELDS = frozenset({"version", "effect_digest_sha256"})
_MAX_RECEIPT_BYTES = 32 * 1024
_MAX_POINTER_BYTES = 4 * 1024
_ELIGIBLE_PRIVACY_TIERS = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK})


@dataclass(frozen=True, slots=True)
class SharedWriterAuthority:
    scope: LockScope

    def __post_init__(self) -> None:
        if self.scope is not LockScope.SHARED_WRITER:
            raise WriterJobError("shared writer authority mismatch")


class CurationWindow(StrEnum):
    PRIOR_DAY = "prior-day"


class LedgerApplyBoundary(Protocol):
    def apply(self, *, stage: LedgerStage, prepared: PreparedLedgerApply) -> ApplyResult: ...


class LedgerPublicationBoundary(Protocol):
    def published_document_set(self, stage_digest_sha256: str) -> PublishedDocumentSet | None: ...


class ReviewQueueBoundary(Protocol):
    def write_if_absent(self, item: CurationReviewItem) -> PutResult: ...

    def get(self, review_item_id: str) -> CurationReviewItem | None: ...


@dataclass(frozen=True, slots=True)
class CurationPromotion:
    record_id: str
    digest_sha256: str
    approval: ApprovalBinding
    approved_record: ApprovedIntentRecord
    review: ReviewAggregate
    stage: LedgerStage
    prepared: PreparedLedgerApply
    applier: LedgerApplyBoundary
    publication_store: LedgerPublicationBoundary

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_id, str)
            or _OPAQUE_ID.fullmatch(self.record_id) is None
            or not isinstance(self.digest_sha256, str)
            or _SHA256.fullmatch(self.digest_sha256) is None
            or not isinstance(self.approval, ApprovalBinding)
            or not isinstance(self.approved_record, ApprovedIntentRecord)
            or not isinstance(self.review, ReviewAggregate)
            or not isinstance(self.stage, LedgerStage)
            or not isinstance(self.prepared, PreparedLedgerApply)
        ):
            raise WriterJobError("invalid curation promotion")
        _validate_review_binding(
            approved_record=self.approved_record,
            review=self.review,
            approval=self.approval,
        )
        _validate_ledger_binding(
            approved_record=self.approved_record,
            stage=self.stage,
            prepared=self.prepared,
        )

    def to_effect_record(self) -> EffectRecord:
        return EffectRecord(
            record_id=self.record_id,
            digest_sha256=self.digest_sha256,
            approval=self.approval,
        )

    def apply(self) -> None:
        _validate_runtime_privacy(self.approved_record)
        result = self.applier.apply(stage=self.stage, prepared=self.prepared)
        if not isinstance(result, ApplyResult) or result.status != "applied":
            raise WriterJobError("curation ledger promotion failed")

    def is_applied(self) -> bool:
        _validate_runtime_privacy(self.approved_record)
        published = self.publication_store.published_document_set(self.stage.stage_digest_sha256)
        return _published_matches(published=published, prepared=self.prepared)


@dataclass(frozen=True, slots=True)
class CurationReviewItem:
    review_item_id: str
    approval: ApprovalBinding
    approved_record: ApprovedIntentRecord
    review: ReviewAggregate
    queue: ReviewQueueBoundary

    def __post_init__(self) -> None:
        if (
            not isinstance(self.review_item_id, str)
            or _OPAQUE_ID.fullmatch(self.review_item_id) is None
            or not isinstance(self.approval, ApprovalBinding)
            or not isinstance(self.approved_record, ApprovedIntentRecord)
            or not isinstance(self.review, ReviewAggregate)
        ):
            raise WriterJobError("invalid curation review item")
        _validate_review_binding(
            approved_record=self.approved_record,
            review=self.review,
            approval=self.approval,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "review_item_id": self.review_item_id,
            "approval": self.approval.to_dict(),
            "review_digest_sha256": _review_digest(self.review),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def digest_sha256(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()

    def queue_review(self) -> None:
        _validate_runtime_privacy(self.approved_record)
        receipt = self.queue.write_if_absent(self)
        if (
            not isinstance(receipt, PutResult)
            or receipt.disposition not in {PutDisposition.CREATED, PutDisposition.DUPLICATE}
            or receipt.record_id != self.review_item_id
            or receipt.digest_sha256 != self.digest_sha256()
        ):
            raise WriterJobError("curation review queue failed")

    def is_queued(self) -> bool:
        _validate_runtime_privacy(self.approved_record)
        queued = self.queue.get(self.review_item_id)
        return isinstance(queued, CurationReviewItem) and queued.to_dict() == self.to_dict()


@dataclass(frozen=True, slots=True)
class CurationBatch:
    window: CurationWindow
    promotions: tuple[CurationPromotion, ...] = ()
    review_items: tuple[CurationReviewItem, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.window, CurationWindow)
            or self.window is not CurationWindow.PRIOR_DAY
            or not isinstance(self.promotions, tuple)
            or any(not isinstance(item, CurationPromotion) for item in self.promotions)
            or len({item.record_id for item in self.promotions}) != len(self.promotions)
            or not isinstance(self.review_items, tuple)
            or any(not isinstance(item, CurationReviewItem) for item in self.review_items)
            or len({item.review_item_id for item in self.review_items})
            != len(self.review_items)
        ):
            raise WriterJobError("invalid curation batch")


@dataclass(frozen=True, slots=True)
class CurationRuntimeApplication:
    batch: CurationBatch

    def __post_init__(self) -> None:
        if not isinstance(self.batch, CurationBatch):
            raise WriterJobError("invalid curation runtime application")

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        if (
            not isinstance(invocation, WriterJobInvocation)
            or invocation.job_id != "JOB-012"
            or invocation.effect is not ScheduledEffect.CURATION_PROMOTION
            or invocation.review_boundary.value != "approved-inputs-only"
            or invocation.apply_review_decisions is not False
        ):
            raise WriterJobError("invalid curation runtime invocation")
        for promotion in self.batch.promotions:
            _require_bound_approval(
                approval=promotion.approval,
                allowed=invocation.approval_bindings,
            )
            _validate_runtime_privacy(promotion.approved_record)
        for review_item in self.batch.review_items:
            _require_bound_approval(
                approval=review_item.approval,
                allowed=invocation.approval_bindings,
            )
            _validate_runtime_privacy(review_item.approved_record)
        return _prepared_effect(self.batch)


class CurationEffectCapability:
    effect = ScheduledEffect.CURATION_PROMOTION
    local_only = True
    dry_run = False

    def __init__(
        self,
        *,
        root: Path,
        batch: CurationBatch,
        authority: SharedWriterAuthority,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not isinstance(batch, CurationBatch)
            or not isinstance(authority, SharedWriterAuthority)
        ):
            raise WriterJobError("invalid curation effect capability")
        self.root = root
        self._batch = batch
        self._authority = authority

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        reservation, _pointer = _paths(job_id, replay_key)
        payload = read_confined(root=self.root, relative=reservation)
        if payload is None:
            return None
        return _receipt_from_bytes(payload)

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        self._validate_command(command)
        receipt = EffectReceipt.from_command(command)
        reservation, _pointer = _paths(command.job_id, command.replay_key)
        try:
            atomic_write_new(root=self.root, relative=reservation, data=_receipt_bytes(receipt))
        except DuplicateConflictError:
            raise WriterJobError("curation effect reservation conflict") from None
        recovered = self.recover(command.job_id, command.replay_key)
        if recovered != receipt:
            raise WriterJobError("curation effect reservation conflict")
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self._validate_command(command)
        expected = EffectReceipt.from_command(command)
        if receipt != expected or self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("curation effect reservation conflict")
        for promotion in self._batch.promotions:
            if not promotion.is_applied():
                promotion.apply()
        for review_item in self._batch.review_items:
            if not review_item.is_queued():
                review_item.queue_review()
        _reservation, pointer = _paths(command.job_id, command.replay_key)
        try:
            atomic_write_new(
                root=self.root,
                relative=pointer,
                data=canonical_json_bytes(
                    {"version": 1, "effect_digest_sha256": receipt.effect_digest_sha256}
                ),
            )
        except DuplicateConflictError:
            raise WriterJobError("curation applied pointer conflict") from None

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        if not isinstance(receipt, EffectReceipt) or receipt.effect is not self.effect:
            raise WriterJobError("invalid curation effect receipt")
        _reservation, pointer = _paths(receipt.job_id, receipt.replay_key)
        payload = read_confined(root=self.root, relative=pointer)
        if payload is None:
            return None
        effect_digest = _pointer_from_bytes(payload)
        if effect_digest != receipt.effect_digest_sha256:
            raise WriterJobError("curation applied pointer conflict")
        for promotion in self._batch.promotions:
            if not promotion.is_applied():
                raise WriterJobError("curation durable read-back conflict")
        for review_item in self._batch.review_items:
            if not review_item.is_queued():
                raise WriterJobError("curation durable read-back conflict")
        return _prepared_effect(self._batch)

    def _validate_command(self, command: EffectCommand) -> None:
        if (
            not isinstance(command, EffectCommand)
            or command.job_id != "JOB-012"
            or command.prepared != _prepared_effect(self._batch)
        ):
            raise WriterJobError("invalid curation effect command")


def _prepared_effect(batch: CurationBatch) -> PreparedEffect:
    return PreparedEffect(
        effect=ScheduledEffect.CURATION_PROMOTION,
        records=tuple(item.to_effect_record() for item in batch.promotions),
        review_item_ids=tuple(item.review_item_id for item in batch.review_items),
    )


def _require_bound_approval(
    *,
    approval: ApprovalBinding,
    allowed: tuple[ApprovalBinding, ...],
) -> None:
    if approval not in allowed:
        raise WriterJobError("unapproved approval binding")


def _validate_runtime_privacy(record: ApprovedIntentRecord) -> None:
    if record.privacy_tier not in _ELIGIBLE_PRIVACY_TIERS:
        raise WriterJobError("privacy-invalid approved intent record")


def _validate_review_binding(
    *,
    approved_record: ApprovedIntentRecord,
    review: ReviewAggregate,
    approval: ApprovalBinding,
) -> None:
    if (
        review.proposal.state is not ReviewState.APPLIED
        or review.approved_record != approved_record
        or ApprovalBinding.from_record(approved_record) != approval
        or review.proposal.review_id != approved_record.review_id
        or review.proposal.capture_id != approved_record.capture_id
        or review.proposal.proposed_intent is not approved_record.intent
        or review.proposal.capture_why != approved_record.owner_statement
        or review.proposal.privacy_tier is not approved_record.privacy_tier
    ):
        raise WriterJobError("invalid curation approval binding")


def _validate_ledger_binding(
    *,
    approved_record: ApprovedIntentRecord,
    stage: LedgerStage,
    prepared: PreparedLedgerApply,
) -> None:
    try:
        stage.validate()
        prepared.validate_for(stage)
    except (LedgerServiceError, LedgerValidationError):
        raise WriterJobError("invalid curation ledger binding") from None
    route = stage.binding.route
    if (
        stage.binding.capture_id != approved_record.capture_id
        or stage.binding.capture_why != approved_record.owner_statement
        or route is None
        or route.privacy_tier is not approved_record.privacy_tier
    ):
        raise WriterJobError("invalid curation ledger binding")


def _review_digest(review: ReviewAggregate) -> str:
    return sha256(canonical_json_bytes(review.to_dict())).hexdigest()


def _published_matches(
    *,
    published: PublishedDocumentSet | None,
    prepared: PreparedLedgerApply,
) -> bool:
    return (
        isinstance(published, PublishedDocumentSet)
        and published.document_ids == prepared.document_ids
        and len(published.sink_digests) == len(prepared.document_ids)
    )


def _paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    identity = sha256(
        canonical_json_bytes({"job_id": job_id, "replay_key": replay_key})
    ).hexdigest()
    base = PurePosixPath("operations/effects/curation") / identity
    return base.with_suffix(".json"), base.with_suffix(".applied.json")


def _receipt_bytes(receipt: EffectReceipt) -> bytes:
    return canonical_json_bytes(
        {
            "version": 1,
            "job_id": receipt.job_id,
            "replay_key": receipt.replay_key,
            "request_digest_sha256": receipt.request_digest_sha256,
            "effect": receipt.effect.value,
            "effect_digest_sha256": receipt.effect_digest_sha256,
            "records": [record.to_dict() for record in receipt.records],
            "review_item_ids": list(receipt.review_item_ids),
            "approval_bindings": [binding.to_dict() for binding in receipt.approval_bindings],
        }
    )


def _receipt_from_bytes(payload: bytes) -> EffectReceipt:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_RECEIPT_BYTES:
        raise WriterJobError("invalid curation effect receipt")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != _RECEIPT_FIELDS or value["version"] != 1:
            raise WriterJobError("invalid curation effect receipt")
        raw_records = value["records"]
        raw_review_item_ids = value["review_item_ids"]
        raw_bindings = value["approval_bindings"]
        if (
            type(raw_records) is not list
            or type(raw_review_item_ids) is not list
            or type(raw_bindings) is not list
        ):
            raise WriterJobError("invalid curation effect receipt")
        records: list[EffectRecord] = []
        for raw_record in raw_records:
            if type(raw_record) is not dict or frozenset(raw_record) != _RECORD_FIELDS:
                raise WriterJobError("invalid curation effect receipt")
            approval = raw_record["approval"]
            if type(approval) is not dict or frozenset(approval) != _APPROVAL_FIELDS:
                raise WriterJobError("invalid curation effect receipt")
            records.append(
                EffectRecord(
                    record_id=raw_record["record_id"],
                    digest_sha256=raw_record["digest_sha256"],
                    approval=ApprovalBinding(
                        record_id=approval["record_id"],
                        review_id=approval["review_id"],
                        record_digest_sha256=approval["record_digest_sha256"],
                    ),
                )
            )
        approval_bindings = tuple(
            ApprovalBinding(
                record_id=binding["record_id"],
                review_id=binding["review_id"],
                record_digest_sha256=binding["record_digest_sha256"],
            )
            for binding in raw_bindings
        )
        receipt = EffectReceipt(
            job_id=value["job_id"],
            replay_key=value["replay_key"],
            request_digest_sha256=value["request_digest_sha256"],
            effect=ScheduledEffect(value["effect"]),
            effect_digest_sha256=value["effect_digest_sha256"],
            records=tuple(records),
            review_item_ids=tuple(raw_review_item_ids),
            approval_bindings=approval_bindings,
        )
    except WriterJobError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid curation effect receipt") from None
    if (
        receipt.effect is not ScheduledEffect.CURATION_PROMOTION
        or _receipt_bytes(receipt) != payload
    ):
        raise WriterJobError("invalid curation effect receipt")
    return receipt


def _pointer_from_bytes(payload: bytes) -> str:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_POINTER_BYTES:
        raise WriterJobError("invalid curation applied pointer")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(value) is not dict or frozenset(value) != _POINTER_FIELDS or value["version"] != 1:
            raise WriterJobError("invalid curation applied pointer")
        effect_digest = value["effect_digest_sha256"]
        if not isinstance(effect_digest, str):
            raise WriterJobError("invalid curation applied pointer")
    except WriterJobError:
        raise
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid curation applied pointer") from None
    if canonical_json_bytes(value) != payload:
        raise WriterJobError("invalid curation applied pointer")
    return effect_digest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value
