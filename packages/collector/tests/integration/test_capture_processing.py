"""Synthetic processing recovery and admission boundaries."""

from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import PrivacyDecision

from open_brain_collector.capture_contracts import Development, SourceObservation
from open_brain_collector.capture_processing import CaptureProcessingStore, ProcessingPolicy
from open_brain_connectors.runtime.live_common import LiveSourceError, local_source_privacy
from open_brain_connectors.runtime.live_storage import PrivateJsonStore


def observation(**kwargs: Any) -> SourceObservation:
    value = SourceObservation(
        "workspace",
        "conversation",
        "123.4",
        "revision1",
        "2026-10-14",
        "https://example.invalid/thread",
        "Decision: use local\nCommitted: ship",
        "",
        "capture_raw",
        local_source_privacy(),
    )
    data = value.to_dict()
    data.update(kwargs)
    if isinstance(data["privacy"], PrivacyDecision):
        data["privacy"] = data["privacy"].to_dict()
    return SourceObservation.from_dict(data)


def test_two_developments_dual_discovery_and_changed_revision(tmp_path: Path) -> None:
    queue = CaptureProcessingStore(PrivateJsonStore(tmp_path / "queue"))
    queue.admit(observation())
    queue.admit(observation(observed_at="2026-10-15", thread_context="later context"))
    accepted: dict[str, str] = {}

    def capture(development: Development, source: SourceObservation, delivery: str) -> str:
        assert development.quote in source.text
        return accepted.setdefault(delivery, "capture_" + str(len(accepted)))

    assert queue.process(ProcessingPolicy(), capture)["captures"] == 2
    assert queue.process(ProcessingPolicy(), capture)["captures"] == 0
    queue.admit(observation(revision_id="same-material", capture_id="capture_repeat"))
    # Equal semantics still require a new immutable evidence revision.
    assert queue.process(ProcessingPolicy(), capture)["captures"] == 2
    queue.admit(
        observation(
            revision_id="revision2", capture_id="capture_raw2", text="Correction: use remote"
        )
    )
    assert queue.process(ProcessingPolicy(), capture)["captures"] == 1


def test_lost_capture_ack_and_postprocess_restart(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "queue")
    queue = CaptureProcessingStore(store)
    queue.admit(observation(text="Decision: keep evidence"))
    accepted: dict[str, str] = {}
    attempts = []

    def capture(development: Development, source: SourceObservation, delivery: str) -> str:
        attempts.append(delivery)
        result = accepted.setdefault(delivery, "capture_derived")
        if len(attempts) == 1:
            raise RuntimeError("synthetic lost acknowledgement")
        return result

    failed = queue.process(ProcessingPolicy(), capture)
    assert failed["failed"] == 1
    assert failed["failure_codes"] == ["processing_item_failed"]
    completed = []
    restarted = CaptureProcessingStore(store)
    assert (
        restarted.process(
            ProcessingPolicy(), capture, postprocess=lambda d, c, op: completed.append(op)
        )["captures"]
        == 1
    )
    assert attempts[0] == attempts[1]
    assert len(accepted) == len(completed) == 1


def test_unknown_excluded_and_external_never_extract(tmp_path: Path) -> None:
    queue = CaptureProcessingStore(PrivateJsonStore(tmp_path / "queue"))
    unknown = PrivacyDecision.from_dict(
        {**local_source_privacy().to_dict(), "tier": "unknown", "reason": "classification_missing"}
    )
    queue.admit(observation(ai_excluded=True))
    queue.admit(observation(message_ts="2", privacy=unknown))
    calls: list[SourceObservation] = []

    def never_extract(source: SourceObservation) -> tuple[Development, ...]:
        calls.append(source)
        pytest.fail("excluded source reached extractor")

    policy = ProcessingPolicy(provider="synthetic", external_processing=True)
    assert queue.process(policy, lambda d, o, k: "capture", extractor=never_extract)["held"] == 2
    assert calls == []


def test_capacity_budget_and_revision_conflict(tmp_path: Path) -> None:
    queue = CaptureProcessingStore(PrivateJsonStore(tmp_path / "queue"), max_observations=1)
    queue.admit(observation())
    with pytest.raises(LiveSourceError, match="processing_queue_full"):
        queue.admit(observation(message_ts="2"))
    with pytest.raises(LiveSourceError, match="processing_revision_conflict"):
        queue.admit(observation(text="Changed without revision"))
    assert (
        queue.process(ProcessingPolicy(max_input_bytes=1), lambda d, o, k: "capture")[
            "budget_exhausted"
        ]
        is True
    )
    assert queue.process(ProcessingPolicy(), lambda d, o, k: "capture")["captures"] == 2


def test_postprocess_capacity_keeps_receipts_and_continues_independent_work(tmp_path: Path) -> None:
    queue = CaptureProcessingStore(PrivateJsonStore(tmp_path / "queue"))
    queue.admit(observation(text="Decision: blocked"))
    queue.admit(observation(message_ts="2", text="Decision: independent"))
    calls: list[str] = []
    blocked = True

    def capture(development: Development, source: SourceObservation, operation: str) -> str:
        calls.append(operation)
        return "capture_" + operation

    def postprocess(development: Development, capture_id: str, operation: str) -> None:
        if blocked and development.claim == "blocked":
            raise LiveSourceError("attention_weekly_limit")

    result = queue.process(ProcessingPolicy(), capture, postprocess=postprocess)
    assert result["captures"] == 2
    assert result["processed"] == 1
    assert result["outcome"] == "partial"
    assert result["failure_codes"] == ["attention_weekly_limit"]
    blocked = False
    assert queue.process(ProcessingPolicy(), capture, postprocess=postprocess)["processed"] == 1
    assert len(calls) == 2
