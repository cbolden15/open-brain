from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.capture.models import (
    CaptureLease,
    CaptureWorkItem,
    DistillationLease,
    DistillationWorkItem,
    QueueErrorCode,
    QueueItemState,
    _parse_timestamp,
    _timestamp,
    _utc_datetime,
)
from open_brain_engine.core.ids import canonical_json_bytes, validate_identifier
from open_brain_engine.core.ports import PutDisposition, PutResult

MAX_ATTEMPTS = 3
_DISTILLATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class QueueError(RuntimeError):
    """A closed capture-queue operation failure."""


class QueueImmutableConflictError(QueueError):
    """An item ID already has different immutable canonical bytes."""


class QueueLeaseError(QueueError):
    """A lease no longer matches the live processing record."""


class QueueWriteError(QueueError):
    """A durable queue write did not complete."""


@dataclass(frozen=True, slots=True)
class PendingQueueSnapshot:
    """Read-only pending metadata for diagnostics without queue transitions."""

    pending_count: int
    malformed_count: int
    oldest_captured_at: datetime | None

    def __post_init__(self) -> None:
        if (
            type(self.pending_count) is not int
            or self.pending_count < 0
            or type(self.malformed_count) is not int
            or self.malformed_count < 0
        ):
            raise ValueError("invalid pending queue snapshot")
        if self.oldest_captured_at is not None:
            object.__setattr__(
                self,
                "oldest_captured_at",
                _utc_datetime(self.oldest_captured_at),
            )
        if (self.pending_count == 0) is not (self.oldest_captured_at is None):
            raise ValueError("invalid pending queue snapshot")


@dataclass(frozen=True, slots=True)
class _QueueRecord:
    state: QueueItemState
    item: CaptureWorkItem
    item_id: str
    payload_digest_sha256: str
    worker_id: str | None
    lease_token: str | None
    claimed_at: datetime | None
    error_code: QueueErrorCode | None

    @classmethod
    def create(
        cls,
        *,
        state: QueueItemState | str,
        item: CaptureWorkItem,
        item_id: str,
        payload_digest_sha256: str,
        worker_id: str | None = None,
        lease_token: str | None = None,
        claimed_at: datetime | None = None,
        error_code: QueueErrorCode | str | None = None,
    ) -> _QueueRecord:
        try:
            normalized_state = QueueItemState(state)
            normalized_error = None if error_code is None else QueueErrorCode(error_code)
            validate_identifier(item_id, prefix="cap_")
        except (TypeError, ValueError) as error:
            raise ValueError("invalid queue record") from error
        if (
            not isinstance(item, CaptureWorkItem)
            or item_id != str(item.envelope.capture_id)
            or payload_digest_sha256 != item.payload_digest_sha256()
        ):
            raise ValueError("invalid queue record")
        if normalized_state is QueueItemState.PROCESSING:
            if (
                not isinstance(worker_id, str)
                or not worker_id
                or not isinstance(lease_token, str)
                or not lease_token
                or claimed_at is None
                or normalized_error is not None
            ):
                raise ValueError("invalid queue record")
            normalized_claimed_at = _utc_datetime(claimed_at)
        else:
            if worker_id is not None or lease_token is not None or claimed_at is not None:
                raise ValueError("invalid queue record")
            normalized_claimed_at = None
            if normalized_state is QueueItemState.QUARANTINED:
                if normalized_error is None:
                    raise ValueError("invalid queue record")
            elif normalized_error is not None:
                raise ValueError("invalid queue record")
        return cls(
            normalized_state,
            item,
            item_id,
            payload_digest_sha256,
            worker_id,
            lease_token,
            normalized_claimed_at,
            normalized_error,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "state": self.state.value,
            "item": self.item.to_dict(),
            "item_id": self.item_id,
            "payload_digest_sha256": self.payload_digest_sha256,
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "claimed_at": None if self.claimed_at is None else _timestamp(self.claimed_at),
            "error_code": None if self.error_code is None else self.error_code.value,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> _QueueRecord:
        try:
            decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
            value = _mapping(decoded)
            if (
                set(value)
                != {
                    "schema_version",
                    "state",
                    "item",
                    "item_id",
                    "payload_digest_sha256",
                    "worker_id",
                    "lease_token",
                    "claimed_at",
                    "error_code",
                }
                or value["schema_version"] != 1
            ):
                raise ValueError("invalid queue record")
            claimed_at = value["claimed_at"]
            return_value = cls.create(
                state=_string(value["state"]),
                item=CaptureWorkItem.from_dict(_mapping(value["item"])),
                item_id=_string(value["item_id"]),
                payload_digest_sha256=_string(value["payload_digest_sha256"]),
                worker_id=_optional_string(value["worker_id"]),
                lease_token=_optional_string(value["lease_token"]),
                claimed_at=None if claimed_at is None else _parse_timestamp(_string(claimed_at)),
                error_code=_optional_string(value["error_code"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("invalid queue record") from error
        if return_value.canonical_bytes() != payload:
            raise ValueError("non-canonical queue record")
        return return_value


class FilesystemCaptureQueue:
    """A locked, atomically-published durable queue for capture work items."""

    def __init__(self, root: Path, *, recover_processing: bool = True) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("queue root must be an absolute path")
        self._root = root
        self._active = root / "active"
        self._quarantine = root / "quarantine"
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._active.mkdir(mode=0o700, exist_ok=True)
        self._quarantine.mkdir(mode=0o700, exist_ok=True)
        if recover_processing:
            with self._locked():
                self._recover_processing()

    def enqueue(self, item: CaptureWorkItem, *, item_id: str, payload_digest: str) -> PutResult:
        record = _QueueRecord.create(
            state=QueueItemState.PENDING,
            item=item,
            item_id=item_id,
            payload_digest_sha256=payload_digest,
        )
        with self._locked():
            existing = self._find_record(record.item_id)
            if existing is not None:
                if existing.payload_digest_sha256 == record.payload_digest_sha256:
                    return PutResult(PutDisposition.DUPLICATE, record.item_id, payload_digest)
                raise QueueImmutableConflictError("immutable queue item conflict")
            self._write_record(self._active, record)
            return PutResult(PutDisposition.CREATED, record.item_id, payload_digest)

    def claim(self, *, worker_id: str, now: datetime) -> CaptureLease | None:
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("invalid worker ID")
        current_time = _utc_datetime(now)
        with self._locked():
            pending = [
                record
                for record in self._active_records()
                if record.state is QueueItemState.PENDING
                and record.item.available_at <= current_time
            ]
            if not pending:
                return None
            record = min(
                pending,
                key=lambda candidate: (
                    candidate.item.available_at,
                    candidate.item.envelope.captured_at,
                    candidate.item_id,
                ),
            )
            processing = _QueueRecord.create(
                state=QueueItemState.PROCESSING,
                item=record.item,
                item_id=record.item_id,
                payload_digest_sha256=record.payload_digest_sha256,
                worker_id=worker_id,
                lease_token=secrets.token_hex(32),
                claimed_at=current_time,
            )
            self._write_record(self._active, processing)
            return CaptureLease.create(
                item=processing.item,
                item_id=processing.item_id,
                payload_digest_sha256=processing.payload_digest_sha256,
                worker_id=processing.worker_id or "",
                lease_token=processing.lease_token or "",
                claimed_at=processing.claimed_at or current_time,
            )

    def acknowledge(self, lease: CaptureLease, *, completed_at: datetime) -> None:
        _utc_datetime(completed_at)
        with self._locked():
            self._require_live_lease(lease)
            self._remove_record(self._active, lease.item_id)

    def retry(self, lease: CaptureLease, *, available_at: datetime, error_code: str) -> None:
        next_available_at = _utc_datetime(available_at)
        code = _queue_error_code(error_code)
        with self._locked():
            record = self._require_live_lease(lease)
            next_item = CaptureWorkItem.create(
                envelope=record.item.envelope,
                available_at=next_available_at,
                attempt_count=record.item.attempt_count + 1,
                last_error_code=code,
            )
            if next_item.attempt_count >= MAX_ATTEMPTS:
                quarantined = _QueueRecord.create(
                    state=QueueItemState.QUARANTINED,
                    item=next_item,
                    item_id=record.item_id,
                    payload_digest_sha256=next_item.payload_digest_sha256(),
                    error_code=QueueErrorCode.RETRY_EXHAUSTED,
                )
                self._write_record(self._quarantine, quarantined)
                self._remove_record(self._active, record.item_id)
                return
            pending = _QueueRecord.create(
                state=QueueItemState.PENDING,
                item=next_item,
                item_id=record.item_id,
                payload_digest_sha256=next_item.payload_digest_sha256(),
            )
            self._write_record(self._active, pending)

    def quarantine(self, lease: CaptureLease, *, at: datetime, error_code: str) -> None:
        _utc_datetime(at)
        code = _queue_error_code(error_code)
        with self._locked():
            record = self._require_live_lease(lease)
            quarantined = _QueueRecord.create(
                state=QueueItemState.QUARANTINED,
                item=record.item,
                item_id=record.item_id,
                payload_digest_sha256=record.payload_digest_sha256,
                error_code=code,
            )
            self._write_record(self._quarantine, quarantined)
            self._remove_record(self._active, record.item_id)

    def pending_snapshot(self) -> PendingQueueSnapshot:
        """Observe pending age inputs without claiming or quarantining any record."""
        return read_pending_queue_snapshot(self._root)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self._root / ".queue.lock"
        file_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
            os.close(file_descriptor)

    def _recover_processing(self) -> None:
        for record in self._active_records():
            if record.state is QueueItemState.PROCESSING:
                self._write_record(
                    self._active,
                    _QueueRecord.create(
                        state=QueueItemState.PENDING,
                        item=record.item,
                        item_id=record.item_id,
                        payload_digest_sha256=record.payload_digest_sha256,
                    ),
                )

    def _active_records(self) -> list[_QueueRecord]:
        records: list[_QueueRecord] = []
        for path in sorted(self._active.glob("*.json")):
            try:
                records.append(_QueueRecord.from_canonical_bytes(path.read_bytes()))
            except (OSError, ValueError):
                self._quarantine_malformed(path)
        return records

    def _find_record(self, item_id: str) -> _QueueRecord | None:
        for directory in (self._active, self._quarantine):
            path = directory / _record_name(item_id)
            if not path.exists():
                continue
            try:
                return _QueueRecord.from_canonical_bytes(path.read_bytes())
            except (OSError, ValueError):
                if directory == self._active:
                    self._quarantine_malformed(path)
                return None
        return None

    def _require_live_lease(self, lease: CaptureLease) -> _QueueRecord:
        if not isinstance(lease, CaptureLease):
            raise QueueLeaseError("invalid queue lease")
        path = self._active / _record_name(lease.item_id)
        try:
            record = _QueueRecord.from_canonical_bytes(path.read_bytes())
        except (OSError, ValueError):
            raise QueueLeaseError("stale queue lease") from None
        if (
            record.state is not QueueItemState.PROCESSING
            or record.item_id != lease.item_id
            or record.payload_digest_sha256 != lease.payload_digest_sha256
            or record.worker_id != lease.worker_id
            or record.lease_token != lease.lease_token
            or record.claimed_at != lease.claimed_at
        ):
            raise QueueLeaseError("stale queue lease")
        return record

    def _quarantine_malformed(self, path: Path) -> None:
        payload = path.read_bytes() if path.exists() else b""
        item_id, digest, code = _malformed_metadata(payload)
        record = canonical_json_bytes(
            {
                "schema_version": 1,
                "item_id": item_id,
                "payload_digest_sha256": digest,
                "error_code": code.value,
            }
        )
        target = self._quarantine / (
            "malformed-" + sha256(path.name.encode()).hexdigest() + ".json"
        )
        self._write_bytes(self._quarantine, target.name, record)
        self._remove_path(path)

    def _write_record(self, directory: Path, record: _QueueRecord) -> None:
        self._write_bytes(directory, _record_name(record.item_id), record.canonical_bytes())

    def _write_bytes(self, directory: Path, name: str, payload: bytes) -> None:
        temporary = directory / ("." + secrets.token_hex(16) + ".tmp")
        file_descriptor = -1
        try:
            file_descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            _write_all(file_descriptor, payload)
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = -1
            os.replace(temporary, directory / name)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise QueueWriteError("durable queue write failed") from error
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()

    def _remove_record(self, directory: Path, item_id: str) -> None:
        self._remove_path(directory / _record_name(item_id))

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            path.unlink()
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise QueueWriteError("durable queue write failed") from error


@dataclass(frozen=True, slots=True)
class _DistillationQueueRecord:
    state: QueueItemState
    item: DistillationWorkItem
    item_id: str
    payload_digest_sha256: str
    available_at: datetime
    attempt_count: int
    worker_id: str | None
    lease_token: str | None
    claimed_at: datetime | None
    error_code: QueueErrorCode | None

    @classmethod
    def create(
        cls,
        *,
        state: QueueItemState | str,
        item: DistillationWorkItem,
        item_id: str,
        payload_digest_sha256: str,
        available_at: datetime,
        attempt_count: int = 0,
        worker_id: str | None = None,
        lease_token: str | None = None,
        claimed_at: datetime | None = None,
        error_code: QueueErrorCode | str | None = None,
    ) -> _DistillationQueueRecord:
        try:
            normalized_state = QueueItemState(state)
            normalized_error = None if error_code is None else QueueErrorCode(error_code)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid distillation queue record") from error
        if (
            not isinstance(item, DistillationWorkItem)
            or not isinstance(item_id, str)
            or _DISTILLATION_ID.fullmatch(item_id) is None
            or item_id != item.event_id
            or payload_digest_sha256 != item.payload_digest_sha256()
            or not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 0
        ):
            raise ValueError("invalid distillation queue record")
        normalized_available_at = _utc_datetime(available_at)
        if normalized_state is QueueItemState.PROCESSING:
            if (
                not isinstance(worker_id, str)
                or _DISTILLATION_ID.fullmatch(worker_id) is None
                or not isinstance(lease_token, str)
                or _DISTILLATION_ID.fullmatch(lease_token) is None
                or claimed_at is None
                or normalized_error is not None
            ):
                raise ValueError("invalid distillation queue record")
            normalized_claimed_at = _utc_datetime(claimed_at)
        else:
            if worker_id is not None or lease_token is not None or claimed_at is not None:
                raise ValueError("invalid distillation queue record")
            normalized_claimed_at = None
            if normalized_state is QueueItemState.QUARANTINED:
                if normalized_error is None:
                    raise ValueError("invalid distillation queue record")
            elif normalized_error is not None:
                raise ValueError("invalid distillation queue record")
        return cls(
            normalized_state,
            item,
            item_id,
            payload_digest_sha256,
            normalized_available_at,
            attempt_count,
            worker_id,
            lease_token,
            normalized_claimed_at,
            normalized_error,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "state": self.state.value,
            "item": self.item.to_dict(),
            "item_id": self.item_id,
            "payload_digest_sha256": self.payload_digest_sha256,
            "available_at": _timestamp(self.available_at),
            "attempt_count": self.attempt_count,
            "worker_id": self.worker_id,
            "lease_token": self.lease_token,
            "claimed_at": None if self.claimed_at is None else _timestamp(self.claimed_at),
            "error_code": None if self.error_code is None else self.error_code.value,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> _DistillationQueueRecord:
        try:
            decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
            value = _mapping(decoded)
            if set(value) != {
                "schema_version",
                "state",
                "item",
                "item_id",
                "payload_digest_sha256",
                "available_at",
                "attempt_count",
                "worker_id",
                "lease_token",
                "claimed_at",
                "error_code",
            } or value["schema_version"] != 1:
                raise ValueError("invalid distillation queue record")
            claimed_at = value["claimed_at"]
            result = cls.create(
                state=_string(value["state"]),
                item=DistillationWorkItem.from_dict(_mapping(value["item"])),
                item_id=_string(value["item_id"]),
                payload_digest_sha256=_string(value["payload_digest_sha256"]),
                available_at=_parse_timestamp(_string(value["available_at"])),
                attempt_count=_integer(value["attempt_count"]),
                worker_id=_optional_string(value["worker_id"]),
                lease_token=_optional_string(value["lease_token"]),
                claimed_at=(
                    None
                    if claimed_at is None
                    else _parse_timestamp(_string(claimed_at))
                ),
                error_code=_optional_string(value["error_code"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("invalid distillation queue record") from error
        if result.canonical_bytes() != payload:
            raise ValueError("non-canonical distillation queue record")
        return result


class FilesystemDistillationQueue:
    """A crash-recoverable durable queue for redacted distillation identities."""

    def __init__(self, root: Path, *, recover_processing: bool = True) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("queue root must be an absolute path")
        self._root = root
        self._active = root / "active"
        self._quarantine = root / "quarantine"
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._active.mkdir(mode=0o700, exist_ok=True)
        self._quarantine.mkdir(mode=0o700, exist_ok=True)
        if recover_processing:
            with self._locked():
                self._recover_processing()

    def enqueue(
        self,
        item: DistillationWorkItem,
        *,
        item_id: str,
        payload_digest: str,
    ) -> PutResult:
        record = _DistillationQueueRecord.create(
            state=QueueItemState.PENDING,
            item=item,
            item_id=item_id,
            payload_digest_sha256=payload_digest,
            available_at=_EPOCH,
        )
        with self._locked():
            existing = self._find_record(record.item_id)
            if existing is not None:
                if existing.payload_digest_sha256 == record.payload_digest_sha256:
                    return PutResult(PutDisposition.DUPLICATE, item_id, payload_digest)
                raise QueueImmutableConflictError("immutable distillation queue item conflict")
            self._write_record(self._active, record)
            return PutResult(PutDisposition.CREATED, item_id, payload_digest)

    def claim(self, *, worker_id: str, now: datetime) -> DistillationLease | None:
        if not isinstance(worker_id, str) or _DISTILLATION_ID.fullmatch(worker_id) is None:
            raise ValueError("invalid worker ID")
        current_time = _utc_datetime(now)
        with self._locked():
            pending = [
                record
                for record in self._active_records()
                if record.state is QueueItemState.PENDING
                and record.available_at <= current_time
            ]
            if not pending:
                return None
            record = min(
                pending,
                key=lambda candidate: (candidate.available_at, candidate.item_id),
            )
            processing = _DistillationQueueRecord.create(
                state=QueueItemState.PROCESSING,
                item=record.item,
                item_id=record.item_id,
                payload_digest_sha256=record.payload_digest_sha256,
                available_at=record.available_at,
                attempt_count=record.attempt_count,
                worker_id=worker_id,
                lease_token=secrets.token_hex(32),
                claimed_at=current_time,
            )
            self._write_record(self._active, processing)
            return DistillationLease.create(
                item=processing.item,
                item_id=processing.item_id,
                payload_digest_sha256=processing.payload_digest_sha256,
                worker_id=worker_id,
                lease_token=processing.lease_token or "",
                claimed_at=current_time,
            )

    def acknowledge(self, lease: DistillationLease, *, completed_at: datetime) -> None:
        _utc_datetime(completed_at)
        with self._locked():
            self._require_live_lease(lease)
            self._remove_path(self._active / _distillation_record_name(lease.item_id))

    def retry(
        self,
        lease: DistillationLease,
        *,
        available_at: datetime,
        error_code: str,
    ) -> None:
        next_available_at = _utc_datetime(available_at)
        _queue_error_code(error_code)
        with self._locked():
            record = self._require_live_lease(lease)
            next_attempt = record.attempt_count + 1
            if next_attempt >= MAX_ATTEMPTS:
                quarantined = _DistillationQueueRecord.create(
                    state=QueueItemState.QUARANTINED,
                    item=record.item,
                    item_id=record.item_id,
                    payload_digest_sha256=record.payload_digest_sha256,
                    available_at=next_available_at,
                    attempt_count=next_attempt,
                    error_code=QueueErrorCode.RETRY_EXHAUSTED,
                )
                self._write_record(self._quarantine, quarantined)
                self._remove_path(
                    self._active / _distillation_record_name(record.item_id)
                )
                return
            pending = _DistillationQueueRecord.create(
                state=QueueItemState.PENDING,
                item=record.item,
                item_id=record.item_id,
                payload_digest_sha256=record.payload_digest_sha256,
                available_at=next_available_at,
                attempt_count=next_attempt,
            )
            self._write_record(self._active, pending)

    def quarantine(
        self,
        lease: DistillationLease,
        *,
        at: datetime,
        error_code: str,
    ) -> None:
        quarantined_at = _utc_datetime(at)
        code = _queue_error_code(error_code)
        with self._locked():
            record = self._require_live_lease(lease)
            quarantined = _DistillationQueueRecord.create(
                state=QueueItemState.QUARANTINED,
                item=record.item,
                item_id=record.item_id,
                payload_digest_sha256=record.payload_digest_sha256,
                available_at=quarantined_at,
                attempt_count=record.attempt_count,
                error_code=code,
            )
            self._write_record(self._quarantine, quarantined)
            self._remove_path(self._active / _distillation_record_name(record.item_id))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self._root / ".queue.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _recover_processing(self) -> None:
        for record in self._active_records():
            if record.state is QueueItemState.PROCESSING:
                self._write_record(
                    self._active,
                    _DistillationQueueRecord.create(
                        state=QueueItemState.PENDING,
                        item=record.item,
                        item_id=record.item_id,
                        payload_digest_sha256=record.payload_digest_sha256,
                        available_at=record.available_at,
                        attempt_count=record.attempt_count,
                    ),
                )

    def _active_records(self) -> list[_DistillationQueueRecord]:
        records: list[_DistillationQueueRecord] = []
        for path in sorted(self._active.glob("*.json")):
            try:
                records.append(
                    _DistillationQueueRecord.from_canonical_bytes(path.read_bytes())
                )
            except (OSError, ValueError):
                self._quarantine_malformed(path)
        return records

    def _find_record(self, item_id: str) -> _DistillationQueueRecord | None:
        name = _distillation_record_name(item_id)
        for directory in (self._active, self._quarantine):
            path = directory / name
            if not path.exists():
                continue
            try:
                return _DistillationQueueRecord.from_canonical_bytes(path.read_bytes())
            except (OSError, ValueError):
                if directory == self._active:
                    self._quarantine_malformed(path)
                return None
        return None

    def _require_live_lease(
        self, lease: DistillationLease
    ) -> _DistillationQueueRecord:
        if not isinstance(lease, DistillationLease):
            raise QueueLeaseError("invalid queue lease")
        path = self._active / _distillation_record_name(lease.item_id)
        try:
            record = _DistillationQueueRecord.from_canonical_bytes(path.read_bytes())
        except (OSError, ValueError):
            raise QueueLeaseError("stale queue lease") from None
        if (
            record.state is not QueueItemState.PROCESSING
            or record.item != lease.item
            or record.item_id != lease.item_id
            or record.payload_digest_sha256 != lease.payload_digest_sha256
            or record.worker_id != lease.worker_id
            or record.lease_token != lease.lease_token
            or record.claimed_at != lease.claimed_at
        ):
            raise QueueLeaseError("stale queue lease")
        return record

    def _quarantine_malformed(self, path: Path) -> None:
        payload = path.read_bytes() if path.exists() else b""
        metadata = canonical_json_bytes(
            {
                "schema_version": 1,
                "payload_digest_sha256": sha256(payload).hexdigest(),
                "error_code": QueueErrorCode.INVALID_SCHEMA.value,
            }
        )
        name = "malformed-" + sha256(path.name.encode()).hexdigest() + ".json"
        self._write_bytes(self._quarantine, name, metadata)
        self._remove_path(path)

    def _write_record(
        self, directory: Path, record: _DistillationQueueRecord
    ) -> None:
        self._write_bytes(
            self._quarantine if directory == self._quarantine else self._active,
            _distillation_record_name(record.item_id),
            record.canonical_bytes(),
        )

    @staticmethod
    def _write_bytes(directory: Path, name: str, payload: bytes) -> None:
        temporary = directory / ("." + secrets.token_hex(16) + ".tmp")
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, directory / name)
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise QueueWriteError("durable queue write failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            path.unlink()
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise QueueWriteError("durable queue write failed") from error


def _distillation_record_name(item_id: str) -> str:
    if not isinstance(item_id, str) or _DISTILLATION_ID.fullmatch(item_id) is None:
        raise ValueError("invalid distillation queue item ID")
    return sha256(item_id.encode("utf-8")).hexdigest() + ".json"


def _record_name(item_id: str) -> str:
    validate_identifier(item_id, prefix="cap_")
    return item_id + ".json"


def read_pending_queue_snapshot(root: Path) -> PendingQueueSnapshot:
    """Read queue diagnostics without constructing or recovering the queue."""
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("queue root must be an absolute path")
    active = root / "active"
    try:
        metadata = os.lstat(active)
    except FileNotFoundError:
        return PendingQueueSnapshot(0, 0, None)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise QueueError("unsafe queue snapshot root")
    captured_at: list[datetime] = []
    malformed_count = 0
    for path in sorted(active.glob("*.json")):
        try:
            record = _QueueRecord.from_canonical_bytes(_read_snapshot_file(path))
        except (OSError, ValueError):
            malformed_count += 1
            continue
        if record.state is QueueItemState.PENDING:
            captured_at.append(record.item.envelope.captured_at)
    return PendingQueueSnapshot(
        pending_count=len(captured_at),
        malformed_count=malformed_count,
        oldest_captured_at=min(captured_at) if captured_at else None,
    )


def _write_all(file_descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(file_descriptor, remaining)
        if written <= 0:
            raise OSError("short queue write")
        remaining = remaining[written:]


def _read_snapshot_file(path: Path) -> bytes:
    file_descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("unsafe queue snapshot record")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(file_descriptor)


def _queue_error_code(value: str) -> QueueErrorCode:
    try:
        return QueueErrorCode(value)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid queue error code") from error


def _malformed_metadata(payload: bytes) -> tuple[str, str, QueueErrorCode]:
    try:
        decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return "unknown", "unknown", QueueErrorCode.INVALID_SCHEMA
    if not isinstance(decoded, dict):
        return "unknown", "unknown", QueueErrorCode.INVALID_SCHEMA
    item_id = decoded.get("item_id")
    digest = decoded.get("payload_digest_sha256")
    try:
        safe_item_id = (
            validate_identifier(item_id, prefix="cap_") if isinstance(item_id, str) else "unknown"
        )
    except ValueError:
        safe_item_id = "unknown"
    safe_digest = (
        digest
        if isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        else "unknown"
    )
    if safe_digest == "unknown" and digest is not None:
        return safe_item_id, safe_digest, QueueErrorCode.INVALID_DIGEST
    if decoded.get("schema_version") != 1:
        return safe_item_id, safe_digest, QueueErrorCode.INVALID_SCHEMA
    return safe_item_id, safe_digest, QueueErrorCode.INVALID_ITEM


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid queue record")
    return cast(Mapping[str, object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid queue record")
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid queue record")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid queue record")
    return value
