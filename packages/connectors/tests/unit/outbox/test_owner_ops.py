from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier

from open_brain_connectors.outbox.contracts import DeliveryEnvelope
from open_brain_connectors.outbox.owner_ops import (
    OwnerOperationError,
    owner_convert,
    owner_discard,
    owner_retry,
)
from open_brain_connectors.outbox.store import (
    EnqueueResult,
    OutboxItemState,
    OutboxStore,
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = derive_brain_id(TENANT_ID)
OTHER_BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000000")
PRINCIPAL_ID = "synthetic-owner-ops-principal"
PAYLOAD_TEXT = "Synthetic owner-operations outbox capture"
ENQUEUED_AT = "2026-09-21T12:00:00Z"
ATTEMPTED_AT = "2026-09-21T12:10:00Z"
RETRY_AT = "2026-09-21T12:30:00Z"


def _envelope(*, delivery_id: str = "delivery.owner-001") -> DeliveryEnvelope:
    return DeliveryEnvelope.create(
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=7,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        delivery_id=delivery_id,
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload={"family": "text", "text": PAYLOAD_TEXT},
        enqueued_at=ENQUEUED_AT,
        retry_age_limit_seconds=86400,
        retry_attempt_limit=8,
    )


def _store(tmp_path: Path) -> OutboxStore:
    return OutboxStore(tmp_path / "outbox", max_items=16, max_bytes=1_048_576)


def _quarantined(store: OutboxStore, *, delivery_id: str = "delivery.owner-001") -> None:
    assert store.enqueue(_envelope(delivery_id=delivery_id)) is EnqueueResult.QUEUED
    store.record_attempt(delivery_id, attempted_at=ATTEMPTED_AT, result="rate_limited")
    store.quarantine(delivery_id, reason="attempts_exhausted")


def _read_envelope(store: OutboxStore, delivery_id: str) -> DeliveryEnvelope:
    raw = (store.directory / f"{delivery_id}.json").read_bytes()
    return DeliveryEnvelope.from_dict(json.loads(raw.decode("utf-8")))


def test_owner_retry_moves_a_quarantined_item_back_to_queued(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _quarantined(store)
    before = _read_envelope(store, "delivery.owner-001")

    item = owner_retry(store, "delivery.owner-001", now=RETRY_AT)

    assert item.state is OutboxItemState.QUEUED
    assert item.quarantine_reason is None
    after = _read_envelope(store, "delivery.owner-001")
    assert after.quarantine_reason is None
    # Preserved identity: same delivery ID, destination, digest, enqueue time.
    assert after.delivery_id == before.delivery_id
    assert after.destination_brain_id == before.destination_brain_id
    assert after.request_digest == before.request_digest
    assert after.enqueued_at == before.enqueued_at
    # Prior attempts stay in the existing attempt fields (no prior_windows slot).
    assert after.attempts == before.attempts == 1
    assert after.last_attempt_at == before.last_attempt_at == ATTEMPTED_AT
    assert after.last_attempt_result == before.last_attempt_result == "rate_limited"
    # New bounded window anchored at the retry time: 1800 elapsed seconds plus
    # a fresh age window the size of the original, and a fresh attempt
    # allowance on top of the preserved attempts.
    assert after.retry_age_limit_seconds == 1800 + 86400
    assert after.retry_attempt_limit == 1 + 8


def test_owner_retry_refuses_items_that_are_not_quarantined(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED

    with pytest.raises(OwnerOperationError):
        owner_retry(store, "delivery.owner-001", now=RETRY_AT)


def test_owner_retry_refuses_missing_items(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(OwnerOperationError):
        owner_retry(store, "delivery.absent-001", now=RETRY_AT)


def test_owner_discard_refuses_without_confirmation_and_keeps_the_body(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _quarantined(store)

    with pytest.raises(OwnerOperationError):
        owner_discard(store, "delivery.owner-001", confirm=False, now=RETRY_AT)

    states = {item.delivery_id: item.state for item in store.scan()}
    assert states["delivery.owner-001"] is OutboxItemState.QUARANTINED
    # The body is still a full envelope, not a metadata-only record.
    assert _read_envelope(store, "delivery.owner-001").payload is not None


def test_owner_discard_with_confirmation_leaves_a_terminal_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _quarantined(store)

    item = owner_discard(store, "delivery.owner-001", confirm=True, now=RETRY_AT)

    assert item.state is OutboxItemState.TERMINAL
    states = {item.delivery_id: item.state for item in store.scan()}
    assert states["delivery.owner-001"] is OutboxItemState.TERMINAL
    raw = (store.directory / "delivery.owner-001.json").read_bytes()
    assert b"text" not in raw and PAYLOAD_TEXT.encode("utf-8") not in raw
    record = json.loads(raw.decode("utf-8"))
    assert record["terminal_kind"] == "discard"
    assert record["terminal_at"] == RETRY_AT


def test_owner_convert_creates_a_new_envelope_with_lineage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _quarantined(store)
    original = _read_envelope(store, "delivery.owner-001")

    conversion = owner_convert(
        store,
        "delivery.owner-001",
        destination_brain_id=OTHER_BRAIN_ID,
        issuer_epoch=9,
        now=RETRY_AT,
        new_delivery_id="delivery.converted-001",
    )

    assert conversion.original_delivery_id == "delivery.owner-001"
    assert conversion.new_delivery_id == "delivery.converted-001"
    assert conversion.result is EnqueueResult.QUEUED
    # The original stays quarantined and untouched.
    survivor = _read_envelope(store, "delivery.owner-001")
    assert survivor.quarantine_reason == original.quarantine_reason
    assert survivor.to_dict() == original.to_dict()
    # The new envelope is immutable-fresh: new ID, destination, digest, and
    # lineage back to the original, with the same payload and request fields.
    converted = _read_envelope(store, "delivery.converted-001")
    assert converted.destination_brain_id == OTHER_BRAIN_ID
    assert converted.expected_issuer_epoch == 9
    assert converted.lineage_delivery_id == "delivery.owner-001"
    assert converted.request_digest != original.request_digest
    assert converted.tenant_id == original.tenant_id
    assert converted.principal_id == original.principal_id
    assert converted.requested_tier == original.requested_tier
    assert converted.payload == original.payload
    assert converted.enqueued_at == RETRY_AT
    states = {item.delivery_id: item.state for item in store.scan()}
    assert states["delivery.owner-001"] is OutboxItemState.QUARANTINED
    assert states["delivery.converted-001"] is OutboxItemState.QUEUED


def test_owner_convert_mints_a_fresh_delivery_id_when_none_is_supplied(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _quarantined(store)

    conversion = owner_convert(
        store,
        "delivery.owner-001",
        destination_brain_id=OTHER_BRAIN_ID,
        issuer_epoch=9,
        now=RETRY_AT,
    )

    assert conversion.new_delivery_id != "delivery.owner-001"
    assert conversion.result is EnqueueResult.QUEUED
    assert (store.directory / f"{conversion.new_delivery_id}.json").exists()


def test_owner_convert_refuses_items_that_are_not_quarantined(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.enqueue(_envelope()) is EnqueueResult.QUEUED

    with pytest.raises(OwnerOperationError):
        owner_convert(
            store,
            "delivery.owner-001",
            destination_brain_id=OTHER_BRAIN_ID,
            issuer_epoch=9,
            now=RETRY_AT,
        )
