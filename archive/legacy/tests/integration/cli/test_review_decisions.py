from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from open_brain_engine.core.ids import ReviewId
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewDecisionResult,
    ReviewProposal,
    ReviewState,
)

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.main import main
from open_brain_legacy.cli.review import ReviewCommandAdapter, decide_review
from open_brain_legacy.review.maintenance import (
    ArchivedReview,
    ArchiveResult,
    CurationClass,
    CurationTarget,
    CurationTaxonomy,
    ReviewTargetEdit,
)

FIXED_TIME = datetime(2026, 8, 14, 12, tzinfo=UTC)


class DecisionServiceFake:
    def __init__(self, aggregate: ReviewAggregate) -> None:
        self.aggregate = aggregate
        self.calls: list[tuple[ReviewId, ReviewDecisionCommand]] = []

    def decide(
        self, review_id: ReviewId, command: ReviewDecisionCommand
    ) -> ReviewDecisionResult:
        self.calls.append((review_id, command))
        result = self.aggregate.decide(command)
        self.aggregate = result.aggregate
        return result


def _review() -> ReviewAggregate:
    return ReviewAggregate.create(
        ReviewProposal.create(
            capture_id="cap_" + "c" * 64,
            source_ref="https://example.test/synthetic-source",
            privacy_tier=PrivacyTier.WORK,
            proposed_intent=Intent.ACTION_CANDIDATE,
            proposal_reason="Synthetic third-party proposal",
            capture_why="Synthetic owner-authored approval statement",
            created_at=FIXED_TIME,
            created_by=Actor(ActorKind.SYSTEM, "fixture"),
        )
    )


def test_review_decision_is_dry_run_aware_redacted_and_idempotent() -> None:
    review = _review()
    service = DecisionServiceFake(review)

    planned = decide_review(
        service=service,
        review_id=review.proposal.review_id,
        action="apply",
        decision_id="decision-plan",
        reason="Synthetic private approval reason",
        occurred_at=FIXED_TIME,
        dry_run=True,
    )
    first = decide_review(
        service=service,
        review_id=review.proposal.review_id,
        action="apply",
        decision_id="decision-apply",
        reason="Synthetic private approval reason",
        occurred_at=FIXED_TIME,
    )
    repeated = decide_review(
        service=service,
        review_id=review.proposal.review_id,
        action="apply",
        decision_id="decision-repeat",
        reason="Synthetic private approval reason",
        occurred_at=FIXED_TIME,
    )

    assert planned.envelope["status"] == "planned"
    assert len(service.calls) == 2
    assert service.calls[0][1].actor == Actor(ActorKind.OWNER, "cli-owner")
    assert service.calls[0][1].target_state is ReviewState.APPLIED
    assert first.exit_code is ExitCode.SUCCESS
    assert first.envelope["idempotent"] is False
    assert repeated.exit_code is ExitCode.SUCCESS
    assert repeated.envelope["idempotent"] is True
    assert repeated.envelope["state"] == "applied"
    assert "Synthetic private" not in planned.to_json()
    assert "Synthetic private" not in first.to_json()
    assert "Synthetic private" not in repeated.to_json()


@pytest.mark.parametrize(
    ("action", "expected_state"),
    [
        ("reject", ReviewState.REJECTED),
    ],
)
def test_non_apply_decisions_are_dry_run_aware_and_never_promote_content(
    action: str, expected_state: ReviewState
) -> None:
    review = _review()
    service = DecisionServiceFake(review)

    planned = decide_review(
        service=service,
        review_id=review.proposal.review_id,
        action=action,
        decision_id=f"decision-{action}-plan",
        reason="Synthetic private owner decision",
        occurred_at=FIXED_TIME,
        dry_run=True,
    )
    first = decide_review(
        service=service,
        review_id=review.proposal.review_id,
        action=action,
        decision_id=f"decision-{action}",
        reason="Synthetic private owner decision",
        occurred_at=FIXED_TIME,
    )
    repeated = decide_review(
        service=service,
        review_id=review.proposal.review_id,
        action=action,
        decision_id=f"decision-{action}-repeat",
        reason="Synthetic private owner decision",
        occurred_at=FIXED_TIME,
    )

    assert planned.envelope["status"] == "planned"
    assert len(service.calls) == 2
    assert service.calls[0][1].target_state is expected_state
    assert service.calls[0][1].actor.kind is ActorKind.OWNER
    assert first.envelope["idempotent"] is False
    assert repeated.envelope["idempotent"] is True
    assert repeated.envelope["state"] == expected_state.value
    assert service.aggregate.approved_record is None
    assert "Synthetic private" not in repeated.to_json()


class MaintenanceFake:
    def __init__(self, target: CurationTarget) -> None:
        self.target = target
        self.edit_calls: list[tuple[ReviewId, ReviewTargetEdit, bool]] = []
        self.archive_calls: list[tuple[str, datetime, bool]] = []

    def edit_curation_target(
        self,
        review_id: ReviewId,
        command: ReviewTargetEdit,
        *,
        taxonomy: CurationTaxonomy,
        dry_run: bool,
    ) -> CurationTarget:
        self.edit_calls.append((review_id, command, dry_run))
        self.target = self.target.edit(command, taxonomy=taxonomy)
        return self.target

    def archive_reviews(
        self, *, before: str, occurred_at: datetime, dry_run: bool
    ) -> ArchiveResult:
        self.archive_calls.append((before, occurred_at, dry_run))
        return ArchiveResult(1, ("2026-06",))

    def get_archived(self, review_id: ReviewId) -> ArchivedReview | None:
        del review_id
        return None


class FixedClock:
    def now(self) -> datetime:
        return FIXED_TIME


def test_review_edit_and_archive_dispatch_through_typed_maintenance_without_defer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review()
    taxonomy = CurationTaxonomy(
        categories={
            PrivacyTier.WORK: frozenset({"projects"}),
            PrivacyTier.PERSONAL: frozenset({"journal"}),
        }
    )
    target = CurationTarget.create(
        review=review,
        tier=PrivacyTier.WORK,
        category="projects",
        slug="synthetic",
        title=None,
        classification_class=CurationClass.NEW_PAGE,
        occurred_at=FIXED_TIME,
        taxonomy=taxonomy,
    )
    maintenance = MaintenanceFake(target)
    registry = CommandAdapterRegistry(
        {"review": ReviewCommandAdapter(maintenance, taxonomy, FixedClock())}
    )

    edit_exit = main(
        (
            "--json",
            "review",
            "edit",
            str(review.proposal.review_id),
            "--tier=work",
            "--category=projects",
            "--slug=edited",
            "--class=page_update",
        ),
        command_adapters=registry,
    )
    edit_output = json.loads(capsys.readouterr().out)
    archive_exit = main(
        ("--json", "review", "archive", "--before=2026-07", "--dry-run"),
        command_adapters=registry,
    )
    archive_output = json.loads(capsys.readouterr().out)

    assert edit_exit is ExitCode.SUCCESS
    assert edit_output == {
        "action": "edit",
        "command": "review",
        "dry_run": False,
        "state": "open",
        "status": "edited",
        "tier": "work",
    }
    assert archive_exit is ExitCode.SUCCESS
    assert archive_output == {
        "action": "archive",
        "archived": 1,
        "command": "review",
        "dry_run": True,
        "status": "planned",
    }
    assert len(maintenance.edit_calls) == 1
    assert len(maintenance.archive_calls) == 1
    assert review.proposal.state is ReviewState.OPEN
