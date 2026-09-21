"""Durable one-file-per-item outbox store with bounded capacity.

The store keeps one JSON file per delivery named ``<delivery_id>.json``
(mode 0o600) inside a single owner-supplied directory (mode 0o700). There is
no index file: every operation rescans the directory, and recovery is that
same scan. An unparseable item file is surfaced as a metadata-only ``corrupt``
entry and is never deleted.

Durability sequence (atomic write): a same-directory temporary file is
written, fsynced, chmod 0o600, atomically renamed onto the item name, and the
parent directory is fsynced where the platform exposes that primitive. This
follows the collector's ``CollectorStateStore.save`` precedent copied locally,
because the engine ``storage.filesystem`` helpers are monolithic with no
injection seam between their internal steps and would add a lock file to the
item directory. ``enqueue`` reports ``queued`` only after the rename and the
directory fsync complete.

Per-item limit: an envelope whose encoded document exceeds ``max_item_bytes``
(default ``DEFAULT_MAX_ITEM_BYTES``, held under the stdio request document cap
with margin) is refused with the stable ``item_too_large`` result before any
temporary file exists, so every accepted item can always be serialized into
one bounded stdio request document.

Capacity headroom rule: admission reserves a fixed
``TERMINAL_METADATA_HEADROOM_BYTES`` (2048) beyond the encoded item size, so
the attempt and quarantine metadata that a later state transition appends to
an accepted body always fits under ``max_bytes``; state transitions are never
refused for capacity. Terminal records replace the item with a smaller
metadata-only record. Quarantined, terminal, and corrupt items count against
both caps for as long as their files remain.

Terminal records: ``mark_terminal`` and ``terminal_discard`` both replace the
item file with one shared metadata-only terminal record -- a store-level
format (``outbox.terminal.v1``) defined in this module, not an outbox.v1
contract surface. The record keeps the delivery ID, destination Brain ID,
issuer epoch, tenant ID, principal ID, request digest, enqueue timestamp,
attempt count, last attempt time, and the terminal timestamp; the request
digest is the only payload-derived value. The ``receipt`` kind embeds a
``TerminalReceipt`` verified by ``verify_terminal_receipt`` before anything is
written and cross-binds it to the record's destination, epoch, delivery ID,
and digest, so a tampered record surfaces as corrupt; the ``discard`` kind
records an owner-confirmed discard with no receipt. The body bytes are
removed on both paths.

Crash seam: tests (and the O6 crash matrix) monkeypatch the module-level
``_write_hooks`` mapping to raise at a named boundary --
``after_temp_write``, ``after_temp_fsync``, ``after_rename``, or
``after_directory_fsync`` -- to prove no partial item survives at each step.
A hook exception propagates without temp-file cleanup, simulating a process
crash; use a non-OSError sentinel so the cleanup path does not mask it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .contracts import (
    DeliveryEnvelope,
    TerminalReceipt,
    verify_terminal_receipt,
)

__all__ = [
    "DEFAULT_MAX_ITEM_BYTES",
    "MAX_ITEM_MARGIN_BYTES",
    "MAX_REQUEST_DOCUMENT_BYTES",
    "TERMINAL_METADATA_HEADROOM_BYTES",
    "EnqueueResult",
    "OutboxItem",
    "OutboxItemState",
    "OutboxStatus",
    "OutboxStore",
    "OutboxStoreError",
]

TERMINAL_METADATA_HEADROOM_BYTES = 2048

# The single source for the stdio transport's request document byte cap:
# ``stdio_transport.MAX_DOCUMENT_BYTES`` binds to this value, and the default
# per-item limit below is derived from it here, in one place. The request
# document carries a subset of the envelope's fields, so an envelope within
# the derived limit always serializes into a request document within the cap.
MAX_REQUEST_DOCUMENT_BYTES = 256 * 1024
MAX_ITEM_MARGIN_BYTES = 8 * 1024
DEFAULT_MAX_ITEM_BYTES = MAX_REQUEST_DOCUMENT_BYTES - MAX_ITEM_MARGIN_BYTES

_ITEM_SUFFIX = ".json"
_TERMINAL_RECORD_VERSION = "outbox.terminal.v1"
_BRAIN_ID = re.compile(r"brn_[a-z2-7]{26}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")
_MAX_REFERENCE_LENGTH = 256

_WriteHook = Callable[[Path], None]

# Crash-injection seam: maps a write boundary name to a hook called at that
# boundary. Tests replace this mapping wholesale; production leaves it empty.
_write_hooks: dict[str, _WriteHook] = {}


class OutboxStoreError(Exception):
    """A durable outbox store operation failed without exposing item content."""


class EnqueueResult(StrEnum):
    QUEUED = "queued"
    ALREADY_QUEUED = "already_queued"
    DELIVERY_CONFLICT = "delivery_conflict"
    OUTBOX_FULL = "outbox_full"
    ITEM_TOO_LARGE = "item_too_large"


class OutboxItemState(StrEnum):
    QUEUED = "queued"
    QUARANTINED = "quarantined"
    TERMINAL = "terminal"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """Metadata-only view of one stored item; never carries payload content."""

    delivery_id: str
    state: OutboxItemState
    size_bytes: int
    enqueued_at: str | None = None
    quarantine_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxStatus:
    """Counts and consumed capacity by state; payload-free by construction."""

    max_items: int
    max_bytes: int
    headroom_bytes: int
    items: int
    size_bytes: int
    queued_items: int
    queued_size_bytes: int
    quarantined_items: int
    quarantined_size_bytes: int
    terminal_items: int
    terminal_size_bytes: int
    corrupt_items: int
    corrupt_size_bytes: int


@dataclass(frozen=True, slots=True)
class _TerminalRecord:
    """Metadata-only terminal record replacing a terminal item's body.

    A store-level format (``outbox.terminal.v1``), not an outbox.v1 contract
    surface: ``receipt_kind`` items embed the verified destination receipt and
    cross-bind it to the record identity; ``discard`` items record an
    owner-confirmed discard.
    """

    record_version: str
    terminal_kind: str
    delivery_id: str
    destination_brain_id: str
    issuer_epoch: int
    tenant_id: str
    principal_id: str
    request_digest: str
    enqueued_at: str
    attempts: int
    last_attempt_at: str | None
    terminal_at: str
    receipt: object = None

    @classmethod
    def for_receipt(
        cls, envelope: DeliveryEnvelope, receipt: TerminalReceipt, terminal_at: str
    ) -> _TerminalRecord:
        return cls(
            record_version=_TERMINAL_RECORD_VERSION,
            terminal_kind="receipt",
            delivery_id=envelope.delivery_id,
            destination_brain_id=envelope.destination_brain_id,
            issuer_epoch=envelope.expected_issuer_epoch,
            tenant_id=envelope.tenant_id,
            principal_id=envelope.principal_id,
            request_digest=envelope.request_digest,
            enqueued_at=envelope.enqueued_at,
            attempts=envelope.attempts,
            last_attempt_at=envelope.last_attempt_at,
            terminal_at=terminal_at,
            receipt=receipt.to_dict(),
        )

    @classmethod
    def for_discard(cls, envelope: DeliveryEnvelope, terminal_at: str) -> _TerminalRecord:
        return cls(
            record_version=_TERMINAL_RECORD_VERSION,
            terminal_kind="discard",
            delivery_id=envelope.delivery_id,
            destination_brain_id=envelope.destination_brain_id,
            issuer_epoch=envelope.expected_issuer_epoch,
            tenant_id=envelope.tenant_id,
            principal_id=envelope.principal_id,
            request_digest=envelope.request_digest,
            enqueued_at=envelope.enqueued_at,
            attempts=envelope.attempts,
            last_attempt_at=envelope.last_attempt_at,
            terminal_at=terminal_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "record_version": self.record_version,
            "terminal_kind": self.terminal_kind,
            "delivery_id": self.delivery_id,
            "destination_brain_id": self.destination_brain_id,
            "issuer_epoch": self.issuer_epoch,
            "tenant_id": self.tenant_id,
            "principal_id": self.principal_id,
            "request_digest": self.request_digest,
            "enqueued_at": self.enqueued_at,
            "attempts": self.attempts,
            "last_attempt_at": self.last_attempt_at,
            "terminal_at": self.terminal_at,
            "receipt": self.receipt,
        }

    @classmethod
    def from_dict(cls, value: object) -> _TerminalRecord:
        expected = {
            "record_version",
            "terminal_kind",
            "delivery_id",
            "destination_brain_id",
            "issuer_epoch",
            "tenant_id",
            "principal_id",
            "request_digest",
            "enqueued_at",
            "attempts",
            "last_attempt_at",
            "terminal_at",
            "receipt",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid terminal record fields")
        kind = value["terminal_kind"]
        if (
            value["record_version"] != _TERMINAL_RECORD_VERSION
            or kind not in {"receipt", "discard"}
            or not _valid_reference(value["delivery_id"])
            or not _valid_reference(value["tenant_id"])
            or not _valid_reference(value["principal_id"])
            or _BRAIN_ID.fullmatch(str(value["destination_brain_id"])) is None
            or _SHA256.fullmatch(str(value["request_digest"])) is None
            or type(value["issuer_epoch"]) is not int
            or value["issuer_epoch"] < 1
            or type(value["attempts"]) is not int
            or value["attempts"] < 0
            or _TIMESTAMP.fullmatch(str(value["enqueued_at"])) is None
            or _TIMESTAMP.fullmatch(str(value["terminal_at"])) is None
            or not _valid_optional_timestamp(value["last_attempt_at"])
            or (value["attempts"] == 0 and value["last_attempt_at"] is not None)
        ):
            raise ValueError("invalid terminal record")
        if kind == "receipt":
            try:
                checked = TerminalReceipt.from_dict(value["receipt"])
            except ValueError:
                raise ValueError("invalid terminal record") from None
            if (
                checked.brain_id != value["destination_brain_id"]
                or checked.issuer_epoch != value["issuer_epoch"]
                or checked.delivery_id != value["delivery_id"]
                or checked.request_digest != value["request_digest"]
            ):
                raise ValueError("invalid terminal record")
            receipt: object = checked.to_dict()
        elif value["receipt"] is not None:
            raise ValueError("invalid terminal record")
        else:
            receipt = None
        return cls(
            record_version=str(value["record_version"]),
            terminal_kind=str(kind),
            delivery_id=str(value["delivery_id"]),
            destination_brain_id=str(value["destination_brain_id"]),
            issuer_epoch=value["issuer_epoch"],
            tenant_id=str(value["tenant_id"]),
            principal_id=str(value["principal_id"]),
            request_digest=str(value["request_digest"]),
            enqueued_at=str(value["enqueued_at"]),
            attempts=value["attempts"],
            last_attempt_at=(
                None if value["last_attempt_at"] is None else str(value["last_attempt_at"])
            ),
            terminal_at=str(value["terminal_at"]),
            receipt=receipt,
        )


@dataclass(frozen=True, slots=True)
class _ScannedItem:
    name: str
    delivery_id: str
    state: OutboxItemState
    size_bytes: int
    enqueued_at: str | None
    quarantine_reason: str | None


def _valid_optional_timestamp(value: object) -> bool:
    return value is None or (isinstance(value, str) and _TIMESTAMP.fullmatch(value) is not None)


def _fire_hook(stage: str, target: Path) -> None:
    hook = _write_hooks.get(stage)
    if hook is not None:
        hook(target)


def _write_all(file_fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError
        remaining = remaining[written:]


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _durable_write(target: Path, data: bytes, *, exclusive: bool) -> str:
    """Write ``data`` durably; returns created, already_exists, or conflict."""
    temp: Path | None = None
    try:
        file_fd, temp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        temp = Path(temp_name)
        with os.fdopen(file_fd, "wb") as handle:
            _write_all(handle.fileno(), data)
            _fire_hook("after_temp_write", target)
            os.fsync(handle.fileno())
        _fire_hook("after_temp_fsync", target)
        temp.chmod(0o600)
        if exclusive and target.exists():
            existing = target.read_bytes()
            os.unlink(temp)
            temp = None
            return "already_exists" if existing == data else "conflict"
        os.replace(temp, target)
        temp = None
        _fire_hook("after_rename", target)
        _fsync_directory(target.parent)
        _fire_hook("after_directory_fsync", target)
        return "created"
    except OSError as error:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)
        raise OutboxStoreError("outbox durable write failed") from error


def _canonical_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _valid_reference(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return (
        bool(value)
        and len(value) <= _MAX_REFERENCE_LENGTH
        and unicodedata.normalize("NFC", value) == value
        and all(33 <= ord(character) <= 0x10FFFF and ord(character) != 127 for character in value)
    )


def _item_name(delivery_id: str) -> str:
    if (
        not _valid_reference(delivery_id)
        or delivery_id.startswith(".")
        or "/" in delivery_id
        or "\\" in delivery_id
    ):
        raise OutboxStoreError("unsafe delivery id")
    return delivery_id + _ITEM_SUFFIX


class OutboxStore:
    """Foreground, single-process durable item store for one outbox."""

    def __init__(
        self,
        directory: Path,
        *,
        max_items: int,
        max_bytes: int,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
    ) -> None:
        if not isinstance(directory, Path):
            raise OutboxStoreError("invalid outbox directory")
        if type(max_items) is not int or max_items < 1:
            raise OutboxStoreError("invalid outbox item capacity")
        if type(max_bytes) is not int or max_bytes < 1:
            raise OutboxStoreError("invalid outbox byte capacity")
        if type(max_item_bytes) is not int or max_item_bytes < 1:
            raise OutboxStoreError("invalid outbox item byte limit")
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise OutboxStoreError("outbox directory unavailable") from error
        self._directory = directory
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._max_item_bytes = max_item_bytes

    @property
    def directory(self) -> Path:
        return self._directory

    def enqueue(self, envelope: DeliveryEnvelope) -> EnqueueResult:
        if not isinstance(envelope, DeliveryEnvelope):
            raise OutboxStoreError("invalid delivery envelope")
        checked = DeliveryEnvelope.from_dict(envelope.to_dict())
        target = self._directory / _item_name(checked.delivery_id)
        data = _canonical_bytes(checked.to_dict())
        if target.exists():
            try:
                existing = target.read_bytes()
            except OSError as error:
                raise OutboxStoreError("outbox item unavailable") from error
            if existing == data:
                return EnqueueResult.ALREADY_QUEUED
            return EnqueueResult.DELIVERY_CONFLICT
        if len(data) > self._max_item_bytes:
            return EnqueueResult.ITEM_TOO_LARGE
        scanned = self._scan()
        if len(scanned) + 1 > self._max_items:
            return EnqueueResult.OUTBOX_FULL
        total_bytes = sum(item.size_bytes for item in scanned)
        if total_bytes + len(data) + TERMINAL_METADATA_HEADROOM_BYTES > self._max_bytes:
            return EnqueueResult.OUTBOX_FULL
        outcome = _durable_write(target, data, exclusive=True)
        if outcome == "already_exists":
            return EnqueueResult.ALREADY_QUEUED
        if outcome == "conflict":
            return EnqueueResult.DELIVERY_CONFLICT
        return EnqueueResult.QUEUED

    def record_attempt(self, delivery_id: str, *, attempted_at: str, result: str) -> OutboxItem:
        envelope = self._require_active_envelope(delivery_id)
        values = envelope.to_dict()
        values["attempts"] = envelope.attempts + 1
        values["last_attempt_at"] = attempted_at
        values["last_attempt_result"] = result
        updated = DeliveryEnvelope.from_dict(values)
        return self._replace_envelope(updated)

    def quarantine(self, delivery_id: str, *, reason: str) -> OutboxItem:
        envelope = self._require_active_envelope(delivery_id)
        values = envelope.to_dict()
        values["quarantine_reason"] = reason
        updated = DeliveryEnvelope.from_dict(values)
        return self._replace_envelope(updated)

    def mark_terminal(
        self, delivery_id: str, receipt: TerminalReceipt, *, terminal_at: str
    ) -> OutboxItem:
        envelope = self._require_active_envelope(delivery_id)
        verified = verify_terminal_receipt(envelope, receipt)
        return self._write_terminal_record(
            _TerminalRecord.for_receipt(envelope, verified, terminal_at)
        )

    def terminal_discard(self, delivery_id: str, *, discarded_at: str) -> OutboxItem:
        envelope = self._require_active_envelope(delivery_id)
        return self._write_terminal_record(_TerminalRecord.for_discard(envelope, discarded_at))

    def scan(self) -> tuple[OutboxItem, ...]:
        return tuple(
            OutboxItem(
                delivery_id=item.delivery_id,
                state=item.state,
                size_bytes=item.size_bytes,
                enqueued_at=item.enqueued_at,
                quarantine_reason=item.quarantine_reason,
            )
            for item in self._scan()
        )

    def status(self) -> OutboxStatus:
        scanned = self._scan()
        counts = {state: [0, 0] for state in OutboxItemState}
        for item in scanned:
            counts[item.state][0] += 1
            counts[item.state][1] += item.size_bytes
        return OutboxStatus(
            max_items=self._max_items,
            max_bytes=self._max_bytes,
            headroom_bytes=TERMINAL_METADATA_HEADROOM_BYTES,
            items=len(scanned),
            size_bytes=sum(item.size_bytes for item in scanned),
            queued_items=counts[OutboxItemState.QUEUED][0],
            queued_size_bytes=counts[OutboxItemState.QUEUED][1],
            quarantined_items=counts[OutboxItemState.QUARANTINED][0],
            quarantined_size_bytes=counts[OutboxItemState.QUARANTINED][1],
            terminal_items=counts[OutboxItemState.TERMINAL][0],
            terminal_size_bytes=counts[OutboxItemState.TERMINAL][1],
            corrupt_items=counts[OutboxItemState.CORRUPT][0],
            corrupt_size_bytes=counts[OutboxItemState.CORRUPT][1],
        )

    def _scan(self) -> tuple[_ScannedItem, ...]:
        try:
            with os.scandir(self._directory) as entries:
                scanned = [
                    self._scan_entry(entry) for entry in entries if not entry.name.startswith(".")
                ]
        except OSError as error:
            raise OutboxStoreError("outbox scan failed") from error
        return tuple(sorted(scanned, key=lambda item: item.name))

    def _scan_entry(self, entry: os.DirEntry[str]) -> _ScannedItem:
        stem = entry.name.removesuffix(_ITEM_SUFFIX)
        try:
            info = entry.stat(follow_symlinks=False)
            raw = Path(entry.path).read_bytes() if entry.is_file(follow_symlinks=False) else None
        except OSError as error:
            raise OutboxStoreError("outbox scan failed") from error
        if raw is not None:
            item = self._parse_item(raw, stem)
            if item is not None:
                return item
        return _ScannedItem(
            name=stem + _ITEM_SUFFIX,
            delivery_id=stem,
            state=OutboxItemState.CORRUPT,
            size_bytes=info.st_size,
            enqueued_at=None,
            quarantine_reason=None,
        )

    def _parse_item(self, raw: bytes, stem: str) -> _ScannedItem | None:
        try:
            value = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return None
        try:
            envelope = DeliveryEnvelope.from_dict(value)
        except ValueError, TypeError:
            envelope = None
        if envelope is not None:
            if envelope.delivery_id != stem:
                return None
            state = (
                OutboxItemState.TERMINAL
                if envelope.terminal_receipt is not None
                else OutboxItemState.QUARANTINED
                if envelope.quarantine_reason is not None
                else OutboxItemState.QUEUED
            )
            return _ScannedItem(
                name=stem + _ITEM_SUFFIX,
                delivery_id=envelope.delivery_id,
                state=state,
                size_bytes=len(raw),
                enqueued_at=envelope.enqueued_at,
                quarantine_reason=envelope.quarantine_reason,
            )
        try:
            record = _TerminalRecord.from_dict(value)
        except ValueError:
            return None
        if record.delivery_id != stem:
            return None
        return _ScannedItem(
            name=stem + _ITEM_SUFFIX,
            delivery_id=record.delivery_id,
            state=OutboxItemState.TERMINAL,
            size_bytes=len(raw),
            enqueued_at=record.enqueued_at,
            quarantine_reason=None,
        )

    def _require_active_envelope(self, delivery_id: str) -> DeliveryEnvelope:
        target = self._directory / _item_name(delivery_id)
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            raise OutboxStoreError("outbox item missing") from None
        except OSError as error:
            raise OutboxStoreError("outbox item unavailable") from error
        try:
            value = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            raise OutboxStoreError("outbox item corrupt") from None
        try:
            envelope = DeliveryEnvelope.from_dict(value)
        except ValueError, TypeError:
            try:
                _TerminalRecord.from_dict(value)
            except ValueError:
                raise OutboxStoreError("outbox item corrupt") from None
            raise OutboxStoreError("outbox item is terminal") from None
        if envelope.delivery_id != delivery_id:
            raise OutboxStoreError("outbox item corrupt")
        if envelope.terminal_receipt is not None:
            raise OutboxStoreError("outbox item is terminal")
        return envelope

    def _replace_envelope(self, updated: DeliveryEnvelope) -> OutboxItem:
        data = _canonical_bytes(updated.to_dict())
        _durable_write(self._directory / _item_name(updated.delivery_id), data, exclusive=False)
        state = (
            OutboxItemState.QUARANTINED
            if updated.quarantine_reason is not None
            else OutboxItemState.QUEUED
        )
        return OutboxItem(
            delivery_id=updated.delivery_id,
            state=state,
            size_bytes=len(data),
            enqueued_at=updated.enqueued_at,
            quarantine_reason=updated.quarantine_reason,
        )

    def _write_terminal_record(self, record: _TerminalRecord) -> OutboxItem:
        checked = _TerminalRecord.from_dict(record.to_dict())
        data = _canonical_bytes(checked.to_dict())
        _durable_write(self._directory / _item_name(checked.delivery_id), data, exclusive=False)
        return OutboxItem(
            delivery_id=checked.delivery_id,
            state=OutboxItemState.TERMINAL,
            size_bytes=len(data),
            enqueued_at=checked.enqueued_at,
        )
