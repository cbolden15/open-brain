from __future__ import annotations

import json
import multiprocessing
import os
import stat
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.capture.models import CaptureWorkItem, QueueErrorCode
from open_brain_engine.core.models import (
    Authority,
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.ports import PutDisposition

from open_brain_legacy.capture.queue import (
    FilesystemCaptureQueue,
    QueueImmutableConflictError,
    QueueWriteError,
    read_pending_queue_snapshot,
)

FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _item(*, why: str = "Synthetic owner context") -> CaptureWorkItem:
    source_url = "https://example.test/synthetic"
    envelope = CaptureEnvelope.create(
        source_type=SourceType.WEB,
        content_kind=ContentKind.ARTICLE,
        source_url=source_url,
        title="Synthetic title",
        shared_text="Synthetic shared text",
        captured_at=FIXED_TIME,
        capture_why=why,
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.SHORTCUT,
        provenance=Provenance.create(
            source_ref=source_url,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=PrivacyDecision.create(
            tier=PrivacyTier.UNKNOWN,
            reason=PrivacyReason.CLASSIFICATION_MISSING,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )
    return CaptureWorkItem.create(envelope=envelope, available_at=FIXED_TIME)


def _enqueue(queue: FilesystemCaptureQueue, item: CaptureWorkItem) -> None:
    result = queue.enqueue(
        item,
        item_id=str(item.envelope.capture_id),
        payload_digest=item.payload_digest_sha256(),
    )
    assert result.disposition is PutDisposition.CREATED


def _claim_in_process(root: str, result: multiprocessing.Queue[bool]) -> None:
    queue = FilesystemCaptureQueue(Path(root), recover_processing=False)
    lease = queue.claim(worker_id=f"worker-{os.getpid()}", now=FIXED_TIME)
    result.put(lease is not None)


def test_q01_process_safe_claim_has_one_winner_and_publishes_only_complete_records(
    tmp_path: Path,
) -> None:
    queue = FilesystemCaptureQueue(tmp_path)
    _enqueue(queue, _item())
    context = multiprocessing.get_context("fork")
    results: multiprocessing.Queue[bool] = context.Queue()
    processes = [
        context.Process(target=_claim_in_process, args=(str(tmp_path), results)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=1) for _ in processes) == [False, True]
    records = tuple((tmp_path / "active").glob("*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_bytes())["state"] == "processing"
    assert not tuple((tmp_path / "active").glob(".*.tmp"))


def test_q02_write_failures_never_publish_partial_claimable_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write

    def short_write(file_descriptor: int, data: bytes | memoryview) -> int:
        original_write(file_descriptor, bytes(data[:1]))
        return 0

    monkeypatch.setattr(os, "write", short_write)
    queue = FilesystemCaptureQueue(tmp_path)

    with pytest.raises(QueueWriteError):
        _enqueue(queue, _item())

    assert queue.claim(worker_id="worker", now=FIXED_TIME) is None
    assert not tuple((tmp_path / "active").glob("*.json"))


@pytest.mark.parametrize("fail_directory", [False, True])
def test_q02_fsync_failures_never_publish_partial_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_directory: bool
) -> None:
    original_fsync = os.fsync

    def fail_selected_fsync(file_descriptor: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(file_descriptor).st_mode)
        if is_directory is fail_directory:
            raise OSError("synthetic fsync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync)
    queue = FilesystemCaptureQueue(tmp_path)
    with pytest.raises(QueueWriteError):
        _enqueue(queue, _item())

    records = tuple((tmp_path / "active").glob("*.json"))
    assert len(records) <= 1
    if records:
        assert json.loads(records[0].read_bytes())["state"] == "pending"


def test_q02_success_fsyncs_file_before_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []
    original_fsync = os.fsync

    def record_fsync(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        observed.append("directory" if stat.S_ISDIR(mode) else "file")
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    queue = FilesystemCaptureQueue(tmp_path)
    _enqueue(queue, _item())

    assert "file" in observed
    assert observed[-1] == "directory"
    assert observed.index("file") < len(observed) - 1


def test_q03_restart_rotates_processing_item_once_and_retains_canonical_context(
    tmp_path: Path,
) -> None:
    queue = FilesystemCaptureQueue(tmp_path)
    item = _item()
    _enqueue(queue, item)
    claimed = queue.claim(worker_id="first-worker", now=FIXED_TIME)
    assert claimed is not None

    FilesystemCaptureQueue(tmp_path)
    repeated_restart = FilesystemCaptureQueue(tmp_path)
    recovered = repeated_restart.claim(worker_id="second-worker", now=FIXED_TIME)
    assert recovered is not None
    assert recovered.item == item
    assert recovered.payload_digest_sha256 == item.payload_digest_sha256()


def test_q04_retry_delays_then_quarantines_on_third_delivery_failure(tmp_path: Path) -> None:
    queue = FilesystemCaptureQueue(tmp_path)
    _enqueue(queue, _item())
    later = FIXED_TIME + timedelta(minutes=1)

    for attempt in range(1, 4):
        lease = queue.claim(worker_id="worker", now=later if attempt > 1 else FIXED_TIME)
        assert lease is not None
        queue.retry(
            lease,
            available_at=later,
            error_code=QueueErrorCode.RETRYABLE_FAILURE.value,
        )
        assert queue.claim(worker_id="early", now=FIXED_TIME) is None

    assert not tuple((tmp_path / "active").glob("*.json"))
    quarantined = tuple((tmp_path / "quarantine").glob("*.json"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_bytes())["state"] == "quarantined"


def test_q05_equal_payload_is_duplicate_and_changed_payload_never_replaces_original(
    tmp_path: Path,
) -> None:
    queue = FilesystemCaptureQueue(tmp_path)
    first = _item()
    _enqueue(queue, first)
    original = next((tmp_path / "active").glob("*.json")).read_bytes()

    duplicate = queue.enqueue(
        first,
        item_id=str(first.envelope.capture_id),
        payload_digest=first.payload_digest_sha256(),
    )
    collision = CaptureWorkItem.create(
        envelope=first.envelope,
        available_at=FIXED_TIME + timedelta(seconds=1),
    )
    with pytest.raises(QueueImmutableConflictError):
        queue.enqueue(
            collision,
            item_id=str(collision.envelope.capture_id),
            payload_digest=collision.payload_digest_sha256(),
        )

    assert duplicate.disposition is PutDisposition.DUPLICATE
    assert next((tmp_path / "active").glob("*.json")).read_bytes() == original


def test_q06_provenance_survives_duplicate_retry_recovery_and_quarantine(tmp_path: Path) -> None:
    queue = FilesystemCaptureQueue(tmp_path)
    item = _item()
    _enqueue(queue, item)
    queue.enqueue(
        item, item_id=str(item.envelope.capture_id), payload_digest=item.payload_digest_sha256()
    )
    lease = queue.claim(worker_id="worker", now=FIXED_TIME)
    assert lease is not None
    queue.retry(
        lease,
        available_at=FIXED_TIME,
        error_code=QueueErrorCode.RETRYABLE_FAILURE.value,
    )
    recovered = FilesystemCaptureQueue(tmp_path).claim(worker_id="worker", now=FIXED_TIME)
    assert recovered is not None
    FilesystemCaptureQueue(tmp_path, recover_processing=False).quarantine(
        recovered,
        at=FIXED_TIME,
        error_code=QueueErrorCode.EXTRACTION_FAILED.value,
    )

    stored = json.loads(next((tmp_path / "quarantine").glob("*.json")).read_bytes())
    envelope = stored["item"]["envelope"]
    assert envelope["capture_id"] == str(item.envelope.capture_id)
    assert envelope["capture_why"] == item.envelope.capture_why
    assert envelope["captured_at"] == "2026-01-02T03:04:05.000000Z"
    assert envelope["source_url"] == item.envelope.source_url
    assert envelope["capture_source"] == item.envelope.capture_source.value


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"not-json-synthetic-private-marker", QueueErrorCode.INVALID_SCHEMA.value),
        (
            b'{"schema_version":1,"marker":"synthetic-private-marker","schema_version":1}',
            QueueErrorCode.INVALID_SCHEMA.value,
        ),
        (
            b'{"schema_version":2,"marker":"synthetic-private-marker"}',
            QueueErrorCode.INVALID_SCHEMA.value,
        ),
        (
            b'{"schema_version":1,"marker":"synthetic-private-marker"}',
            QueueErrorCode.INVALID_ITEM.value,
        ),
        (
            b'{"schema_version":1,"item_id":"cap_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","payload_digest_sha256":"bad","marker":"synthetic-private-marker"}',
            QueueErrorCode.INVALID_DIGEST.value,
        ),
    ],
)
def test_q07_malformed_active_records_are_quarantined_without_body_leaks(
    tmp_path: Path, payload: bytes, expected_code: str
) -> None:
    marker = b"synthetic-private-marker"
    active = tmp_path / "active"
    active.mkdir(parents=True)
    (active / "broken.json").write_bytes(payload)

    FilesystemCaptureQueue(tmp_path)

    assert not tuple(active.glob("*.json"))
    records = tuple((tmp_path / "quarantine").glob("*.json"))
    assert len(records) == 1
    quarantined = records[0].read_bytes()
    assert marker not in quarantined
    assert json.loads(quarantined)["error_code"] == expected_code


def test_q07_invalid_timestamp_is_quarantined_without_body_leak(tmp_path: Path) -> None:
    marker = b"synthetic-private-marker"
    queue = FilesystemCaptureQueue(tmp_path)
    item = _item(why=marker.decode())
    _enqueue(queue, item)
    record_path = next((tmp_path / "active").glob("*.json"))
    record_path.write_bytes(
        record_path.read_bytes().replace(
            b'"available_at":"2026-01-02T03:04:05Z"',
            b'"available_at":"not-a-timestamp"',
        )
    )

    FilesystemCaptureQueue(tmp_path)

    quarantined = next((tmp_path / "quarantine").glob("*.json")).read_bytes()
    assert marker not in quarantined
    assert json.loads(quarantined)["error_code"] == QueueErrorCode.INVALID_ITEM.value


def test_pending_snapshot_reports_age_inputs_without_transitioning_queue_state(
    tmp_path: Path,
) -> None:
    queue = FilesystemCaptureQueue(tmp_path, recover_processing=False)
    _enqueue(queue, _item())

    snapshot = queue.pending_snapshot()

    assert snapshot.pending_count == 1
    assert snapshot.malformed_count == 0
    assert snapshot.oldest_captured_at == FIXED_TIME
    assert len(tuple((tmp_path / "active").glob("*.json"))) == 1
    assert not tuple((tmp_path / "quarantine").glob("*.json"))


def test_pending_snapshot_counts_malformed_records_without_quarantining_them(
    tmp_path: Path,
) -> None:
    queue = FilesystemCaptureQueue(tmp_path, recover_processing=False)
    broken = tmp_path / "active" / "broken.json"
    broken.write_bytes(b"synthetic malformed queue bytes")

    snapshot = queue.pending_snapshot()

    assert snapshot.pending_count == 0
    assert snapshot.malformed_count == 1
    assert snapshot.oldest_captured_at is None
    assert broken.exists()
    assert not tuple((tmp_path / "quarantine").glob("*.json"))


def test_pending_snapshot_never_follows_a_symbolic_link(tmp_path: Path) -> None:
    queue = FilesystemCaptureQueue(tmp_path, recover_processing=False)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"synthetic outside bytes")
    linked = tmp_path / "active" / "linked.json"
    linked.symlink_to(outside)

    snapshot = queue.pending_snapshot()

    assert snapshot.malformed_count == 1
    assert outside.read_bytes() == b"synthetic outside bytes"
    assert linked.is_symlink()


def test_pending_snapshot_reader_does_not_create_an_absent_queue(tmp_path: Path) -> None:
    queue_root = tmp_path / "absent-queue"

    assert read_pending_queue_snapshot(queue_root).pending_count == 0
    assert not queue_root.exists()


def test_capture_work_item_uses_canonical_exact_key_codec_and_closed_codes() -> None:
    item = _item()
    payload = item.canonical_bytes()

    assert CaptureWorkItem.from_canonical_bytes(payload) == item
    assert item.payload_digest_sha256() == sha256(payload).hexdigest()
    with pytest.raises(ValueError):
        CaptureWorkItem.from_canonical_bytes(payload.replace(b'"attempt_count"', b'"extra"'))
