from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import Intent, PrivacyTier
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewProposal,
    ReviewState,
)

from open_brain_legacy.production.optional_automation import (
    OptionalAutomationConfigError,
    approved_life_os_candidates,
    load_private_life_os_config,
    load_private_messages_config,
)
from open_brain_legacy.review.store import SqliteReviewStore


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 16, 20, 0, tzinfo=UTC)


def _private_file(path: Path, value: object) -> Path:
    path.write_bytes(canonical_json_bytes(value))
    path.chmod(0o600)
    return path


def _create_review(root: Path, *, suffix: str, intent: Intent, applied: bool) -> None:
    aggregate = ReviewAggregate.create(
        ReviewProposal.create(
            capture_id="cap_" + suffix * 64,
            source_ref=f"https://example.test/{suffix}",
            privacy_tier=PrivacyTier.PERSONAL,
            proposed_intent=intent,
            proposal_reason="Synthetic optional automation candidate",
            capture_why="Owner reviewed synthetic optional automation candidate",
            created_at=FixedClock().now(),
            created_by=Actor(ActorKind.SYSTEM, "synthetic-automation"),
        )
    )
    payload = canonical_json_bytes(aggregate.to_dict())
    with SqliteReviewStore(
        root=root,
        database_name="review/review.sqlite3",
        clock=FixedClock(),
    ) as reviews:
        reviews.create_if_absent(
            aggregate,
            payload_digest=sha256(payload).hexdigest(),
        )
        if applied:
            reviews.decide(
                aggregate.proposal.review_id,
                ReviewDecisionCommand.create(
                    decision_id=f"decision-{suffix}",
                    target_state=ReviewState.APPLIED,
                    reason="Owner approved synthetic optional automation candidate",
                    occurred_at=FixedClock().now(),
                    actor=Actor(ActorKind.OWNER, "synthetic-owner"),
                ),
            )


def test_private_optional_automation_configs_are_closed_and_owner_only(
    tmp_path: Path,
) -> None:
    life_os_path = _private_file(
        tmp_path / "life-os.json",
        {"schema_version": 1, "candidate_limit": 25},
    )
    messages_path = _private_file(
        tmp_path / "messages.json",
        {"schema_version": 1, "resource_ref": "messages_configured"},
    )

    assert load_private_life_os_config(life_os_path).candidate_limit == 25
    assert load_private_messages_config(messages_path).resource_ref == "messages_configured"

    life_os_path.chmod(0o644)
    with pytest.raises(OptionalAutomationConfigError):
        load_private_life_os_config(life_os_path)

    malformed = _private_file(
        tmp_path / "malformed.json",
        {"schema_version": 1, "resource_ref": "messages", "extra": True},
    )
    with pytest.raises(OptionalAutomationConfigError):
        load_private_messages_config(malformed)


def test_life_os_candidates_include_only_applied_action_reviews_with_limit(
    tmp_path: Path,
) -> None:
    _create_review(tmp_path, suffix="a", intent=Intent.ACTION_CANDIDATE, applied=True)
    _create_review(tmp_path, suffix="b", intent=Intent.IDEA, applied=True)
    _create_review(tmp_path, suffix="c", intent=Intent.ACTION_CANDIDATE, applied=False)
    config_path = _private_file(
        tmp_path / "life-os.json",
        {"schema_version": 1, "candidate_limit": 1},
    )

    candidates = approved_life_os_candidates(
        root=tmp_path,
        clock=FixedClock(),
        config=load_private_life_os_config(config_path),
    )

    assert len(candidates) == 1
    assert candidates[0].requires_review is True
    assert candidates[0].candidate_id.startswith("intent_")
    assert candidates[0].review_id.startswith("review_")
