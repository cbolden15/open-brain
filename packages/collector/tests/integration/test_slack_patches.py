from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    DecisionOutcome,
    EngineTaskSet,
    LocalEngineContext,
    ProposalDraft,
    TextPayload,
    open_local_engine,
)

from open_brain.profile import compile_single_user_local
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_collector.slack_patches import SlackPatchProposer
from open_brain_collector.slack_policy import SlackPolicyStore
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveSourceError,
    local_source_privacy,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

_ACCOUNT = "account:" + "a" * 64
_CHANNEL = "CROADMAP"
_SELECTION = SourceResourceSelection("slack", _ACCOUNT, f"channel:{_CHANNEL}", "channel")


@dataclass
class _Clock:
    value: int = 100

    def __call__(self) -> int:
        return self.value


class _SlackRuntime:
    def __init__(self, batch: LiveBatch) -> None:
        self.batch = batch
        self.calls = 0

    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None:
        assert selection == _SELECTION

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        assert selection == _SELECTION and options == {"date_floor": "2026-09-01T00:00:00Z"}
        self.calls += 1
        return self.batch


def _intake(external_id: str, text: str, *, revision: str) -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey("slack", _ACCOUNT, f"channel:{_CHANNEL}", external_id, revision),
        "https://example.slack.com/archives/CROADMAP/p" + external_id.replace(":", ""),
        text,
        local_source_privacy(),
        title="Slack roadmap",
    )


def _published_page(tmp_path: Path) -> tuple[LocalEngineContext, EngineTaskSet, str]:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Roadmap",))
    tasks = open_local_engine(profile)
    source = tasks.capture.accept(
        TextPayload("Seed roadmap page"),
        delivery_id="slack.patch.seed.capture",
        space_id=tasks.inbox.spaces()[0].space_id,
    )
    proposal = tasks.review.propose(
        source.capture_id,
        (ProposalDraft("Roadmap", "Current roadmap."),),
        delivery_id="slack.patch.seed.proposal",
    )[0]
    tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="slack.patch.seed.approve",
        expected_review_digest=proposal.review_digest,
    )
    assert proposal.page_id is not None
    return profile, tasks, proposal.page_id


def test_scheduled_slack_thread_patch_is_opt_in_idempotent_and_provenanced(
    tmp_path: Path,
) -> None:
    profile, tasks, page_id = _published_page(tmp_path)
    policy = SlackPolicyStore(tmp_path / "live")
    policy.setup(_ACCOUNT, {"proposal_opt_in": True})
    policy.add_mapping(_ACCOUNT, _CHANNEL, page_id, None, page_exists=lambda page: page == page_id)
    batch = LiveBatch(
        (
            _intake("message:1790000000.000100", "Root roadmap update", revision="edited:1"),
            _intake(
                "reply:1790000000.000100:1790000001.000100",
                "Thread reply",
                revision="edited:2",
            ),
        ),
        {"cursor": "next"},
    )
    runtime = _SlackRuntime(batch)
    clock = _Clock()
    proposer = SlackPatchProposer(profile.root, policy)

    def propose_captured(
        selection: SourceResourceSelection,
        records: tuple[tuple[SourceRecordIntake, str], ...],
    ) -> None:
        proposer.propose(selection, records)

    service = LiveCaptureService(
        tmp_path / "live" / "capture",
        profile.root,
        runtime=runtime,
        post_apply=propose_captured,
        clock=clock,
    )
    service.configure("slack.roadmap", _SELECTION, {"date_floor": "2026-09-01T00:00:00Z"})
    service.control("slack.roadmap", "enable", interval_seconds=14_400)

    first = service.sync_due()[0]
    assert first["captured_count"] == 2
    assert service.status("slack.roadmap")["next_run_epoch"] == 14_500
    pending = tasks.review.list(status="pending")
    assert len(pending) == 1
    shown = tasks.review.show(pending[0].proposal_id)
    assert shown.patch is not None and shown.target_page_id == page_id
    assert "Slack update" in (shown.patch_diff or "")
    assert len(shown.selected_capture_ids) == 2 and shown.operation == "update"

    drift_source = tasks.capture.accept(
        TextPayload("Owner update after Slack proposal"),
        delivery_id="slack.patch.drift.capture",
        space_id=tasks.inbox.spaces()[0].space_id,
    )
    drift = tasks.review.propose_append(
        drift_source.capture_id,
        target_page_id=page_id,
        append_markdown="### Owner update",
        delivery_id="slack.patch.drift.proposal",
    )[0]
    tasks.review.decide(
        drift.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="slack.patch.drift.approve",
        expected_review_digest=drift.review_digest,
    )
    with pytest.raises(ValueError, match="canonical page revision conflict"):
        tasks.review.decide(
            pending[0].proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="slack.patch.stale.approve",
            expected_review_digest=shown.review_digest,
        )

    clock.value = 14_500
    restarted = LiveCaptureService(
        tmp_path / "live" / "capture",
        profile.root,
        runtime=runtime,
        post_apply=propose_captured,
        clock=clock,
    )
    catch_up = restarted.sync_due()[0]
    assert catch_up["duplicate_count"] == 2 and runtime.calls == 2
    assert len(tasks.review.list(status="pending")) == 2

    mappings = cast(list[dict[str, object]], policy.mappings(_ACCOUNT)["mappings"])
    mapping = mappings[0]
    policy.remove_mapping(_ACCOUNT, cast(str, mapping["mapping_id"]))
    policy.add_mapping(_ACCOUNT, _CHANNEL, page_id, None, page_exists=lambda page: page == page_id)
    clock.value += 14_400
    assert restarted.sync_due()[0]["duplicate_count"] == 2
    assert len(tasks.review.list(status="pending")) == 3


def test_discovery_is_daily_and_capture_failures_preserve_the_pending_patch_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    manager = LiveSourceManager(tmp_path / "live", brain)
    configured = manager.dispatch(
        "sources.configure",
        {
            "selection": {
                "connector_name": "slack",
                "connection_id": _ACCOUNT,
                "resource_id": f"channel:{_CHANNEL}",
                "resource_type": "channel",
            },
            "options": {"date_floor": "2026-09-01T00:00:00Z"},
        },
    )
    assert configured["interval_seconds"] == 14_400
    manager._slack_policy.setup(_ACCOUNT, {"keywords": []})
    calls: list[str] = []

    def discover(operation: str, args: dict[str, object]) -> dict[str, object]:
        calls.append(operation)
        return {"fetch_candidates": [], "has_more": False}

    monkeypatch.setattr(
        manager.capture,
        "sync_due",
        lambda: [{"source_id": "slack", "outcome": "completed"}],
    )
    monkeypatch.setattr(
        manager,
        "_slack",
        discover,
    )
    assert manager.sync_due()[-1]["source_id"] == "slack"
    assert calls == ["sources.slack_discover"]
    manager._slack_policy.record_discovery(_ACCOUNT, None, (), completed_at=10**10)
    assert manager.sync_due()[-1]["source_id"] == "slack"
    assert calls == ["sources.slack_discover"]

    class Revoked(_SlackRuntime):
        def fetch(
            self,
            selection: SourceResourceSelection,
            options: dict[str, object],
            checkpoint: dict[str, object] | None,
        ) -> LiveBatch:
            raise LiveSourceError("source_access_revoked")

    runtime = Revoked(LiveBatch((), {"cursor": "next"}))
    clock = _Clock()
    service = LiveCaptureService(tmp_path / "failed", brain, runtime=runtime, clock=clock)
    service.configure("slack.revoked", _SELECTION, {"date_floor": "2026-09-01T00:00:00Z"})
    service.control("slack.revoked", "enable", interval_seconds=14_400)
    assert service.sync_due()[0]["failure_code"] == "source_access_revoked"

    class Broken(_SlackRuntime):
        def fetch(
            self,
            selection: SourceResourceSelection,
            options: dict[str, object],
            checkpoint: dict[str, object] | None,
        ) -> LiveBatch:
            raise RuntimeError("synthetic capture failure")

    failed = LiveCaptureService(
        tmp_path / "broken",
        brain,
        runtime=Broken(LiveBatch((), {})),
        clock=clock,
    )
    failed.configure("slack.broken", _SELECTION, {"date_floor": "2026-09-01T00:00:00Z"})
    failed.control("slack.broken", "enable", interval_seconds=14_400)
    assert failed.sync_due()[0]["failure_code"] == "source_capture_failed"
