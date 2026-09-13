from __future__ import annotations

from datetime import UTC, date, datetime

from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ApprovedIntentRecord,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
)

from open_brain_legacy._compat.open_brain.integrations import Capability, IntegrationConfig
from open_brain_legacy.integrations.life_os import (
    CalendarBlockPort,
    CalendarBlockRequest,
    CalendarBlockWriteDisposition,
    CalendarWriteApproval,
    InMemoryCalendarBlockWriter,
    InMemoryLifePlanStore,
    LifeOSIntegration,
    LifePlanDisposition,
    LifePlanPort,
    LifePlanRequest,
    LifeResetDisposition,
    LifeResetRequest,
    ReviewGatedActionCandidate,
)


def _approved_action(*, suffix: str) -> ApprovedIntentRecord:
    aggregate = ReviewAggregate.create(
        ReviewProposal.create(
            capture_id="cap_" + suffix * 64,
            source_ref="https://example.test/synthetic-source",
            privacy_tier=PrivacyTier.PERSONAL,
            proposed_intent=Intent.ACTION_CANDIDATE,
            proposal_reason="Synthetic candidate",
            capture_why="Owner approved synthetic calendar action",
            created_at=datetime(2026, 8, 14, tzinfo=UTC),
            created_by=Actor(ActorKind.SYSTEM, "fixture"),
        )
    )
    result = aggregate.decide(
        ReviewDecisionCommand.create(
            decision_id=f"decision-{suffix}",
            target_state=ReviewState.APPLIED,
            reason="Owner approved synthetic calendar action",
            occurred_at=datetime(2026, 8, 14, 1, tzinfo=UTC),
            actor=Actor(ActorKind.OWNER, "fixture-owner"),
        )
    )
    assert result.approved_record is not None
    return result.approved_record


def test_plan_is_disabled_by_default_and_replay_only_returns_review_candidates() -> None:
    store = InMemoryLifePlanStore()
    candidate = ReviewGatedActionCandidate(
        candidate_id="candidate_fixture",
        review_id="review_fixture",
    )
    request = LifePlanRequest(
        plan_date=date(2026, 8, 14),
        action_candidates=(candidate,),
    )

    disabled = LifeOSIntegration(store=store).plan(request)

    assert disabled.disposition is LifePlanDisposition.DISABLED
    assert store.plans == ()

    integration: LifePlanPort = LifeOSIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.LIFE_OS})),
        store=store,
    )
    first = integration.plan(request)
    replay = integration.plan(request)

    assert first.disposition is LifePlanDisposition.PLANNED
    assert replay.disposition is LifePlanDisposition.DUPLICATE
    assert replay.plan == first.plan
    assert first.plan is not None
    assert len(store.plans) == 1
    assert store.plans[0] == first.plan
    assert first.plan.action_candidates == (candidate,)
    assert candidate.requires_review
    assert not hasattr(first.plan, "task_id")
    assert not hasattr(integration, "calendar_writer")


def test_reset_replay_and_calendar_write_require_explicit_injection_and_authority() -> None:
    store = InMemoryLifePlanStore()
    approved_record = _approved_action(suffix="c")
    candidate = ReviewGatedActionCandidate(
        candidate_id=approved_record.record_id,
        review_id=str(approved_record.review_id),
    )
    plan_request = LifePlanRequest(
        plan_date=date(2026, 8, 15),
        action_candidates=(candidate,),
    )
    disabled_reset = LifeOSIntegration(store=store).reset(
        LifeResetRequest(plan_date=plan_request.plan_date)
    )

    assert disabled_reset.disposition is LifeResetDisposition.DISABLED
    assert store.plans == ()

    integration = LifeOSIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.LIFE_OS})),
        store=store,
    )
    planned = integration.plan(plan_request)
    assert planned.plan is not None
    calendar_request = CalendarBlockRequest(
        block_key="calendar_block_fixture",
        plan_key=planned.plan.plan_key,
        action_candidate=candidate,
    )
    calendar_writer = InMemoryCalendarBlockWriter()
    calendar_port: CalendarBlockPort = calendar_writer

    blocked = integration.write_calendar(calendar_request, writer=calendar_port)

    assert blocked.disposition is CalendarBlockWriteDisposition.DISABLED
    assert calendar_writer.requests == ()

    authorized = LifeOSIntegration(
        config=IntegrationConfig(
            live_adapters=frozenset({Capability.LIFE_OS}),
            lifeos_external_writes_enabled=True,
        ),
        store=store,
    )
    missing_approval = authorized.write_calendar(calendar_request, writer=calendar_port)
    other_record = _approved_action(suffix="d")
    mismatched_approval = authorized.write_calendar(
        calendar_request,
        writer=calendar_port,
        approval=CalendarWriteApproval.from_record(
            plan_key=planned.plan.plan_key,
            record=other_record,
        ),
    )
    approval = CalendarWriteApproval.from_record(
        plan_key=planned.plan.plan_key,
        record=approved_record,
    )
    first_write = authorized.write_calendar(
        calendar_request,
        writer=calendar_port,
        approval=approval,
    )
    replayed_write = authorized.write_calendar(
        calendar_request,
        writer=calendar_port,
        approval=approval,
    )

    assert missing_approval.disposition is CalendarBlockWriteDisposition.BLOCKED
    assert mismatched_approval.disposition is CalendarBlockWriteDisposition.BLOCKED
    assert first_write.disposition is CalendarBlockWriteDisposition.WRITTEN
    assert replayed_write.disposition is CalendarBlockWriteDisposition.DUPLICATE
    assert len(calendar_writer.requests) == 1
    assert calendar_writer.requests[0] == calendar_request

    first_reset = integration.reset(LifeResetRequest(plan_date=plan_request.plan_date))
    replayed_reset = integration.reset(LifeResetRequest(plan_date=plan_request.plan_date))

    assert first_reset.disposition is LifeResetDisposition.RESET
    assert replayed_reset.disposition is LifeResetDisposition.DUPLICATE
    assert store.plans == ()
    assert len(calendar_writer.requests) == 1
    assert calendar_writer.requests[0] == calendar_request
