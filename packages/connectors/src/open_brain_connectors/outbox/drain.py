"""One bounded foreground drain cycle over a durable ``OutboxStore``.

The drain is invoked by the owner and runs to completion of exactly one
cycle: acquire an exclusive lease, walk queued items in a deterministic
order inside a bounded retry window and per-cycle batch bounds, delegate
each delivery to an injected ``Transport`` callable, and record every
outcome as a store state transition. There is no thread, timer, daemon,
or scheduler anywhere in this module.

Lease: one exclusive marker per outbox directory, ``.drain-lease.json``
(the leading dot keeps it invisible to the store's item scan), created
with ``O_CREAT | O_EXCL`` at mode 0o600. A second concurrent drain
observes the marker and returns the stable ``drain_busy`` result without
touching any item. The marker holds the lease version, the creating
pid, and an acquisition timestamp. A stale marker (older than
``stale_lease_after_seconds``, default 600) is reclaimed only when the
recorded pid is no longer alive on this host, and the reclaim is
recorded in the summary. An unparseable or unreadable marker never
authorizes a reclaim; removing one is owner territory. The lease is
released in a ``finally`` block, and a release failure is best effort
because the stale rule reclaims the marker once the recorded pid dies.

Order and window: queued items are attempted in ``enqueued_at`` then
``delivery_id`` order. Before any delivery attempt, an item whose age
has reached ``retry_age_limit_seconds`` or whose recorded attempts have
reached ``retry_attempt_limit`` is quarantined with reason
``age_exhausted`` or ``attempts_exhausted`` and counted in the summary.
Both windows are half-open: the boundary value itself is exhausted. An
item that reaches its attempt limit through a retryable failure inside
this cycle stays queued; the next cycle's pre-attempt check quarantines
it.

Batch bounds: ``max_batch_items`` and ``max_batch_bytes`` cap how many
items and how many envelope bytes one cycle attempts, measured from the
on-disk item sizes at the start of the cycle. A cycle that would exceed
either bound stops after the last admitted item, counts the untouched
remainder under ``batch_too_large_items``, and leaves those items
queued.

Outcomes per attempted item, in order: a ``TerminalReceipt`` is first
verified with ``verify_terminal_receipt``; a verified accepted or
duplicate receipt calls ``store.mark_terminal`` (which re-verifies and
replaces the body with a metadata-only terminal record); a verification
failure quarantines with reason ``receipt_mismatch`` and never removes
the body. A ``DeliveryFailure`` with ``retryable`` true is recorded via
``store.record_attempt`` and the item stays queued; with ``retryable``
false the item is quarantined under the failure code. The ``retryable``
flag mirrors OSS-G4's ``CaptureAdmissionError.retryable`` and is never
reinterpreted here. A transport exception is recorded as a retryable
``transport_error`` attempt -- never quarantined -- and the cycle
continues with the next item.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .contracts import (
    DeliveryEnvelope,
    OutboxContractError,
    TerminalReceipt,
    TerminalReceiptStatus,
    verify_terminal_receipt,
)
from .store import OutboxItem, OutboxItemState, OutboxStore

__all__ = [
    "DEFAULT_STALE_LEASE_SECONDS",
    "DRAIN_LEASE_NAME",
    "DeliveryFailure",
    "DrainResult",
    "DrainSummary",
    "OutboxDrainError",
    "Transport",
    "run_drain_cycle",
]

DRAIN_LEASE_NAME = ".drain-lease.json"
DRAIN_LEASE_VERSION = "outbox.drain-lease.v1"
DEFAULT_STALE_LEASE_SECONDS = 600

Clock = Callable[[], datetime]


class OutboxDrainError(Exception):
    """A drain lease or cycle input failed without exposing item content."""


class DrainResult(StrEnum):
    COMPLETED = "completed"
    DRAIN_BUSY = "drain_busy"


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    """A destination refusal; ``retryable`` is never reinterpreted by the drain."""

    code: str
    retryable: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not self.code
            or len(self.code) > 256
            or any(ord(character) < 33 or ord(character) == 127 for character in self.code)
        ):
            raise OutboxDrainError("invalid delivery failure code")
        if type(self.retryable) is not bool:
            raise OutboxDrainError("invalid delivery failure retryable flag")


class Transport(Protocol):
    """Delivers one immutable envelope; O4 supplies real implementations."""

    def __call__(self, envelope: DeliveryEnvelope, /) -> TerminalReceipt | DeliveryFailure: ...


@dataclass(frozen=True, slots=True)
class DrainSummary:
    """Metadata-only account of one drain cycle; carries no payload content."""

    result: DrainResult
    stale_lease_reclaimed: bool = False
    admitted_items: int = 0
    skipped_items: int = 0
    delivery_attempts: int = 0
    accepted: int = 0
    duplicate: int = 0
    quarantined_age_exhausted: int = 0
    quarantined_attempts_exhausted: int = 0
    quarantined_receipt_mismatch: int = 0
    quarantined_refused: int = 0
    retried: int = 0
    transport_errors: int = 0
    batch_too_large_items: int = 0


@dataclass(frozen=True, slots=True)
class _LeaseMarker:
    pid: int
    acquired_at: datetime


@dataclass(slots=True)
class _Counters:
    admitted: int = 0
    skipped: int = 0
    attempts: int = 0
    accepted: int = 0
    duplicate: int = 0
    age_exhausted: int = 0
    attempts_exhausted: int = 0
    receipt_mismatch: int = 0
    refused: int = 0
    retried: int = 0
    transport_errors: int = 0


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OutboxDrainError("invalid drain clock value")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise OutboxDrainError("invalid drain timestamp") from error
    if parsed.tzinfo is None:
        raise OutboxDrainError("invalid drain timestamp")
    return parsed.astimezone(UTC)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_all(file_fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError
        remaining = remaining[written:]


def _read_lease_marker(path: Path) -> _LeaseMarker | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"acquired_at", "lease_version", "pid"}
        or value["lease_version"] != DRAIN_LEASE_VERSION
        or type(value["pid"]) is not int
        or value["pid"] < 1
        or not isinstance(value["acquired_at"], str)
    ):
        return None
    acquired_at = _parse_timestamp(value["acquired_at"])
    return _LeaseMarker(pid=value["pid"], acquired_at=acquired_at)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _create_marker(path: Path, *, now: datetime, pid: int) -> bool:
    payload = json.dumps(
        {"acquired_at": _format_timestamp(now), "lease_version": DRAIN_LEASE_VERSION, "pid": pid},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    except OSError as error:
        raise OutboxDrainError("drain lease create failed") from error
    try:
        _write_all(file_fd, payload)
        os.fsync(file_fd)
    except OSError as error:
        os.close(file_fd)
        with contextlib.suppress(OSError):
            path.unlink()
        raise OutboxDrainError("drain lease write failed") from error
    os.close(file_fd)
    try:
        _fsync_directory(path.parent)
    except OSError as error:
        with contextlib.suppress(OSError):
            path.unlink()
        raise OutboxDrainError("drain lease sync failed") from error
    return True


def _acquire_lease(
    directory: Path, *, now: datetime, stale_after_seconds: int
) -> tuple[bool, bool]:
    """Create the exclusive marker; returns ``(held, reclaimed_stale)``."""
    path = directory / DRAIN_LEASE_NAME
    reclaimed = False
    while True:
        if _create_marker(path, now=now, pid=os.getpid()):
            return True, reclaimed
        marker = _read_lease_marker(path)
        if marker is None:
            return False, False
        age_seconds = (now - marker.acquired_at).total_seconds()
        if age_seconds <= stale_after_seconds or _pid_alive(marker.pid):
            return False, False
        if reclaimed:
            return False, False
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise OutboxDrainError("drain lease reclaim failed") from error
        reclaimed = True


def _release_lease(directory: Path) -> None:
    try:
        (directory / DRAIN_LEASE_NAME).unlink()
    except OSError:
        return
    with contextlib.suppress(OSError):
        _fsync_directory(directory)


def _load_queued_envelope(store: OutboxStore, delivery_id: str) -> DeliveryEnvelope | None:
    """Re-read one item fresh; ``None`` when it changed or vanished mid-cycle."""
    try:
        raw = (store.directory / f"{delivery_id}.json").read_bytes()
    except OSError:
        return None
    try:
        envelope = DeliveryEnvelope.from_dict(json.loads(raw.decode("utf-8")))
    except UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError:
        return None
    if envelope.delivery_id != delivery_id:
        return None
    if envelope.quarantine_reason is not None or envelope.terminal_receipt is not None:
        return None
    return envelope


def _ordered_queued_items(store: OutboxStore) -> tuple[OutboxItem, ...]:
    """Queued items in ``enqueued_at`` then ``delivery_id`` order."""

    def order_key(item: OutboxItem) -> tuple[datetime, str]:
        if item.enqueued_at is None:
            # Unreachable for a parsed queued envelope; refuse loudly if met.
            raise OutboxDrainError("queued item missing enqueue time")
        return (_parse_timestamp(item.enqueued_at), item.delivery_id)

    queued = [item for item in store.scan() if item.state is OutboxItemState.QUEUED]
    return tuple(sorted(queued, key=order_key))


def run_drain_cycle(
    store: OutboxStore,
    transport: Transport,
    *,
    max_batch_items: int,
    max_batch_bytes: int,
    stale_lease_after_seconds: int = DEFAULT_STALE_LEASE_SECONDS,
    clock: Clock | None = None,
    require_independent_protection: bool = False,
) -> DrainSummary:
    """Run exactly one bounded foreground drain cycle over ``store``."""
    if not isinstance(store, OutboxStore):
        raise OutboxDrainError("invalid outbox store")
    if not callable(transport):
        raise OutboxDrainError("invalid drain transport")
    if type(max_batch_items) is not int or max_batch_items < 1:
        raise OutboxDrainError("invalid drain item bound")
    if type(max_batch_bytes) is not int or max_batch_bytes < 1:
        raise OutboxDrainError("invalid drain byte bound")
    if type(stale_lease_after_seconds) is not int or stale_lease_after_seconds < 1:
        raise OutboxDrainError("invalid stale lease bound")
    if type(require_independent_protection) is not bool:
        raise OutboxDrainError("invalid protection requirement")
    tick = _default_clock if clock is None else clock
    now = _utc_now(tick)
    held, reclaimed = _acquire_lease(
        store.directory, now=now, stale_after_seconds=stale_lease_after_seconds
    )
    if not held:
        return DrainSummary(result=DrainResult.DRAIN_BUSY)
    try:
        return _run_cycle(
            store,
            transport,
            tick=tick,
            max_batch_items=max_batch_items,
            max_batch_bytes=max_batch_bytes,
            reclaimed_stale=reclaimed,
            require_independent_protection=require_independent_protection,
        )
    finally:
        _release_lease(store.directory)


def _run_cycle(
    store: OutboxStore,
    transport: Transport,
    *,
    tick: Clock,
    max_batch_items: int,
    max_batch_bytes: int,
    reclaimed_stale: bool,
    require_independent_protection: bool,
) -> DrainSummary:
    counters = _Counters()
    candidates = _ordered_queued_items(store)

    admitted: list[DeliveryEnvelope] = []
    admitted_bytes = 0
    deferred = 0
    for index, item in enumerate(candidates):
        if len(admitted) == max_batch_items or admitted_bytes + item.size_bytes > max_batch_bytes:
            deferred = len(candidates) - index
            break
        envelope = _load_queued_envelope(store, item.delivery_id)
        if envelope is None:
            counters.skipped += 1
            continue
        admitted.append(envelope)
        admitted_bytes += item.size_bytes
        counters.admitted += 1

    for envelope in admitted:
        now = _utc_now(tick)
        age_seconds = (now - _parse_timestamp(envelope.enqueued_at)).total_seconds()
        if age_seconds >= envelope.retry_age_limit_seconds:
            store.quarantine(envelope.delivery_id, reason="age_exhausted")
            counters.age_exhausted += 1
            continue
        if envelope.attempts >= envelope.retry_attempt_limit:
            store.quarantine(envelope.delivery_id, reason="attempts_exhausted")
            counters.attempts_exhausted += 1
            continue
        counters.attempts += 1
        try:
            outcome = transport(envelope)
        except Exception:
            store.record_attempt(
                envelope.delivery_id,
                attempted_at=_format_timestamp(now),
                result="transport_error",
            )
            counters.transport_errors += 1
            continue
        if isinstance(outcome, TerminalReceipt):
            try:
                verified = verify_terminal_receipt(envelope, outcome)
            except OutboxContractError:
                store.quarantine(envelope.delivery_id, reason="receipt_mismatch")
                counters.receipt_mismatch += 1
                continue
            if require_independent_protection and verified.protection_acknowledgement is None:
                store.record_attempt(
                    envelope.delivery_id,
                    attempted_at=_format_timestamp(now),
                    result="recovery_pending",
                )
                counters.retried += 1
                continue
            store.mark_terminal(envelope.delivery_id, verified, terminal_at=_format_timestamp(now))
            if verified.status is TerminalReceiptStatus.DUPLICATE:
                counters.duplicate += 1
            else:
                counters.accepted += 1
        elif isinstance(outcome, DeliveryFailure):
            if outcome.retryable:
                store.record_attempt(
                    envelope.delivery_id,
                    attempted_at=_format_timestamp(now),
                    result=outcome.code,
                )
                counters.retried += 1
            else:
                store.quarantine(envelope.delivery_id, reason=outcome.code)
                counters.refused += 1
        else:
            raise OutboxDrainError("invalid drain transport outcome")

    return DrainSummary(
        result=DrainResult.COMPLETED,
        stale_lease_reclaimed=reclaimed_stale,
        admitted_items=counters.admitted,
        skipped_items=counters.skipped,
        delivery_attempts=counters.attempts,
        accepted=counters.accepted,
        duplicate=counters.duplicate,
        quarantined_age_exhausted=counters.age_exhausted,
        quarantined_attempts_exhausted=counters.attempts_exhausted,
        quarantined_receipt_mismatch=counters.receipt_mismatch,
        quarantined_refused=counters.refused,
        retried=counters.retried,
        transport_errors=counters.transport_errors,
        batch_too_large_items=deferred,
    )
