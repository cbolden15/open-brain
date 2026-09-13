from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from open_brain_engine.core.ids import CaptureId, ReviewId, canonical_json_bytes
from open_brain_engine.core.models import Intent, ValidationError
from open_brain_engine.core.ports import Clock, PutDisposition, PutResult
from open_brain_engine.review.models import (
    Actor,
    ApprovedIntentRecord,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewDecisionResult,
    ReviewProposal,
    capture_reference_for,
)

from .store import PendingReviewOutput, SqliteReviewStore


@dataclass(frozen=True, slots=True)
class OwnerAuthoredOutput:
    output_id: str
    review_id: ReviewId
    capture_id: CaptureId
    intent: Intent
    owner_statement: str
    capture_ref: str
    approved_by: Actor
    approved_at: datetime
    outbox_created_at: datetime

    def __post_init__(self) -> None:
        if self.capture_ref != capture_reference_for(self.capture_id):
            raise ValidationError("invalid owner output reference")

    def to_dict(self) -> dict[str, object]:
        return {
            "output_id": self.output_id,
            "review_id": str(self.review_id),
            "capture_id": str(self.capture_id),
            "intent": self.intent.value,
            "owner_statement": self.owner_statement,
            "capture_ref": self.capture_ref,
            "approved_by": self.approved_by.to_dict(),
            "approved_at": self.approved_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "outbox_created_at": self.outbox_created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


class OwnerAuthoredOutputSink(Protocol):
    def write_if_absent(self, output: OwnerAuthoredOutput) -> PutResult: ...


class ReviewApplicationService:
    def __init__(
        self,
        *,
        store: SqliteReviewStore,
        output_sink: OwnerAuthoredOutputSink,
        clock: Clock,
    ) -> None:
        self._store = store
        self._output_sink = output_sink
        self._clock = clock

    def create_review(self, proposal: ReviewProposal) -> PutResult:
        aggregate = ReviewAggregate.create(proposal)
        payload = canonical_json_bytes(aggregate.to_dict())
        return self._store.create_if_absent(aggregate, payload_digest=sha256(payload).hexdigest())

    def decide(self, review_id: ReviewId, command: ReviewDecisionCommand) -> ReviewDecisionResult:
        return self._store.decide(review_id, command)

    def deliver_pending(self, *, limit: int) -> tuple[str, ...]:
        delivered: list[str] = []
        for pending in self._store.pending_outputs(limit=limit):
            output = _owner_authored_output(pending)
            receipt = self._output_sink.write_if_absent(output)
            _verify_sink_receipt(receipt, output)
            self._store.mark_output_delivered(output.output_id, delivered_at=self._clock.now())
            delivered.append(output.output_id)
        return tuple(delivered)


def _owner_authored_output(pending: PendingReviewOutput) -> OwnerAuthoredOutput:
    record: ApprovedIntentRecord = pending.approved_record
    if record.intent not in {Intent.IDEA, Intent.ACTION_CANDIDATE}:
        raise ValidationError("invalid approved intent output")
    return OwnerAuthoredOutput(
        output_id=pending.output_id,
        review_id=record.review_id,
        capture_id=record.capture_id,
        intent=record.intent,
        owner_statement=record.owner_statement,
        capture_ref=record.source_ref,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
        outbox_created_at=pending.created_at,
    )


def _verify_sink_receipt(receipt: PutResult, output: OwnerAuthoredOutput) -> None:
    expected_digest = sha256(output.canonical_bytes()).hexdigest()
    if (
        not isinstance(receipt, PutResult)
        or receipt.disposition is not PutDisposition.CREATED
        and receipt.disposition is not PutDisposition.DUPLICATE
        or receipt.record_id != output.output_id
        or receipt.digest_sha256 != expected_digest
    ):
        raise ValidationError("invalid owner output receipt")
