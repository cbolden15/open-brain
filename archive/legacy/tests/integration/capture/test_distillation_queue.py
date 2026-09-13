from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from open_brain_engine.capture.models import DistillationWorkItem, QueueErrorCode
from open_brain_engine.core.ports import PutDisposition

from open_brain_legacy.capture.queue import (
    FilesystemDistillationQueue,
    QueueImmutableConflictError,
)

FIXED_TIME = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _item(*, event_id: str = "event-synthetic-001", digest: str = "a" * 64) -> DistillationWorkItem:
    return DistillationWorkItem.create(
        capture_id="cap_" + "1" * 64,
        event_id=event_id,
        redacted_event_digest_sha256=digest,
    )


def _enqueue(queue: FilesystemDistillationQueue, item: DistillationWorkItem) -> None:
    result = queue.enqueue(
        item,
        item_id=item.event_id,
        payload_digest=item.payload_digest_sha256(),
    )
    assert result.disposition is PutDisposition.CREATED


def test_distillation_queue_recovers_claim_and_replays_exact_item(tmp_path: Path) -> None:
    root = tmp_path / "distillation"
    item = _item()
    queue = FilesystemDistillationQueue(root)
    _enqueue(queue, item)

    lease = queue.claim(worker_id="worker-001", now=FIXED_TIME)
    assert lease is not None and lease.item == item

    recovered = FilesystemDistillationQueue(root)
    replay = recovered.claim(worker_id="worker-002", now=FIXED_TIME)
    assert replay is not None and replay.item == item
    recovered.acknowledge(replay, completed_at=FIXED_TIME)

    assert recovered.claim(worker_id="worker-003", now=FIXED_TIME) is None


def test_distillation_queue_retry_delay_conflict_and_exhaustion_are_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "distillation"
    queue = FilesystemDistillationQueue(root)
    item = _item()
    _enqueue(queue, item)

    with pytest.raises(QueueImmutableConflictError, match="immutable"):
        conflicting = _item(digest="b" * 64)
        queue.enqueue(
            conflicting,
            item_id=conflicting.event_id,
            payload_digest=conflicting.payload_digest_sha256(),
        )

    current_time = FIXED_TIME
    for _ in range(3):
        lease = queue.claim(worker_id="worker-001", now=current_time)
        assert lease is not None
        current_time += timedelta(minutes=1)
        queue.retry(
            lease,
            available_at=current_time,
            error_code=QueueErrorCode.RETRYABLE_FAILURE.value,
        )

    assert queue.claim(worker_id="worker-001", now=FIXED_TIME + timedelta(days=1)) is None
    assert len(tuple((root / "quarantine").glob("*.json"))) == 1


def test_distillation_queue_quarantines_malformed_active_record_without_exposing_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "distillation"
    queue = FilesystemDistillationQueue(root)
    active = root / "active"
    (active / "malformed.json").write_text('{"schema_version":1,"item":"sensitive"}')

    assert queue.claim(worker_id="worker-001", now=FIXED_TIME) is None
    assert tuple(active.glob("*.json")) == ()
    quarantined = tuple((root / "quarantine").glob("malformed-*.json"))
    assert len(quarantined) == 1
    assert "sensitive" not in quarantined[0].read_text()
