from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    DecisionOutcome,
    ProposalDraft,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine import facet_contracts as fc

from open_brain.profile import compile_single_user_local
from open_brain_collector.capture_candidates import CandidateResolver
from open_brain_collector.capture_review import CaptureReviewStore
from open_brain_collector.capture_workflow import automation_authority, owner_authority
from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
from open_brain_connectors.runtime.live_common import LiveBatch, local_source_privacy
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

ACCOUNT = "account:" + "a" * 64
SELECTION = SourceResourceSelection("slack", ACCOUNT, "channel:CSYNTHETIC", "channel")


class Runtime(PriorityRuntime):
    batch: LiveBatch

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        return self.batch


def intake(text: str, revision: str = "edited:1") -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey(
            "slack", ACCOUNT, "channel:CSYNTHETIC", "message:1791900000.000001", revision
        ),
        "https://slack.com/archives/CSYNTHETIC/p1791900000000001",
        text,
        local_source_privacy(),
    )


def test_public_workflow_evidence_review_candidates_and_correction(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Synthetic",))
    tasks = open_local_engine(profile)
    seed = tasks.capture.accept(
        TextPayload("Seed"), delivery_id="workflow.seed", space_id=tasks.inbox.spaces()[0].space_id
    )
    proposal = tasks.review.propose(
        (seed.capture_id,),
        (ProposalDraft("Synthetic", "Initial."),),
        delivery_id="workflow.seed.proposal",
    )[0]
    tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="workflow.seed.approve",
        expected_review_digest=proposal.review_digest,
    )
    assert proposal.page_id is not None
    runtime = Runtime(tmp_path / "live")
    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=runtime)
    manager.dispatch(
        "sources.slack_policy_setup", {"connection_id": ACCOUNT, "proposal_opt_in": True}
    )
    manager.dispatch(
        "sources.slack_mapping_add",
        {"connection_id": ACCOUNT, "channel_id": "CSYNTHETIC", "page_id": proposal.page_id},
    )
    assert tasks.facets is not None
    dimension = tasks.facets.dimension_create(
        fc.DimensionCreateRequest(
            operation_id="workflow.dimension", expected_version=0, name="Topics"
        ),
        authority=owner_authority(),
    ).to_wire()
    dimension_id = cast(str, dimension["subject_id"])
    manager.dispatch(
        "sources.capture_setup",
        {
            "enabled": True,
            "ai_exclusions_verified": True,
            "candidate_dimensions": {"subject": dimension_id},
        },
    )
    manager.capture.configure("synthetic", SELECTION, {})
    runtime.batch = LiveBatch(
        (intake("Decision: Use synthetic storage\nCommitted: Ship on Friday"),), {"page": 1}
    )
    preview = manager.capture.preview("synthetic")
    manager.capture.apply("synthetic", cast(str, preview["preview_id"]))
    result = manager.dispatch("sources.capture_process", {})
    assert result["captures"] == 2
    assert manager.dispatch("sources.capture_process", {})["captures"] == 0
    items = cast(list[dict[str, object]], manager.dispatch("sources.capture_review", {})["items"])
    assert len(items) == 2
    review_id = cast(str, items[0]["proposal_id"])
    inspection = tasks.review.show(review_id)
    manager.dispatch(
        "sources.capture_review_defer",
        {"proposal_id": review_id, "until": "2099-01-01T00:00:00+00:00"},
    )
    assert len(cast(list[object], manager.dispatch("sources.capture_review", {})["items"])) == 1
    candidates = cast(
        list[dict[str, object]], manager.dispatch("sources.capture_candidates", {})["candidates"]
    )
    assert len(candidates) == 2
    value = tasks.facets.value_create(
        fc.ValueCreateRequest(
            operation_id="workflow.value",
            expected_version=0,
            dimension_id=dimension_id,
            expected_dimension_version=1,
            name="Existing topic",
        ),
        authority=owner_authority(),
    ).to_wire()
    resolved = manager.dispatch(
        "sources.capture_candidate_decide",
        {
            "candidate_id": candidates[0]["candidate_id"],
            "action": "map",
            "value_id": value["subject_id"],
        },
    )
    assert resolved["state"] == "resolved"
    assert (
        CandidateResolver(tmp_path / "live", profile.root).retry(
            cast(str, candidates[0]["candidate_id"])
        )["state"]
        == "resolved"
    )
    runtime.batch = LiveBatch(
        (intake("Correction: Friday is no longer agreed", "edited:2"),), {"page": 2}
    )
    preview = manager.capture.preview("synthetic")
    manager.capture.apply("synthetic", cast(str, preview["preview_id"]))
    with pytest.raises(ValueError, match="development evidence revision conflict"):
        tasks.review.decide(
            review_id,
            DecisionOutcome.APPROVED,
            delivery_id="workflow.stale.approve",
            expected_review_digest=inspection.review_digest,
        )
    with pytest.raises(ValueError, match="unsupported_capability"):
        tasks.review.with_authority(automation_authority()).decide(
            review_id,
            DecisionOutcome.APPROVED,
            delivery_id="workflow.forbidden.approve",
            expected_review_digest=inspection.review_digest,
        )


def test_workflow_off_by_default_and_exclusion_holds_processing(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    runtime = Runtime(tmp_path / "live")
    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=runtime)
    assert manager.dispatch("sources.capture_process", {})["outcome"] == "disabled"
    manager.dispatch("sources.slack_policy_setup", {"connection_id": ACCOUNT})
    manager.dispatch("sources.capture_setup", {"enabled": True})
    manager.capture.configure("synthetic", SELECTION, {})
    runtime.batch = LiveBatch((intake("Decision: Keep private"),), {"page": 1})
    preview = manager.capture.preview("synthetic")
    manager.capture.apply("synthetic", cast(str, preview["preview_id"]))
    assert (
        manager.dispatch("sources.capture_process", {})["failure_code"]
        == "unsupported_ai_exclusions"
    )
    assert manager.workflow.status()["development_count"] == 0
    review = CaptureReviewStore(PrivateJsonStore(tmp_path / "live" / "capture-review"))
    assert review.candidates() == ()
    manager.dispatch("sources.capture_setup", {"ai_exclusions_verified": True})
    assert manager.dispatch("sources.capture_process", {})["captures"] == 1


def test_current_exclusion_fences_retained_preview_and_extraction(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    runtime = Runtime(tmp_path / "live")
    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=runtime)
    manager.dispatch("sources.slack_policy_setup", {"connection_id": ACCOUNT})
    manager.dispatch("sources.capture_setup", {"enabled": True, "ai_exclusions_verified": True})
    manager.capture.configure("synthetic", SELECTION, {})
    runtime.batch = LiveBatch((intake("Decision: Keep private"),), {"page": 1})
    preview = manager.capture.preview("synthetic")
    manager.dispatch("sources.capture_setup", {"excluded_conversations": ["CSYNTHETIC"]})
    from open_brain_connectors.runtime.live_common import LiveSourceError

    with pytest.raises(LiveSourceError, match="source_policy_excluded"):
        manager.capture.apply("synthetic", cast(str, preview["preview_id"]))
    assert not open_local_engine(profile).retrieval.search("Keep private")
    manager.dispatch("sources.capture_setup", {"excluded_conversations": []})
    manager.capture.apply("synthetic", cast(str, preview["preview_id"]))
    manager.dispatch("sources.capture_setup", {"excluded_conversations": ["CSYNTHETIC"]})
    assert manager.dispatch("sources.capture_process", {})["held"] == 1
    assert manager.workflow.status()["development_count"] == 0


def test_health_reads_pending_age_and_acknowledged_checkpoint(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    runtime = Runtime(tmp_path / "live")
    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=runtime)
    manager.dispatch("sources.slack_policy_setup", {"connection_id": ACCOUNT})
    manager.dispatch("sources.capture_setup", {"enabled": True, "ai_exclusions_verified": True})
    manager.capture.configure("synthetic", SELECTION, {})
    manager.capture.control("synthetic", "enable")
    runtime.batch = LiveBatch((intake("Decision: Inspect backlog"),), {"page": 1})
    preview = manager.capture.preview("synthetic")
    manager.capture.apply("synthetic", cast(str, preview["preview_id"]))
    state = manager.workflow.queue._load()
    observations = cast(dict[str, dict[str, object]], state["observations"])
    for entry in observations.values():
        observation = cast(dict[str, object], entry["observation"])
        observation["observed_at"] = datetime.fromtimestamp(100, UTC).isoformat()
    manager.workflow.queue._save(state)
    health = manager.dispatch("sources.health", {})
    metrics = cast(dict[str, int], health["metrics"])
    assert metrics["backlog_count"] == 1
    assert metrics["oldest_pending_epoch"] == 100
    assert cast(int, health["backlog_age_seconds"]) > 100
    assert cast(int, health["checkpoint_lag_seconds"]) >= 0
    manager.dispatch("sources.capture_process", {})
    after = manager.dispatch("sources.health", {})
    assert cast(dict[str, int], after["metrics"])["backlog_count"] == 0
    assert after["backlog_age_seconds"] is None
