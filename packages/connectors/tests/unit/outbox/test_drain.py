from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier

from open_brain_connectors.outbox import drain as drain_module
from open_brain_connectors.outbox.contracts import (
    DeliveryEnvelope,
    TerminalReceipt,
    TerminalReceiptStatus,
)
from open_brain_connectors.outbox.drain import (
    DRAIN_LEASE_NAME,
    DeliveryFailure,
    DrainResult,
    DrainSummary,
    OutboxDrainError,
    run_drain_cycle,
)
from open_brain_connectors.outbox.store import EnqueueResult, OutboxItemState, OutboxStore

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = derive_brain_id(TENANT_ID)
OTHER_BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000000")
PRINCIPAL_ID = "synthetic-drain-principal"
PAYLOAD_TEXT = "Synthetic drain cycle body"
ENQUEUED_AT = "2026-09-21T12:00:00Z"
NOW = datetime(2026, 9, 21, 12, 30, 0, tzinfo=UTC)
# 1800 seconds before NOW: past the 600-second default stale bound.
STALE_ACQUIRED_AT = "2026-09-21T12:00:00Z"
FRESH_ACQUIRED_AT = "2026-09-21T12:29:30Z"


def _envelope(
    *,
    delivery_id: str = "delivery.drain-001",
    payload_text: str = PAYLOAD_TEXT,
    enqueued_at: str = ENQUEUED_AT,
    retry_age_limit_seconds: int = 86400,
    retry_attempt_limit: int = 8,
    attempts: int = 0,
) -> DeliveryEnvelope:
    envelope = DeliveryEnvelope.create(
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        delivery_id=delivery_id,
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload={"family": "text", "text": payload_text},
        enqueued_at=enqueued_at,
        retry_age_limit_seconds=retry_age_limit_seconds,
        retry_attempt_limit=retry_attempt_limit,
    )
    if attempts:
        values = envelope.to_dict()
        values["attempts"] = attempts
        values["last_attempt_at"] = "2026-09-21T12:10:00Z"
        values["last_attempt_result"] = "rate_limited"
        envelope = DeliveryEnvelope.from_dict(values)
    return envelope


def _receipt(
    envelope: DeliveryEnvelope, *, status: TerminalReceiptStatus = TerminalReceiptStatus.ACCEPTED
) -> TerminalReceipt:
    return TerminalReceipt(
        status=status,
        brain_id=envelope.destination_brain_id,
        issuer_epoch=envelope.expected_issuer_epoch,
        delivery_id=envelope.delivery_id,
        request_digest=envelope.request_digest,
        final_admitted_tier=envelope.requested_tier,
    )


def _foreign_receipt(
    envelope: DeliveryEnvelope, *, brain_id: str | None = None, issuer_epoch: int | None = None
) -> TerminalReceipt:
    return TerminalReceipt(
        status=TerminalReceiptStatus.ACCEPTED,
        brain_id=brain_id if brain_id is not None else envelope.destination_brain_id,
        issuer_epoch=issuer_epoch if issuer_epoch is not None else envelope.expected_issuer_epoch,
        delivery_id=envelope.delivery_id,
        request_digest=envelope.request_digest,
        final_admitted_tier=envelope.requested_tier,
    )


def _store(tmp_path: Path) -> OutboxStore:
    return OutboxStore(tmp_path / "outbox", max_items=64, max_bytes=1024 * 1024)


def _clock(at: datetime = NOW) -> Callable[[], datetime]:
    def tick() -> datetime:
        return at

    return tick


def _drain(
    store: OutboxStore,
    transport: drain_module.Transport,
    *,
    max_batch_items: int = 16,
    max_batch_bytes: int = 1024 * 1024,
    clock: Callable[[], datetime] | None = None,
) -> DrainSummary:
    return run_drain_cycle(
        store,
        transport,
        max_batch_items=max_batch_items,
        max_batch_bytes=max_batch_bytes,
        clock=clock if clock is not None else _clock(),
    )


class _FakeTransport:
    """Scripted per-delivery transport; records every delivery it receives."""

    def __init__(
        self,
        outcomes: dict[str, TerminalReceipt | DeliveryFailure | Exception] | None = None,
    ) -> None:
        self._outcomes = dict(outcomes or {})
        self.seen: list[str] = []

    def __call__(self, envelope: DeliveryEnvelope) -> TerminalReceipt | DeliveryFailure:
        self.seen.append(envelope.delivery_id)
        scripted = self._outcomes.get(envelope.delivery_id)
        if scripted is None:
            return _receipt(envelope)
        if isinstance(scripted, Exception):
            raise scripted
        return scripted


def _item_path(store: OutboxStore, delivery_id: str) -> Path:
    return store.directory / f"{delivery_id}.json"


def _item_json(raw: bytes) -> dict[str, object]:
    return cast(dict[str, object], json.loads(raw.decode("utf-8")))


def _write_lease_marker(store: OutboxStore, *, pid: int, acquired_at: str) -> None:
    payload = json.dumps(
        {
            "acquired_at": acquired_at,
            "lease_version": drain_module.DRAIN_LEASE_VERSION,
            "pid": pid,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (store.directory / DRAIN_LEASE_NAME).write_bytes(payload)


def _dead_pid() -> int:
    """Spawn and reap a real child so the pid is provably not alive."""
    for _ in range(10):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        try:
            os.kill(child.pid, 0)
        except ProcessLookupError:
            return child.pid
    pytest.fail("could not obtain a dead pid")


def test_cycle_without_items_completes_with_zero_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)

    summary = _drain(store, _FakeTransport())

    assert summary.result is DrainResult.COMPLETED
    assert summary.stale_lease_reclaimed is False
    assert summary.admitted_items == 0
    assert summary.delivery_attempts == 0
    assert summary.batch_too_large_items == 0
    assert not (store.directory / DRAIN_LEASE_NAME).exists()


def test_accepted_receipt_marks_terminal_removes_body_and_releases_lease(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.result is DrainResult.COMPLETED
    assert summary.admitted_items == 1
    assert summary.delivery_attempts == 1
    assert summary.accepted == 1
    assert summary.duplicate == 0
    assert fake.seen == [envelope.delivery_id]
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") not in raw
    record = _item_json(raw)
    assert record["record_version"] == "outbox.terminal.v1"
    assert record["terminal_kind"] == "receipt"
    assert not (store.directory / DRAIN_LEASE_NAME).exists()


def test_duplicate_receipt_marks_terminal_and_counts_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    duplicate = _receipt(envelope, status=TerminalReceiptStatus.DUPLICATE)
    fake = _FakeTransport({envelope.delivery_id: duplicate})

    summary = _drain(store, fake)

    assert summary.duplicate == 1
    assert summary.accepted == 0
    record = _item_json(_item_path(store, envelope.delivery_id).read_bytes())
    assert record["receipt"] == duplicate.to_dict()
    assert PAYLOAD_TEXT.encode("utf-8") not in _item_path(store, envelope.delivery_id).read_bytes()


def test_queued_custody_receipt_marks_terminal_without_consuming_retry_attempt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _envelope(attempts=2)
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    custody = _receipt(envelope, status=TerminalReceiptStatus.QUEUED)

    summary = _drain(store, _FakeTransport({envelope.delivery_id: custody}))

    assert summary.accepted == 1
    assert summary.duplicate == 0
    record = _item_json(_item_path(store, envelope.delivery_id).read_bytes())
    assert record["attempts"] == 2
    assert record["receipt"] == custody.to_dict()
    assert PAYLOAD_TEXT.encode("utf-8") not in _item_path(store, envelope.delivery_id).read_bytes()


def test_second_concurrent_drain_returns_drain_busy_without_touching_items(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    before = _item_path(store, envelope.delivery_id).read_bytes()
    nested: dict[str, DrainSummary] = {}
    observed: list[bytes] = []

    def holding_transport(item: DeliveryEnvelope) -> TerminalReceipt:
        nested["summary"] = _drain(store, _FakeTransport())
        observed.append(_item_path(store, envelope.delivery_id).read_bytes())
        return _receipt(item)

    summary = _drain(store, holding_transport)

    assert summary.result is DrainResult.COMPLETED
    assert summary.accepted == 1
    assert nested["summary"].result is DrainResult.DRAIN_BUSY
    assert nested["summary"].stale_lease_reclaimed is False
    assert nested["summary"].admitted_items == 0
    assert nested["summary"].delivery_attempts == 0
    assert observed == [before]
    assert store.status().terminal_items == 1


def test_fresh_marker_with_live_pid_blocks_drain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED
    _write_lease_marker(store, pid=os.getpid(), acquired_at=FRESH_ACQUIRED_AT)
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.result is DrainResult.DRAIN_BUSY
    assert summary.stale_lease_reclaimed is False
    assert fake.seen == []
    assert store.status().queued_items == 1
    assert (store.directory / DRAIN_LEASE_NAME).exists()


def test_stale_marker_with_dead_pid_is_reclaimed_and_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    _write_lease_marker(store, pid=_dead_pid(), acquired_at=STALE_ACQUIRED_AT)

    summary = _drain(store, _FakeTransport())

    assert summary.result is DrainResult.COMPLETED
    assert summary.stale_lease_reclaimed is True
    assert summary.accepted == 1
    assert not (store.directory / DRAIN_LEASE_NAME).exists()


def test_stale_marker_with_live_pid_is_not_reclaimed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED
    marker = store.directory / DRAIN_LEASE_NAME
    _write_lease_marker(store, pid=os.getpid(), acquired_at=STALE_ACQUIRED_AT)
    before = marker.read_bytes()
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.result is DrainResult.DRAIN_BUSY
    assert summary.stale_lease_reclaimed is False
    assert fake.seen == []
    assert marker.read_bytes() == before


def test_fresh_marker_with_dead_pid_stays_busy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED
    _write_lease_marker(store, pid=_dead_pid(), acquired_at=FRESH_ACQUIRED_AT)
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.result is DrainResult.DRAIN_BUSY
    assert summary.stale_lease_reclaimed is False
    assert fake.seen == []


def test_unparseable_marker_is_busy_and_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED
    marker = store.directory / DRAIN_LEASE_NAME
    marker.write_bytes(b"not a lease")
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.result is DrainResult.DRAIN_BUSY
    assert summary.stale_lease_reclaimed is False
    assert fake.seen == []
    assert marker.read_bytes() == b"not a lease"


def test_order_is_enqueued_at_then_delivery_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inserted = (
        ("delivery.d-bb", "2026-09-21T12:00:00Z"),
        ("delivery.d-zz", "2026-09-21T11:00:00Z"),
        ("delivery.d-aa", "2026-09-21T11:00:00Z"),
    )
    for delivery_id, enqueued_at in inserted:
        assert store.enqueue(_envelope(delivery_id=delivery_id, enqueued_at=enqueued_at)) is (
            EnqueueResult.QUEUED
        )
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert fake.seen == ["delivery.d-aa", "delivery.d-zz", "delivery.d-bb"]
    assert summary.accepted == 3


def test_age_exhausted_at_exact_limit_quarantines_before_any_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope(retry_age_limit_seconds=1800)
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.quarantined_age_exhausted == 1
    assert summary.delivery_attempts == 0
    assert fake.seen == []
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "age_exhausted"
    assert store.status().quarantined_items == 1


def test_age_one_second_under_limit_is_still_delivered(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope(retry_age_limit_seconds=1801)) is EnqueueResult.QUEUED
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.accepted == 1
    assert summary.quarantined_age_exhausted == 0


def test_attempts_exhausted_at_exact_limit_quarantines_before_any_attempt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    envelope = _envelope(retry_attempt_limit=2, attempts=2)
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    fake = _FakeTransport()

    summary = _drain(store, fake)

    assert summary.quarantined_attempts_exhausted == 1
    assert summary.delivery_attempts == 0
    assert fake.seen == []
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "attempts_exhausted"


def test_batch_item_bound_stops_after_last_admitted_item(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for suffix in ("001", "002", "003"):
        assert store.enqueue(_envelope(delivery_id=f"delivery.drain-{suffix}")) is (
            EnqueueResult.QUEUED
        )
    third_before = _item_path(store, "delivery.drain-003").read_bytes()
    fake = _FakeTransport()

    summary = _drain(store, fake, max_batch_items=2)

    assert fake.seen == ["delivery.drain-001", "delivery.drain-002"]
    assert summary.delivery_attempts == 2
    assert summary.accepted == 2
    assert summary.batch_too_large_items == 1
    assert _item_path(store, "delivery.drain-003").read_bytes() == third_before
    assert store.status().queued_items == 1


def test_batch_byte_bound_admits_exact_fit_and_defers_overflow(tmp_path: Path) -> None:
    exact = _store(tmp_path / "exact")
    assert exact.enqueue(
        _envelope(delivery_id="delivery.drain-001", payload_text="First body")
    ) is (EnqueueResult.QUEUED)
    assert exact.enqueue(_envelope(delivery_id="delivery.drain-002", payload_text="Second")) is (
        EnqueueResult.QUEUED
    )
    first_size = _item_path(exact, "delivery.drain-001").stat().st_size
    second_size = _item_path(exact, "delivery.drain-002").stat().st_size

    exact_summary = _drain(exact, _FakeTransport(), max_batch_bytes=first_size + second_size)
    tight = _store(tmp_path / "tight")
    assert tight.enqueue(
        _envelope(delivery_id="delivery.drain-001", payload_text="First body")
    ) is (EnqueueResult.QUEUED)
    assert tight.enqueue(_envelope(delivery_id="delivery.drain-002", payload_text="Second")) is (
        EnqueueResult.QUEUED
    )
    second_before = _item_path(tight, "delivery.drain-002").read_bytes()
    fake = _FakeTransport()

    tight_summary = _drain(tight, fake, max_batch_bytes=first_size + second_size - 1)

    assert exact_summary.batch_too_large_items == 0
    assert exact_summary.accepted == 2
    assert tight_summary.batch_too_large_items == 1
    assert tight_summary.accepted == 1
    assert fake.seen == ["delivery.drain-001"]
    assert _item_path(tight, "delivery.drain-002").read_bytes() == second_before


def test_retryable_failure_records_attempt_and_retains_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    refusal = DeliveryFailure(code="rate_limited", retryable=True)
    fake = _FakeTransport({envelope.delivery_id: refusal})

    summary = _drain(store, fake)

    assert summary.retried == 1
    assert summary.quarantined_refused == 0
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    value = _item_json(raw)
    assert value["attempts"] == 1
    assert value["last_attempt_result"] == "rate_limited"
    assert value["quarantine_reason"] is None
    assert store.status().queued_items == 1


def test_terminal_failure_quarantines_under_failure_code(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    refusal = DeliveryFailure(code="tier_not_permitted", retryable=False)
    fake = _FakeTransport({envelope.delivery_id: refusal})

    summary = _drain(store, fake)

    assert summary.quarantined_refused == 1
    assert summary.retried == 0
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "tier_not_permitted"
    assert store.status().quarantined_items == 1


def test_mismatched_brain_receipt_quarantines_without_body_removal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    wrong_brain = _foreign_receipt(envelope, brain_id=OTHER_BRAIN_ID)
    fake = _FakeTransport({envelope.delivery_id: wrong_brain})

    summary = _drain(store, fake)

    assert summary.quarantined_receipt_mismatch == 1
    assert summary.accepted == 0
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert b'"payload"' in raw
    assert _item_json(raw)["quarantine_reason"] == "receipt_mismatch"


def test_stale_epoch_receipt_quarantines_without_body_removal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    envelope = _envelope()
    assert store.enqueue(envelope) is EnqueueResult.QUEUED
    stale_epoch = _foreign_receipt(envelope, issuer_epoch=envelope.expected_issuer_epoch - 1)
    fake = _FakeTransport({envelope.delivery_id: stale_epoch})

    summary = _drain(store, fake)

    assert summary.quarantined_receipt_mismatch == 1
    assert summary.accepted == 0
    raw = _item_path(store, envelope.delivery_id).read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    assert _item_json(raw)["quarantine_reason"] == "receipt_mismatch"


def test_transport_exception_is_recorded_and_cycle_continues(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for suffix in ("001", "002", "003"):
        assert store.enqueue(_envelope(delivery_id=f"delivery.drain-{suffix}")) is (
            EnqueueResult.QUEUED
        )
    fake = _FakeTransport({"delivery.drain-002": RuntimeError("synthetic transport crash")})

    summary = _drain(store, fake)

    assert summary.result is DrainResult.COMPLETED
    assert summary.delivery_attempts == 3
    assert summary.accepted == 2
    assert summary.transport_errors == 1
    assert summary.quarantined_refused == 0
    assert fake.seen == ["delivery.drain-001", "delivery.drain-002", "delivery.drain-003"]
    raw = _item_path(store, "delivery.drain-002").read_bytes()
    assert PAYLOAD_TEXT.encode("utf-8") in raw
    value = _item_json(raw)
    assert value["attempts"] == 1
    assert value["last_attempt_result"] == "transport_error"
    assert value["quarantine_reason"] is None
    assert [item.state for item in store.scan()] == [
        OutboxItemState.TERMINAL,
        OutboxItemState.QUEUED,
        OutboxItemState.TERMINAL,
    ]


@pytest.mark.parametrize("bound", ["items", "bytes", "stale_lease"])
def test_invalid_cycle_bounds_are_refused(tmp_path: Path, bound: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(OutboxDrainError):
        if bound == "items":
            _drain(store, _FakeTransport(), max_batch_items=0)
        elif bound == "bytes":
            _drain(store, _FakeTransport(), max_batch_bytes=0)
        else:
            run_drain_cycle(
                store,
                _FakeTransport(),
                max_batch_items=4,
                max_batch_bytes=4096,
                stale_lease_after_seconds=0,
                clock=_clock(),
            )
