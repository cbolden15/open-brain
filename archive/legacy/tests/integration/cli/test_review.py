from __future__ import annotations

from datetime import UTC, datetime

from open_brain_engine.core.ids import ReviewId
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewProposal,
)

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.review import list_reviews, preview_review, show_review

FIXED_TIME = datetime(2026, 8, 14, 12, tzinfo=UTC)


class ReviewReaderFake:
    def __init__(self, reviews: tuple[ReviewAggregate, ...]) -> None:
        self._reviews = {review.proposal.review_id: review for review in reviews}
        self.get_calls: list[ReviewId] = []

    def get(self, review_id: ReviewId) -> ReviewAggregate | None:
        self.get_calls.append(review_id)
        return self._reviews.get(review_id)


def _review(*, suffix: str) -> ReviewAggregate:
    proposal = ReviewProposal.create(
        capture_id="cap_" + suffix * 64,
        source_ref="https://example.test/synthetic-source",
        privacy_tier=PrivacyTier.WORK,
        proposed_intent=Intent.IDEA,
        proposal_reason="Synthetic private proposal reason",
        capture_why="Synthetic private capture reason",
        created_at=FIXED_TIME,
        created_by=Actor(ActorKind.SYSTEM, "fixture"),
    )
    return ReviewAggregate.create(proposal)


def test_review_read_adapters_are_sorted_redacted_and_non_mutating() -> None:
    first = _review(suffix="a")
    second = _review(suffix="b")
    reader = ReviewReaderFake((second, first))

    listed = list_reviews(reviews=(second, first), state="open")
    shown = show_review(reader=reader, review_id=first.proposal.review_id)
    previewed = preview_review(reader=reader, review_id=first.proposal.review_id)

    expected_review = {
        "capture_id": str(first.proposal.capture_id),
        "privacy_tier": "work",
        "proposed_intent": "idea",
        "review_id": str(first.proposal.review_id),
        "state": "open",
    }
    assert listed.exit_code is ExitCode.SUCCESS
    expected_reviews = sorted(
        (
            expected_review,
            {
                **expected_review,
                "capture_id": str(second.proposal.capture_id),
                "review_id": str(second.proposal.review_id),
            },
        ),
        key=lambda review: str(review["review_id"]),
    )
    assert listed.envelope == {
        "command": "review",
        "reviews": expected_reviews,
        "state": "open",
        "status": "listed",
    }
    assert shown.exit_code is ExitCode.SUCCESS
    assert shown.envelope == {"command": "review", "review": expected_review, "status": "shown"}
    assert previewed.exit_code is ExitCode.SUCCESS
    assert previewed.envelope == {
        "command": "review",
        "dry_run": True,
        "review": expected_review,
        "status": "previewed",
    }
    assert reader.get_calls == [first.proposal.review_id, first.proposal.review_id]
    assert "Synthetic private" not in listed.to_json()
    assert "Synthetic private" not in shown.to_json()
    assert "Synthetic private" not in previewed.to_json()
