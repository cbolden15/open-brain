from __future__ import annotations

from datetime import UTC, datetime

from open_brain_engine.capture.models import CaptureWorkItem
from open_brain_engine.core.ports import PutDisposition, PutResult

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.capture import capture_stdin, capture_text

FIXED_TIME = datetime(2026, 8, 14, 12, tzinfo=UTC)


class CaptureQueueFake:
    def __init__(self) -> None:
        self.items: list[CaptureWorkItem] = []

    def enqueue(self, item: CaptureWorkItem, *, item_id: str, payload_digest: str) -> PutResult:
        assert item_id == str(item.envelope.capture_id)
        assert payload_digest == item.payload_digest_sha256()
        self.items.append(item)
        return PutResult(PutDisposition.CREATED, item_id, payload_digest)


def test_capture_text_normalizes_stdin_and_preserves_provenance_and_reason() -> None:
    queue = CaptureQueueFake()

    result = capture_stdin(
        queue=queue,
        now=FIXED_TIME,
        text="Cafe\u0301\r\nsynthetic capture",
        why="Use this synthetic reference",
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "capture_id": result.envelope["capture_id"],
        "command": "capture",
        "dry_run": False,
        "privacy_tier": "unknown",
        "source_type": "text",
        "status": "queued",
    }
    assert len(queue.items) == 1
    capture = queue.items[0].envelope
    assert capture.shared_text == "Caf\u00e9\nsynthetic capture"
    assert capture.capture_why == "Use this synthetic reference"
    assert capture.provenance.to_dict() == {
        "content_origin": "owner_authored",
        "owner_context": "owner_authored",
        "source_ref": "urn:open-brain:text:sha256:"
        "379a3e3c8820bcdeaa9487c9fa6100e9352a6521ca12f8b3f6ae3f947bb6d733",
    }
    assert "synthetic capture" not in result.to_json()
    assert "Use this synthetic reference" not in result.to_json()


def test_capture_dry_run_preserves_deterministic_id_without_writing() -> None:
    queue = CaptureQueueFake()

    first = capture_text(
        queue=queue,
        now=FIXED_TIME,
        text="Synthetic capture",
        why="Keep synthetic capture",
        dry_run=True,
    )
    second = capture_text(
        queue=queue,
        now=FIXED_TIME,
        text="Synthetic capture",
        why="Keep synthetic capture",
        dry_run=True,
    )

    assert first.exit_code is ExitCode.SUCCESS
    assert first.envelope["status"] == "planned"
    assert first.envelope["capture_id"] == second.envelope["capture_id"]
    assert queue.items == []


def test_capture_text_accepts_explicit_work_classification_without_granting_egress() -> None:
    queue = CaptureQueueFake()

    result = capture_text(
        queue=queue,
        now=FIXED_TIME,
        text="Synthetic classified capture",
        why="Keep the classified synthetic capture",
        privacy_tier="work",
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope["privacy_tier"] == "work"
    decision = queue.items[0].envelope.privacy_decision
    assert decision.reason.value == "policy_work"
    assert decision.authority.cloud is False
    assert decision.authority.external_egress is False
