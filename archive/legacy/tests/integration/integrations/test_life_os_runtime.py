from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import pytest
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
from open_brain_engine.storage.filesystem import (
    DurabilityError,
    RootConfinementError,
    atomic_replace,
    read_confined,
)

from open_brain_legacy.integrations.life_os import (
    CalendarBlockRequest,
    CalendarBlockWriteDisposition,
    CalendarWriteApproval,
    InMemoryCalendarBlockWriter,
    LifePlanDisposition,
    LifePlanRequest,
    LifeResetDisposition,
    LifeResetRequest,
    ReviewGatedActionCandidate,
)
from open_brain_legacy.integrations.life_os_runtime import (
    LifeOSPlanningRuntime,
    LifeOSRuntimeDisposition,
    LifeOSRuntimeOperation,
    LifeOSRuntimeStateError,
)

_STATE_RELATIVE = PurePosixPath("runtime/life_os/planning-runtime.json")


def _request(*, day: int, suffix: str = "fixture") -> LifePlanRequest:
    return LifePlanRequest(
        plan_date=date(2026, 8, day),
        action_candidates=(
            ReviewGatedActionCandidate(
                candidate_id=f"candidate_{suffix}",
                review_id=f"review_{suffix}",
            ),
        ),
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


def test_midday_plan_and_reset_stage_public_safe_date_keyed_runtime_commands(
    tmp_path: Path,
) -> None:
    runtime = LifeOSPlanningRuntime.bind(root=tmp_path)
    request = _request(day=14)
    reset = LifeResetRequest(plan_date=request.plan_date)

    midday = runtime.midday(request)
    planned = runtime.plan(request)
    reset_result = runtime.reset(reset)

    assert midday.disposition is LifeOSRuntimeDisposition.STAGED
    assert planned.disposition is LifeOSRuntimeDisposition.STAGED
    assert reset_result.disposition is LifeOSRuntimeDisposition.STAGED
    assert midday.operation is LifeOSRuntimeOperation.MIDDAY
    assert planned.operation is LifeOSRuntimeOperation.PLAN
    assert reset_result.operation is LifeOSRuntimeOperation.RESET
    assert midday.command == (
        "open-brain",
        "lifeos",
        "nudge",
        "midday",
        "--date=2026-08-14",
        "--json",
    )
    assert planned.command == (
        "open-brain",
        "lifeos",
        "plan",
        "--date=2026-08-14",
        "--generic-titles",
        "--json",
    )
    assert reset_result.command == (
        "open-brain",
        "lifeos",
        "reset",
        "--date=2026-08-14",
        "--json",
    )
    for public_output in (repr(midday), repr(planned), repr(reset_result)):
        assert "candidate_fixture" not in public_output
        assert "review_fixture" not in public_output

    assert (
        runtime.load(operation=LifeOSRuntimeOperation.MIDDAY, plan_date=request.plan_date)
        == request
    )
    assert (
        runtime.load(operation=LifeOSRuntimeOperation.PLAN, plan_date=request.plan_date)
        == request
    )
    assert (
        runtime.load(operation=LifeOSRuntimeOperation.RESET, plan_date=request.plan_date)
        == reset
    )


def test_duplicate_replay_is_deterministic_for_all_three_operations(tmp_path: Path) -> None:
    runtime = LifeOSPlanningRuntime.bind(root=tmp_path)
    request = _request(day=15, suffix="a")
    reset = LifeResetRequest(plan_date=request.plan_date)

    first_midday = runtime.midday(request)
    replayed_midday = runtime.midday(request)
    first_plan = runtime.plan(request)
    replayed_plan = runtime.plan(request)
    first_reset = runtime.reset(reset)
    replayed_reset = runtime.reset(reset)

    assert first_midday.disposition is LifeOSRuntimeDisposition.STAGED
    assert replayed_midday.disposition is LifeOSRuntimeDisposition.DUPLICATE
    assert replayed_midday.state_ref == first_midday.state_ref
    assert first_plan.disposition is LifeOSRuntimeDisposition.STAGED
    assert replayed_plan.disposition is LifeOSRuntimeDisposition.DUPLICATE
    assert replayed_plan.state_ref == first_plan.state_ref
    assert first_reset.disposition is LifeOSRuntimeDisposition.STAGED
    assert replayed_reset.disposition is LifeOSRuntimeDisposition.DUPLICATE
    assert replayed_reset.state_ref == first_reset.state_ref

    with pytest.raises(ValueError, match="conflicting LifeOS runtime plan replay"):
        runtime.plan(_request(day=15, suffix="b"))


def test_runtime_executes_live_plan_and_reset_contracts_with_durable_store(
    tmp_path: Path,
) -> None:
    runtime = LifeOSPlanningRuntime.bind(root=tmp_path)
    request = _request(day=20, suffix="store")

    first = runtime.execute_plan(request)
    replay = runtime.execute_plan(request)

    assert first.disposition is LifePlanDisposition.PLANNED
    assert replay.disposition is LifePlanDisposition.DUPLICATE
    assert first.plan == replay.plan
    assert first.plan is not None
    assert first.plan.plan_date == request.plan_date
    assert first.plan.action_candidates == request.action_candidates
    assert runtime.store.get(first.plan.plan_key) == first.plan

    first_reset = runtime.execute_reset(LifeResetRequest(plan_date=request.plan_date))
    replayed_reset = runtime.execute_reset(LifeResetRequest(plan_date=request.plan_date))

    assert first_reset.plan_key == first.plan.plan_key
    assert first_reset.disposition is LifeResetDisposition.RESET
    assert replayed_reset.disposition is LifeResetDisposition.DUPLICATE
    assert runtime.store.get(first.plan.plan_key) is None


def test_calendar_writes_stay_disabled_without_explicit_writer_and_exact_approval(
    tmp_path: Path,
) -> None:
    approved_record = _approved_action(suffix="a")
    candidate = ReviewGatedActionCandidate(
        candidate_id=approved_record.record_id,
        review_id=str(approved_record.review_id),
    )
    runtime = LifeOSPlanningRuntime.bind(root=tmp_path)
    planned = runtime.execute_plan(
        LifePlanRequest(plan_date=date(2026, 8, 21), action_candidates=(candidate,))
    )
    assert planned.plan is not None
    request = CalendarBlockRequest(
        block_key="calendar_block_fixture",
        plan_key=planned.plan.plan_key,
        action_candidate=candidate,
    )
    writer = InMemoryCalendarBlockWriter()

    disabled_without_writer = runtime.write_calendar(
        request,
        approval=CalendarWriteApproval.from_record(
            plan_key=planned.plan.plan_key,
            record=approved_record,
        ),
    )
    disabled_without_approval = runtime.write_calendar(request, writer=writer)
    blocked_with_mismatched_approval = runtime.write_calendar(
        request,
        writer=writer,
        approval=CalendarWriteApproval.from_record(
            plan_key=planned.plan.plan_key,
            record=_approved_action(suffix="b"),
        ),
    )
    first_write = runtime.write_calendar(
        request,
        writer=writer,
        approval=CalendarWriteApproval.from_record(
            plan_key=planned.plan.plan_key,
            record=approved_record,
        ),
    )
    replayed_write = runtime.write_calendar(
        request,
        writer=writer,
        approval=CalendarWriteApproval.from_record(
            plan_key=planned.plan.plan_key,
            record=approved_record,
        ),
    )

    assert disabled_without_writer.disposition is CalendarBlockWriteDisposition.DISABLED
    assert disabled_without_approval.disposition is CalendarBlockWriteDisposition.DISABLED
    assert blocked_with_mismatched_approval.disposition is CalendarBlockWriteDisposition.BLOCKED
    assert first_write.disposition is CalendarBlockWriteDisposition.WRITTEN
    assert replayed_write.disposition is CalendarBlockWriteDisposition.DUPLICATE
    assert writer.requests == (request,)


@pytest.mark.parametrize(
    "payload",
    (
        b"{not-json",
        (
            b'{"midday":{},"plan":{},"reset":{},"schema_version":1,'
            b'"unexpected":true}'
        ),
    ),
)
def test_malformed_or_corrupt_state_is_rejected(tmp_path: Path, payload: bytes) -> None:
    runtime = LifeOSPlanningRuntime.bind(root=tmp_path)
    atomic_replace(root=tmp_path, relative=_STATE_RELATIVE, data=payload, require_existing=False)

    with pytest.raises(LifeOSRuntimeStateError, match="invalid LifeOS runtime state"):
        runtime.plan(_request(day=16))


def test_symlink_roots_and_state_paths_are_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    symlink_root = tmp_path.parent / f"{tmp_path.name}-link"
    symlink_root.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(RootConfinementError, match="unsafe storage root"):
        LifeOSPlanningRuntime.bind(root=symlink_root)

    runtime = LifeOSPlanningRuntime.bind(root=tmp_path)
    (tmp_path / "runtime").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RootConfinementError, match="unsafe storage path"):
        runtime.midday(_request(day=17))

    assert not tuple(outside.iterdir())


def test_failed_atomic_replace_leaves_previous_runtime_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = LifeOSPlanningRuntime.bind(root=tmp_path)
    original_request = _request(day=18, suffix="original")
    runtime.midday(original_request)
    original_bytes = read_confined(root=tmp_path, relative=_STATE_RELATIVE)
    assert original_bytes is not None

    def fail_replace(
        *,
        root: Path,
        relative: PurePosixPath,
        data: bytes,
        require_existing: bool | None = None,
    ) -> None:
        del root, relative, data, require_existing
        raise DurabilityError("durable storage write failed")

    monkeypatch.setattr(
        "open_brain_legacy.integrations.life_os_runtime.atomic_replace", fail_replace
    )

    with pytest.raises(DurabilityError, match="durable storage write failed"):
        runtime.plan(_request(day=19, suffix="new"))

    assert read_confined(root=tmp_path, relative=_STATE_RELATIVE) == original_bytes
    assert (
        runtime.load(
            operation=LifeOSRuntimeOperation.MIDDAY,
            plan_date=original_request.plan_date,
        )
        == original_request
    )
    assert runtime.load(operation=LifeOSRuntimeOperation.PLAN, plan_date=date(2026, 8, 19)) is None
