from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from open_brain_engine.core.ids import ReviewId, canonical_json_bytes
from open_brain_engine.core.models import CaptureEnvelope, Intent, PrivacyTier, ValidationError
from open_brain_engine.core.policy import IntentPolicyReason, route_intent
from open_brain_engine.core.ports import Clock, PutDisposition, PutResult
from open_brain_engine.review.models import Actor, ActorKind, ReviewAggregate, ReviewProposal

from open_brain_legacy.ledger.models import LedgerValidationError
from open_brain_legacy.ledger.service import (
    ApplyResult,
    LedgerServiceError,
    PreparedLedgerApply,
)
from open_brain_legacy.ledger.stage import LedgerStage


class IntentRoutingError(ValidationError):
    """A capture cannot cross the selected Phase 4 intent boundary."""


class IntentRoutingStatus(StrEnum):
    HELD = "held"
    REFERENCE_APPLIED = "reference_applied"
    REVIEW_OPEN = "review_open"


class IntentRoutingDestination(StrEnum):
    HOLD = "hold"
    WORK = "work"
    PERSONAL = "personal"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class IntentRoutingResult:
    intent: Intent
    reason: IntentPolicyReason
    status: IntentRoutingStatus
    destination: IntentRoutingDestination
    review_id: ReviewId | None


class LedgerApplyBoundary(Protocol):
    def apply(self, *, stage: LedgerStage, prepared: PreparedLedgerApply) -> ApplyResult: ...


class ReviewCreationBoundary(Protocol):
    def create_review(self, proposal: ReviewProposal) -> PutResult: ...


class Phase4IntentRouter:
    def __init__(
        self,
        *,
        ledger: LedgerApplyBoundary,
        reviews: ReviewCreationBoundary,
        clock: Clock,
    ) -> None:
        self._ledger = ledger
        self._reviews = reviews
        self._clock = clock

    def route(
        self,
        *,
        capture: CaptureEnvelope,
        proposed_intent: str | None,
        proposal_reason: str,
        stage: LedgerStage | None = None,
        prepared: PreparedLedgerApply | None = None,
    ) -> IntentRoutingResult:
        if not isinstance(capture, CaptureEnvelope):
            raise IntentRoutingError("invalid capture intent input")
        route = route_intent(
            capture,
            proposed_intent,
            proposal_reason,
            now=self._clock.now(),
        )
        if route.intent is Intent.HOLD:
            if route.review_proposal is not None:
                raise IntentRoutingError("invalid hold route")
            return IntentRoutingResult(
                intent=Intent.HOLD,
                reason=route.reason,
                status=IntentRoutingStatus.HELD,
                destination=IntentRoutingDestination.HOLD,
                review_id=None,
            )
        if route.intent is Intent.REFERENCE:
            bound_stage, bound_prepared = self._validate_ledger_binding(
                capture=capture,
                stage=stage,
                prepared=prepared,
            )
            bound_route = bound_stage.binding.route
            if bound_route is None:
                raise IntentRoutingError("reference destination unavailable")
            route_tier = bound_route.privacy_tier
            if route_tier is PrivacyTier.WORK:
                destination = IntentRoutingDestination.WORK
            elif route_tier is PrivacyTier.PERSONAL:
                destination = IntentRoutingDestination.PERSONAL
            else:
                raise IntentRoutingError("reference destination unavailable")
            applied = self._ledger.apply(stage=bound_stage, prepared=bound_prepared)
            if not isinstance(applied, ApplyResult) or applied.status != "applied":
                raise IntentRoutingError("reference apply failed")
            return IntentRoutingResult(
                intent=Intent.REFERENCE,
                reason=route.reason,
                status=IntentRoutingStatus.REFERENCE_APPLIED,
                destination=destination,
                review_id=None,
            )
        if route.intent in {Intent.IDEA, Intent.ACTION_CANDIDATE}:
            draft = route.review_proposal
            if draft is None:
                raise IntentRoutingError("review route unavailable")
            proposal = ReviewProposal.create(
                review_id=draft.review_id,
                capture_id=draft.capture_id,
                source_ref=draft.source_ref,
                privacy_tier=draft.privacy_tier,
                proposed_intent=draft.proposed_intent,
                proposal_reason=draft.proposal_reason,
                capture_why=draft.capture_why,
                created_at=draft.created_at,
                created_by=Actor(kind=ActorKind.SYSTEM, label="intent-router"),
            )
            aggregate = ReviewAggregate.create(proposal)
            expected_digest = sha256(canonical_json_bytes(aggregate.to_dict())).hexdigest()
            receipt = self._reviews.create_review(proposal)
            if (
                not isinstance(receipt, PutResult)
                or receipt.disposition is not PutDisposition.CREATED
                and receipt.disposition is not PutDisposition.DUPLICATE
                or receipt.record_id != str(proposal.review_id)
                or receipt.digest_sha256 != expected_digest
            ):
                raise IntentRoutingError("review persistence failed")
            return IntentRoutingResult(
                intent=route.intent,
                reason=route.reason,
                status=IntentRoutingStatus.REVIEW_OPEN,
                destination=IntentRoutingDestination.REVIEW,
                review_id=proposal.review_id,
            )
        raise IntentRoutingError("unsupported intent route")

    @staticmethod
    def _validate_ledger_binding(
        *,
        capture: CaptureEnvelope,
        stage: LedgerStage | None,
        prepared: PreparedLedgerApply | None,
    ) -> tuple[LedgerStage, PreparedLedgerApply]:
        if not isinstance(stage, LedgerStage) or not isinstance(prepared, PreparedLedgerApply):
            raise IntentRoutingError("capture ledger binding mismatch")
        try:
            stage.validate()
            prepared.validate_for(stage)
        except (LedgerValidationError, LedgerServiceError):
            raise IntentRoutingError("capture ledger binding mismatch") from None
        binding = stage.binding
        if (
            binding.capture_id != capture.capture_id
            or binding.event_privacy_decision != capture.privacy_decision
            or binding.capture_why != capture.capture_why
            or binding.captured_at != capture.captured_at
            or binding.capture_source is not capture.capture_source
            or binding.source_type is not capture.source_type
            or binding.content_kind is not capture.content_kind
            or binding.provenance != capture.provenance
        ):
            raise IntentRoutingError("capture ledger binding mismatch")
        return stage, prepared
