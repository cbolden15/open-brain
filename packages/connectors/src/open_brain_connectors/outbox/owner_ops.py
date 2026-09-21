"""Owner territory: explicit retry, confirmed discard, and conversion.

Quarantined items stop automatic retries (O3) and are resolved only here.
Every operation is foreground, synchronous, and metadata-only in what it
returns; refusal messages are fixed strings that never echo item content.

``owner_retry`` moves a quarantined item back to queued under a new bounded
window anchored at the retry time while preserving the delivery ID,
destination, request digest, and enqueue time. The O1 ``DeliveryEnvelope``
has no ``prior_windows`` slot, so prior attempts stay recorded in the
existing ``attempts`` / ``last_attempt_at`` / ``last_attempt_result``
fields and the fresh window extends the bounds instead of resetting the
counters: the new age limit is the elapsed age plus one fresh age window,
and the new attempt limit is the preserved attempts plus one fresh attempt
allowance. That limitation is reported to the milestone contract; the
identity fields the contract names are preserved exactly.

``owner_discard`` refuses without ``confirm=True`` and otherwise delegates
to the store's terminal discard, which replaces the body with a
metadata-only terminal record.

``owner_convert`` never edits the quarantined original: it enqueues a new
immutable envelope with a fresh delivery ID, the requested destination and
epoch, the same payload, tenant, principal, requested tier, and policy, a
recomputed request digest, ``lineage_delivery_id`` pointing at the
original, and the original's window bounds restarted from the conversion
time. Both IDs are returned.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .contracts import DeliveryEnvelope, OutboxContractError
from .store import (
    EnqueueResult,
    OutboxItem,
    OutboxItemState,
    OutboxStore,
    _canonical_bytes,
    _durable_write,
    _item_name,
)

__all__ = [
    "ConversionResult",
    "OwnerOperationError",
    "owner_convert",
    "owner_discard",
    "owner_retry",
]

_MAX_WINDOW = 2**31 - 1


class OwnerOperationError(Exception):
    """An owner operation refused a request without exposing item content."""


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """Both delivery IDs of one explicit destination conversion."""

    original_delivery_id: str
    new_delivery_id: str
    result: EnqueueResult


def owner_retry(store: OutboxStore, delivery_id: str, *, now: str) -> OutboxItem:
    """Move one quarantined item back to queued under a new bounded window."""
    envelope = _require_quarantined(store, delivery_id)
    retried_at = _parse_timestamp(now, "retry time")
    enqueued_at = _parse_timestamp(envelope.enqueued_at, "enqueue time")
    elapsed_seconds = int((retried_at - enqueued_at).total_seconds())
    new_age_limit = elapsed_seconds + envelope.retry_age_limit_seconds
    new_attempt_limit = envelope.attempts + envelope.retry_attempt_limit
    if new_age_limit > _MAX_WINDOW or new_attempt_limit > _MAX_WINDOW:
        raise OwnerOperationError("owner retry window bound exceeded")
    values = envelope.to_dict()
    values["quarantine_reason"] = None
    values["retry_age_limit_seconds"] = new_age_limit
    values["retry_attempt_limit"] = new_attempt_limit
    try:
        updated = DeliveryEnvelope.from_dict(values)
    except OutboxContractError as error:
        raise OwnerOperationError("owner retry window invalid") from error
    return _replace(store, updated)


def owner_discard(store: OutboxStore, delivery_id: str, *, confirm: bool, now: str) -> OutboxItem:
    """Discard one item terminally; only an explicit confirmation may pass."""
    if confirm is not True:
        raise OwnerOperationError("owner discard requires confirmation")
    return store.terminal_discard(delivery_id, discarded_at=now)


def owner_convert(
    store: OutboxStore,
    delivery_id: str,
    *,
    destination_brain_id: str,
    issuer_epoch: int,
    now: str,
    new_delivery_id: str | None = None,
) -> ConversionResult:
    """Enqueue a fresh destination-bound envelope; leave the original quarantined."""
    envelope = _require_quarantined(store, delivery_id)
    fresh_id = str(uuid.uuid4()) if new_delivery_id is None else new_delivery_id
    payload = envelope.payload
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"family", "text"}
        or payload["family"] != "text"
        or not isinstance(payload["text"], str)
    ):
        raise OwnerOperationError("owner conversion payload invalid")
    try:
        converted = DeliveryEnvelope.create(
            destination_brain_id=destination_brain_id,
            expected_issuer_epoch=issuer_epoch,
            tenant_id=envelope.tenant_id,
            principal_id=envelope.principal_id,
            delivery_id=fresh_id,
            requested_tier=envelope.requested_tier,
            policy_ref=envelope.policy_ref,
            payload={"family": "text", "text": payload["text"]},
            enqueued_at=_parse_timestamp(now, "conversion time").strftime("%Y-%m-%dT%H:%M:%SZ"),
            retry_age_limit_seconds=envelope.retry_age_limit_seconds,
            retry_attempt_limit=envelope.retry_attempt_limit,
            lineage_delivery_id=envelope.delivery_id,
        )
    except OutboxContractError as error:
        raise OwnerOperationError("owner conversion envelope invalid") from error
    return ConversionResult(
        original_delivery_id=envelope.delivery_id,
        new_delivery_id=fresh_id,
        result=store.enqueue(converted),
    )


def _require_quarantined(store: OutboxStore, delivery_id: str) -> DeliveryEnvelope:
    if not isinstance(store, OutboxStore):
        raise OwnerOperationError("invalid outbox store")
    target: Path = store.directory / _item_name(delivery_id)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        raise OwnerOperationError("outbox item missing") from None
    except OSError:
        raise OwnerOperationError("outbox item unavailable") from None
    try:
        envelope = DeliveryEnvelope.from_dict(json.loads(raw.decode("utf-8")))
    except UnicodeDecodeError, json.JSONDecodeError, OutboxContractError, TypeError, ValueError:
        raise OwnerOperationError("outbox item is not quarantined") from None
    if envelope.delivery_id != delivery_id:
        raise OwnerOperationError("outbox item corrupt")
    if envelope.terminal_receipt is not None or envelope.quarantine_reason is None:
        raise OwnerOperationError("outbox item is not quarantined")
    return envelope


def _replace(store: OutboxStore, updated: DeliveryEnvelope) -> OutboxItem:
    data = _canonical_bytes(updated.to_dict())
    _durable_write(store.directory / _item_name(updated.delivery_id), data, exclusive=False)
    return OutboxItem(
        delivery_id=updated.delivery_id,
        state=(
            OutboxItemState.QUARANTINED
            if updated.quarantine_reason is not None
            else OutboxItemState.QUEUED
        ),
        size_bytes=len(data),
        enqueued_at=updated.enqueued_at,
        quarantine_reason=updated.quarantine_reason,
    )


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise OwnerOperationError(f"invalid owner operation {label}") from error
    if parsed.tzinfo is None:
        raise OwnerOperationError(f"invalid owner operation {label}")
    return parsed.astimezone(UTC)
