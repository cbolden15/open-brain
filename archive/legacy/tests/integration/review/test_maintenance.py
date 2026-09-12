from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import Intent, PrivacyTier, ValidationError
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
)

from open_brain_legacy.review.maintenance import (
    CurationClass,
    CurationTarget,
    CurationTaxonomy,
    ReviewTargetEdit,
    predecessor_curation_taxonomy,
)
from open_brain_legacy.review.store import ReviewStoreError, SqliteReviewStore

CREATED = datetime(2026, 6, 9, 9, tzinfo=UTC)
EDITED = datetime(2026, 6, 9, 10, tzinfo=UTC)
CLOSED = datetime(2026, 6, 10, 10, tzinfo=UTC)
ARCHIVED = datetime(2026, 7, 1, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return CREATED


def _taxonomy() -> CurationTaxonomy:
    return CurationTaxonomy(
        categories={
            PrivacyTier.WORK: frozenset({"projects", "patterns"}),
            PrivacyTier.PERSONAL: frozenset({"journal"}),
        }
    )


def test_predecessor_taxonomy_and_aliases_preserve_nested_edit_semantics() -> None:
    taxonomy = predecessor_curation_taxonomy()
    review = _review("f")
    original = CurationTarget.create(
        review=review,
        tier=PrivacyTier.WORK,
        category="projects",
        slug="synthetic",
        title="Synthetic title",
        classification_class=CurationClass.NEW_PAGE,
        occurred_at=CREATED,
        taxonomy=taxonomy,
    )

    edited = original.edit(
        ReviewTargetEdit.create(
            tier="business",
            category="patterns",
            slug="nested/synthetic.md",
            title=None,
            classification_class=None,
            occurred_at=EDITED,
            actor=Actor(ActorKind.OWNER, "cli-owner"),
        ),
        taxonomy=taxonomy,
    )

    assert edited.tier is PrivacyTier.WORK
    assert edited.page == PurePosixPath("patterns/nested/synthetic.md")
    assert edited.title == "Synthetic title"
    assert edited.classification_class is CurationClass.NEW_PAGE
    assert taxonomy.require(tier=PrivacyTier.PERSONAL, category="relationships") == (
        "relationships"
    )
    with pytest.raises(ValidationError, match="invalid curation category"):
        taxonomy.require(tier=PrivacyTier.PERSONAL, category="projects")


def _review(suffix: str, *, privacy: PrivacyTier = PrivacyTier.WORK) -> ReviewAggregate:
    return ReviewAggregate.create(
        ReviewProposal.create(
            capture_id="cap_" + suffix * 64,
            source_ref="synthetic-source",
            privacy_tier=privacy,
            proposed_intent=Intent.IDEA,
            proposal_reason="Synthetic proposal",
            capture_why="Synthetic owner statement",
            created_at=CREATED,
            created_by=Actor(ActorKind.SYSTEM, "fixture"),
        )
    )


def _create(store: SqliteReviewStore, review: ReviewAggregate) -> None:
    payload = canonical_json_bytes(review.to_dict())
    store.create_if_absent(review, payload_digest=sha256(payload).hexdigest())


def _reject(store: SqliteReviewStore, review: ReviewAggregate) -> None:
    store.decide(
        review.proposal.review_id,
        ReviewDecisionCommand.create(
            decision_id="reject-" + str(review.proposal.review_id)[-8:],
            target_state=ReviewState.REJECTED,
            reason="Synthetic rejection",
            occurred_at=CLOSED,
            actor=Actor(ActorKind.OWNER, "fixture-owner"),
        ),
    )


def test_edit_target_matches_predecessor_validation_audit_and_dry_run(
    tmp_path: Path,
) -> None:
    with SqliteReviewStore(
        root=tmp_path,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ) as store:
        review = _review("a")
        _create(store, review)
        original = CurationTarget.create(
            review=review,
            tier=PrivacyTier.WORK,
            category="patterns",
            slug="private-fact",
            title="Private fact",
            classification_class=CurationClass.NEW_PAGE,
            occurred_at=CREATED,
            taxonomy=_taxonomy(),
        )
        store.register_curation_target(original)
        command = ReviewTargetEdit.create(
            tier=PrivacyTier.WORK,
            category="projects",
            slug="private-fact.md",
            title="Private fact",
            classification_class=CurationClass.PAGE_UPDATE,
            occurred_at=EDITED,
            actor=Actor(ActorKind.OWNER, "cli-owner"),
        )

        planned = store.edit_curation_target(
            review.proposal.review_id,
            command,
            taxonomy=_taxonomy(),
            dry_run=True,
        )
        assert store.get_curation_target(review.proposal.review_id) == original

        edited = store.edit_curation_target(
            review.proposal.review_id,
            command,
            taxonomy=_taxonomy(),
            dry_run=False,
        )

        assert planned == edited
        assert edited.page == PurePosixPath("projects/private-fact.md")
        assert edited.tier is PrivacyTier.WORK
        assert edited.classification_class is CurationClass.PAGE_UPDATE
        assert store.get(review.proposal.review_id) == review
        events = store.maintenance_events(review.proposal.review_id)
        assert len(events) == 1
        assert events[0].action == "edited"
        assert events[0].actor.kind is ActorKind.OWNER
        assert "Private fact" not in canonical_json_bytes(events[0].to_dict()).decode()


def test_edit_rejects_invalid_category_and_privacy_widening_without_mutation(
    tmp_path: Path,
) -> None:
    with SqliteReviewStore(
        root=tmp_path,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ) as store:
        review = _review("b", privacy=PrivacyTier.PERSONAL)
        _create(store, review)
        original = CurationTarget.create(
            review=review,
            tier=PrivacyTier.PERSONAL,
            category="journal",
            slug="private-fact",
            title=None,
            classification_class=CurationClass.NEW_PAGE,
            occurred_at=CREATED,
            taxonomy=_taxonomy(),
        )
        store.register_curation_target(original)

        invalid_category = ReviewTargetEdit.create(
            tier=PrivacyTier.PERSONAL,
            category="projects",
            slug="private-fact",
            title=None,
            classification_class=CurationClass.NEW_PAGE,
            occurred_at=EDITED,
            actor=Actor(ActorKind.OWNER, "cli-owner"),
        )
        widening = ReviewTargetEdit.create(
            tier=PrivacyTier.WORK,
            category="projects",
            slug="private-fact",
            title=None,
            classification_class=CurationClass.NEW_PAGE,
            occurred_at=EDITED,
            actor=Actor(ActorKind.OWNER, "cli-owner"),
        )

        with pytest.raises(ValidationError, match="invalid curation category"):
            store.edit_curation_target(
                review.proposal.review_id,
                invalid_category,
                taxonomy=_taxonomy(),
                dry_run=False,
            )
        with pytest.raises(ValidationError, match="privacy"):
            store.edit_curation_target(
                review.proposal.review_id,
                widening,
                taxonomy=_taxonomy(),
                dry_run=False,
            )

        assert store.get_curation_target(review.proposal.review_id) == original
        assert store.maintenance_events(review.proposal.review_id) == ()


def test_archive_moves_only_old_applied_or_rejected_reviews_and_is_idempotent(
    tmp_path: Path,
) -> None:
    with SqliteReviewStore(
        root=tmp_path,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ) as store:
        closed = _review("c")
        open_review = _review("d")
        _create(store, closed)
        _create(store, open_review)
        _reject(store, closed)

        planned = store.archive_reviews(
            before="2026-07",
            occurred_at=ARCHIVED,
            dry_run=True,
        )
        assert planned.archived == 1
        assert planned.months == ("2026-06",)
        assert store.get(closed.proposal.review_id) is not None
        assert store.get_archived(closed.proposal.review_id) is None

        archived = store.archive_reviews(
            before="2026-07",
            occurred_at=ARCHIVED,
            dry_run=False,
        )

        assert archived == planned
        assert store.get(closed.proposal.review_id) is None
        archived_record = store.get_archived(closed.proposal.review_id)
        assert archived_record is not None
        assert archived_record.aggregate.proposal.state is ReviewState.REJECTED
        assert archived_record.closed_month == "2026-06"
        assert store.get(open_review.proposal.review_id) == open_review
        assert store.archive_reviews(
            before="2026-07",
            occurred_at=ARCHIVED,
            dry_run=False,
        ).archived == 0
        events = store.maintenance_events(closed.proposal.review_id)
        assert [event.action for event in events] == ["archived"]


@pytest.mark.parametrize("value", ["2026-00", "2026-13", "2026-7", "../2026-07"])
def test_archive_rejects_invalid_month_without_writes(tmp_path: Path, value: str) -> None:
    with SqliteReviewStore(
        root=tmp_path,
        database_name="review.sqlite3",
        clock=FixedClock(),
    ) as store:
        review = _review("e")
        _create(store, review)
        _reject(store, review)

        with pytest.raises(ReviewStoreError, match="invalid archive month"):
            store.archive_reviews(before=value, occurred_at=ARCHIVED, dry_run=False)

        assert store.get(review.proposal.review_id) is not None
        assert store.get_archived(review.proposal.review_id) is None
