"""Confined durable storage for metadata-only scheduler run records."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    StorageError,
    WriteState,
    atomic_write_new,
    read_confined,
)

from .runlog import RunMetadata
from .scheduler import EXPECTED_JOB_IDS

_MAXIMUM_RECORD_BYTES = 16 * 1024
_MAXIMUM_RECORDS = 4_096


class RunLogStoreError(RuntimeError):
    """Run metadata could not be persisted or restored exactly."""


class FilesystemRunLogStore:
    def __init__(self, *, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise RunLogStoreError("invalid run-log root")
        self._root = root

    def append(self, metadata: RunMetadata) -> str:
        if not isinstance(metadata, RunMetadata):
            raise RunLogStoreError("invalid run metadata")
        payload = canonical_json_bytes(metadata.to_dict())
        digest = sha256(payload).hexdigest()
        relative = _record_path(metadata.job_id, digest)
        try:
            state = atomic_write_new(root=self._root, relative=relative, data=payload)
            restored = read_confined(root=self._root, relative=relative)
        except (DuplicateConflictError, StorageError):
            raise RunLogStoreError("run metadata persistence failed") from None
        if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS} or restored != payload:
            raise RunLogStoreError("run metadata persistence failed")
        return digest

    def reports(self, *, now: datetime, window_seconds: int) -> tuple[RunMetadata, ...]:
        current = _utc(now)
        if (
            not isinstance(window_seconds, int)
            or isinstance(window_seconds, bool)
            or not 1 <= window_seconds <= 604_800
        ):
            raise RunLogStoreError("invalid run-log window")
        cutoff = current - timedelta(seconds=window_seconds)
        records: list[RunMetadata] = []
        count = 0
        for job_id in EXPECTED_JOB_IDS:
            directory = self._root / "runlog" / job_id
            try:
                metadata = directory.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RunLogStoreError("invalid run metadata")
            for path in sorted(directory.glob("*.json")):
                count += 1
                if count > _MAXIMUM_RECORDS:
                    raise RunLogStoreError("run metadata limit exceeded")
                relative = PurePosixPath("runlog", job_id, path.name)
                try:
                    payload = read_confined(root=self._root, relative=relative)
                    record = _record_from_bytes(payload)
                except (StorageError, ValueError):
                    raise RunLogStoreError("invalid run metadata") from None
                expected_name = sha256(canonical_json_bytes(record.to_dict())).hexdigest() + ".json"
                if path.name != expected_name:
                    raise RunLogStoreError("invalid run metadata")
                if cutoff <= record.finished_at <= current:
                    records.append(record)
        return tuple(
            sorted(records, key=lambda item: (item.finished_at, item.job_id, item.started_at))
        )


def _record_path(job_id: str, digest: str) -> PurePosixPath:
    if job_id not in EXPECTED_JOB_IDS:
        raise RunLogStoreError("invalid run metadata")
    return PurePosixPath("runlog", job_id, digest + ".json")


def _record_from_bytes(payload: bytes | None) -> RunMetadata:
    if payload is None or not payload or len(payload) > _MAXIMUM_RECORD_BYTES:
        raise RunLogStoreError("invalid run metadata")
    try:
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        record = RunMetadata.from_dict(value)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise RunLogStoreError("invalid run metadata") from None
    if canonical_json_bytes(record.to_dict()) != payload:
        raise RunLogStoreError("invalid run metadata")
    return record


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RunLogStoreError("invalid run-log time")
    return value.astimezone(UTC)
