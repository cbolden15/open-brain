from __future__ import annotations

from datetime import UTC, datetime

from open_brain_engine.core.ids import ReviewId
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewDecisionResult,
    ReviewProposal,
)

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.proposals import list_proposals, resolve_proposal

FIXED_TIME = datetime(2026, 8, 14, 12, tzinfo=UTC)


def _review_sort_key(proposal: dict[str, str]) -> str:
    return proposal["review_id"]


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


def _proposal(*, suffix: str) -> ReviewAggregate:
    return ReviewAggregate.create(
        ReviewProposal.create(
            capture_id="cap_" + suffix * 64,
            source_ref="https://example.test/synthetic-source",
            privacy_tier=PrivacyTier.WORK,
            proposed_intent=Intent.ACTION_CANDIDATE,
            proposal_reason="Synthetic third-party proposal reason",
            capture_why="Synthetic owner-authored review statement",
            created_at=FIXED_TIME,
            created_by=Actor(ActorKind.SYSTEM, "fixture"),
        )
    )


def test_proposal_list_is_deterministic_redacted_and_defers_old_format() -> None:
    first = _proposal(suffix="d")
    second = _proposal(suffix="e")

    listed = list_proposals(proposals=(second, first))
    old_format = list_proposals(proposals=({"id": "synthetic-old-proposal"},))

    expected_proposals: list[dict[str, str]] = [
        {
            "capture_id": str(first.proposal.capture_id),
            "privacy_tier": "work",
            "proposed_intent": "action_candidate",
            "review_id": str(first.proposal.review_id),
            "state": "open",
        },
        {
            "capture_id": str(second.proposal.capture_id),
            "privacy_tier": "work",
            "proposed_intent": "action_candidate",
            "review_id": str(second.proposal.review_id),
            "state": "open",
        },
    ]
    assert listed.exit_code is ExitCode.SUCCESS
    assert listed.envelope == {
        "command": "proposals",
        "proposals": sorted(
            expected_proposals,
            key=_review_sort_key,
        ),
        "status": "listed",
    }
    assert old_format.exit_code is ExitCode.DEFERRED
    assert old_format.envelope == {
        "command": "proposals",
        "error": {
            "code": "proposal_format_migration_required",
            "message": "old proposal format detected; migrate before retrying",
            "redacted": True,
        },
        "status": "migration_required",
    }
    assert "Synthetic third-party" not in listed.to_json()
    assert "synthetic-old-proposal" not in old_format.to_json()


def test_proposal_resolution_is_explicit_review_gated_and_idempotent() -> None:
    proposal = _proposal(suffix="f")
    service = DecisionServiceFake(proposal)

    planned = resolve_proposal(
        service=service,
        proposal=proposal,
        action="apply",
        decision_id="proposal-plan",
        reason="Synthetic private resolution reason",
        occurred_at=FIXED_TIME,
        dry_run=True,
    )
    first = resolve_proposal(
        service=service,
        proposal=proposal,
        action="apply",
        decision_id="proposal-apply",
        reason="Synthetic private resolution reason",
        occurred_at=FIXED_TIME,
    )
    repeated = resolve_proposal(
        service=service,
        proposal=proposal,
        action="apply",
        decision_id="proposal-repeat",
        reason="Synthetic private resolution reason",
        occurred_at=FIXED_TIME,
    )
    old_format = resolve_proposal(
        service=service,
        proposal={"id": "synthetic-old-proposal"},
        action="apply",
        decision_id="proposal-old",
        reason="Synthetic private resolution reason",
        occurred_at=FIXED_TIME,
    )

    assert planned.envelope["status"] == "planned"
    assert len(service.calls) == 2
    assert first.envelope["status"] == "resolved"
    assert first.envelope["idempotent"] is False
    assert repeated.envelope["idempotent"] is True
    assert repeated.envelope["state"] == "applied"
    assert old_format.exit_code is ExitCode.DEFERRED
    assert old_format.envelope["status"] == "migration_required"
    assert "Synthetic private" not in planned.to_json()
    assert "Synthetic private" not in repeated.to_json()
    assert "synthetic-old-proposal" not in old_format.to_json()
