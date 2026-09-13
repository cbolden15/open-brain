from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from open_brain_engine.core.models import PrivacyDecision, PrivacyTier
from open_brain_engine.core.policy import classify_privacy

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.ledger import claim_lifecycle
from open_brain_legacy.cli.social import SocialCompatibilityAction, compatibility
from open_brain_legacy.ledger.index import ClaimInput, ClaimRecord
from open_brain_legacy.ledger.merge import TrustedCitation
from open_brain_legacy.ledger.render import ClaimViewRenderer, ClaimViewResult, RenderDisposition


def _claim(*, text: str, citation_id: str, observed_at: datetime) -> ClaimInput:
    return ClaimInput.create(
        topic_id="research",
        text=text,
        citations=(
            TrustedCitation.create(
                citation_id=citation_id,
                destination=f"captures/{citation_id}.md",
            ),
        ),
        observed_at=observed_at,
    )


class _RecordingClaimRenderer:
    def __init__(self) -> None:
        self.calls = 0

    def render(
        self, *, claims: tuple[ClaimRecord, ...], privacy: PrivacyDecision
    ) -> ClaimViewResult:
        self.calls += 1
        raise AssertionError("dry run rendered claim views")


def test_claim_lifecycle_dry_run_ranks_without_rendering() -> None:
    observed_at = datetime(2026, 8, 1, tzinfo=UTC)
    renderer = _RecordingClaimRenderer()

    result = claim_lifecycle(
        inputs=(
            _claim(
                text="Synthetic alpha finding",
                citation_id="cite-alpha",
                observed_at=observed_at,
            ),
        ),
        renderer=renderer,
        privacy=classify_privacy(PrivacyTier.WORK, policy_version="synthetic-v1"),
        query="Synthetic alpha finding",
        now=datetime(2026, 8, 14, tzinfo=UTC),
        aging_after=timedelta(days=30),
        retire_after=timedelta(days=60),
        dimensions=32,
        similarity_threshold=1.0,
        limit=1,
        dry_run=True,
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope["status"] == "dry_run"
    assert result.envelope["claim_count"] == 1
    assert renderer.calls == 0
    assert "Synthetic alpha finding" not in result.to_json()


def test_claim_lifecycle_preserves_generations_and_replays_without_delete(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    inputs = (
        _claim(
            text="Synthetic alpha finding",
            citation_id="cite-alpha",
            observed_at=observed_at,
        ),
        _claim(
            text="Synthetic beta observation",
            citation_id="cite-beta",
            observed_at=observed_at,
        ),
    )
    renderer = ClaimViewRenderer(root=tmp_path / "views")
    privacy = classify_privacy(PrivacyTier.WORK, policy_version="synthetic-v1")
    active = claim_lifecycle(
        inputs=inputs,
        renderer=renderer,
        privacy=privacy,
        query="Synthetic alpha finding",
        now=datetime(2026, 1, 10, tzinfo=UTC),
        aging_after=timedelta(days=30),
        retire_after=timedelta(days=60),
        dimensions=32,
        similarity_threshold=1.0,
        limit=1,
        dry_run=False,
    )
    retired = claim_lifecycle(
        inputs=inputs,
        renderer=renderer,
        privacy=privacy,
        query="Synthetic alpha finding",
        now=datetime(2026, 4, 1, tzinfo=UTC),
        aging_after=timedelta(days=30),
        retire_after=timedelta(days=60),
        dimensions=32,
        similarity_threshold=1.0,
        limit=1,
        dry_run=False,
    )
    replay = claim_lifecycle(
        inputs=inputs,
        renderer=renderer,
        privacy=privacy,
        query="Synthetic alpha finding",
        now=datetime(2026, 4, 1, tzinfo=UTC),
        aging_after=timedelta(days=30),
        retire_after=timedelta(days=60),
        dimensions=32,
        similarity_threshold=1.0,
        limit=1,
        dry_run=False,
    )

    assert active.exit_code is ExitCode.SUCCESS
    assert retired.exit_code is ExitCode.SUCCESS
    assert active.envelope["ranked_claim_ids"] == retired.envelope["ranked_claim_ids"]
    assert isinstance(active.value, ClaimViewResult)
    assert isinstance(retired.value, ClaimViewResult)
    assert isinstance(replay.value, ClaimViewResult)
    assert active.value.current.disposition is RenderDisposition.CREATED
    assert retired.value.archive.disposition is RenderDisposition.CREATED
    assert replay.value.current.disposition is RenderDisposition.UNCHANGED
    assert replay.value.archive.disposition is RenderDisposition.UNCHANGED
    assert len(tuple((tmp_path / "views").rglob("*.md"))) == 4
    assert "Synthetic alpha finding" not in active.to_json()


def test_social_compatibility_retains_the_open_brain_implementation() -> None:
    result = compatibility(action=SocialCompatibilityAction.RETAIN, dry_run=True)

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "action": "retain",
        "command": "social.compatibility",
        "disposition": "open-brain-live",
        "dry_run": True,
        "status": "implementation-ready",
    }


def test_social_compatibility_refuses_predecessor_retirement() -> None:
    result = compatibility(action=SocialCompatibilityAction.RETIRE, dry_run=True)

    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope["status"] == "blocked"
    assert result.envelope["error"]["redacted"] is True  # type: ignore[index]
