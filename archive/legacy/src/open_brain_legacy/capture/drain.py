"""Bounded, replay-safe draining for social and web capture queues."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from open_brain_engine.capture.models import (
    CaptureLease,
    CaptureWorkItem,
    ExtractionFailure,
    ExtractionState,
    Extractor,
    NormalizedExtraction,
    QueueErrorCode,
)
from open_brain_engine.core.ids import canonical_json_bytes, validate_identifier
from open_brain_engine.core.models import SourceType
from open_brain_engine.core.ports import CaptureQueue, PutDisposition, PutResult

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest
from open_brain_legacy.capture.extractors.social import SocialExtractionRequest


class DrainItemState(StrEnum):
    COMPLETE = "complete"
    STUBBED = "stubbed"
    PRIVACY_HOLD = "privacy_hold"


class DrainProcessStatus(StrEnum):
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    STUBBED = "stubbed"
    PRIVACY_HOLD = "privacy_hold"
    QUARANTINED = "quarantined"
    RECOVERY_PENDING = "recovery_pending"


@dataclass(frozen=True, slots=True)
class DrainOutcome:
    schema_version: int
    capture_id: str
    source_type: SourceType
    state: DrainItemState
    capture_why: str
    attempt_count: int
    failure_code: str | None
    extraction: NormalizedExtraction | None

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        source_type: SourceType | str,
        state: DrainItemState | str,
        capture_why: str,
        attempt_count: int,
        failure_code: str | None,
        extraction: NormalizedExtraction | None,
    ) -> DrainOutcome:
        try:
            validate_identifier(capture_id, prefix="cap_")
            normalized_source = SourceType(source_type)
            normalized_state = DrainItemState(state)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid drain outcome") from error
        if (
            normalized_source not in {SourceType.SOCIAL, SourceType.WEB}
            or not isinstance(capture_why, str)
            or len(capture_why) > 2_000
            or not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 0
        ):
            raise ValueError("invalid drain outcome")
        if normalized_state is DrainItemState.COMPLETE:
            if (
                not isinstance(extraction, NormalizedExtraction)
                or extraction.state
                not in {ExtractionState.COMPLETE, ExtractionState.NO_CONTENT}
                or extraction.source_type is not normalized_source
                or failure_code is not None
                or attempt_count < 1
            ):
                raise ValueError("invalid drain outcome")
        elif normalized_state is DrainItemState.STUBBED:
            if extraction is not None or not failure_code or attempt_count < 1:
                raise ValueError("invalid drain outcome")
        elif extraction is not None or failure_code is not None:
            raise ValueError("invalid drain outcome")
        return cls(
            1,
            capture_id,
            normalized_source,
            normalized_state,
            capture_why,
            attempt_count,
            failure_code,
            extraction,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "capture_id": self.capture_id,
            "source_type": self.source_type.value,
            "state": self.state.value,
            "capture_why": self.capture_why,
            "attempt_count": self.attempt_count,
            "failure_code": self.failure_code,
            "extraction": None if self.extraction is None else self.extraction.to_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DrainOutcome:
        if set(value) != {
            "schema_version",
            "capture_id",
            "source_type",
            "state",
            "capture_why",
            "attempt_count",
            "failure_code",
            "extraction",
        } or value["schema_version"] != 1:
            raise ValueError("invalid drain outcome")
        raw_extraction = value["extraction"]
        return cls.create(
            capture_id=_string(value["capture_id"]),
            source_type=_string(value["source_type"]),
            state=_string(value["state"]),
            capture_why=_string(value["capture_why"]),
            attempt_count=_integer(value["attempt_count"]),
            failure_code=_optional_string(value["failure_code"]),
            extraction=None
            if raw_extraction is None
            else NormalizedExtraction.from_dict(_mapping(raw_extraction)),
        )


class DrainOutcomeStore(Protocol):
    def get(self, capture_id: str) -> DrainOutcome | None: ...

    def put_if_absent(self, outcome: DrainOutcome) -> PutResult: ...


class FilesystemDrainOutcomeStore:
    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("drain outcome root must be absolute")
        self._root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def get(self, capture_id: str) -> DrainOutcome | None:
        path = self._path(capture_id)
        if not path.exists():
            return None
        return DrainOutcome.from_dict(_decode_mapping(path.read_bytes()))

    def put_if_absent(self, outcome: DrainOutcome) -> PutResult:
        if not isinstance(outcome, DrainOutcome):
            raise ValueError("invalid drain outcome")
        path = self._path(outcome.capture_id)
        payload = outcome.canonical_bytes()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self.get(outcome.capture_id)
            if existing != outcome:
                raise ValueError("immutable drain outcome conflict") from None
            return PutResult(
                PutDisposition.DUPLICATE,
                outcome.capture_id,
                sha256(payload).hexdigest(),
            )
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return PutResult(PutDisposition.CREATED, outcome.capture_id, sha256(payload).hexdigest())

    def _path(self, capture_id: str) -> Path:
        validate_identifier(capture_id, prefix="cap_")
        return self._root / (capture_id + ".json")


class CaptureDrain:
    def __init__(
        self,
        *,
        queue: CaptureQueue[CaptureWorkItem, CaptureLease],
        outcome_store: DrainOutcomeStore,
        extractors: Mapping[SourceType, Extractor[object]],
        clock: Callable[[], datetime],
        max_attempts: int = 3,
    ) -> None:
        if (
            not callable(clock)
            or not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 20
        ):
            raise ValueError("invalid drain configuration")
        self._queue = queue
        self._outcome_store = outcome_store
        self._extractors = dict(extractors)
        self._clock = clock
        self._max_attempts = max_attempts

    def process_one(self, *, worker_id: str) -> DrainProcessStatus | None:
        now = self._now()
        lease = self._queue.claim(worker_id=worker_id, now=now)
        if lease is None:
            return None
        envelope = lease.item.envelope
        existing = self._outcome_store.get(str(envelope.capture_id))
        if existing is not None:
            if (
                existing.source_type is not envelope.source_type
                or existing.capture_why != envelope.capture_why
            ):
                return self._quarantine(lease)
            return self._acknowledge(lease, _status_for(existing.state))
        if envelope.source_type not in {SourceType.SOCIAL, SourceType.WEB}:
            return self._quarantine(lease)
        if not envelope.privacy_decision.authority.external_egress:
            outcome = DrainOutcome.create(
                capture_id=str(envelope.capture_id),
                source_type=envelope.source_type,
                state=DrainItemState.PRIVACY_HOLD,
                capture_why=envelope.capture_why,
                attempt_count=lease.item.attempt_count,
                failure_code=None,
                extraction=None,
            )
            return self._persist_terminal(lease, outcome, DrainProcessStatus.PRIVACY_HOLD)
        extractor = self._extractors.get(envelope.source_type)
        if extractor is None:
            return self._failed(lease, ExtractionFailure.TOOL_UNAVAILABLE.value)
        try:
            extraction = extractor.extract(
                _request_for(lease.item), privacy=envelope.privacy_decision
            )
        except Exception:
            return self._failed(lease, ExtractionFailure.FETCH_FAILED.value)
        if (
            extraction.state in {ExtractionState.COMPLETE, ExtractionState.NO_CONTENT}
            and extraction.source_type is envelope.source_type
        ):
            outcome = DrainOutcome.create(
                capture_id=str(envelope.capture_id),
                source_type=envelope.source_type,
                state=DrainItemState.COMPLETE,
                capture_why=envelope.capture_why,
                attempt_count=lease.item.attempt_count + 1,
                failure_code=None,
                extraction=extraction,
            )
            return self._persist_terminal(lease, outcome, DrainProcessStatus.COMPLETED)
        failure = (
            extraction.failure.value
            if extraction.failure is not None
            else ExtractionFailure.MALFORMED_TOOL_OUTPUT.value
        )
        return self._failed(lease, failure)

    def _failed(self, lease: CaptureLease, failure_code: str) -> DrainProcessStatus:
        next_attempt = lease.item.attempt_count + 1
        if next_attempt < self._max_attempts:
            try:
                self._queue.retry(
                    lease,
                    available_at=self._now(),
                    error_code=QueueErrorCode.RETRYABLE_FAILURE.value,
                )
            except Exception:
                return DrainProcessStatus.RECOVERY_PENDING
            return DrainProcessStatus.RETRY_SCHEDULED
        outcome = DrainOutcome.create(
            capture_id=str(lease.item.envelope.capture_id),
            source_type=lease.item.envelope.source_type,
            state=DrainItemState.STUBBED,
            capture_why=lease.item.envelope.capture_why,
            attempt_count=next_attempt,
            failure_code=failure_code,
            extraction=None,
        )
        return self._persist_terminal(lease, outcome, DrainProcessStatus.STUBBED)

    def _persist_terminal(
        self,
        lease: CaptureLease,
        outcome: DrainOutcome,
        status: DrainProcessStatus,
    ) -> DrainProcessStatus:
        try:
            self._outcome_store.put_if_absent(outcome)
        except Exception:
            return DrainProcessStatus.RECOVERY_PENDING
        return self._acknowledge(lease, status)

    def _acknowledge(
        self, lease: CaptureLease, status: DrainProcessStatus
    ) -> DrainProcessStatus:
        try:
            self._queue.acknowledge(lease, completed_at=self._now())
        except Exception:
            return DrainProcessStatus.RECOVERY_PENDING
        return status

    def _quarantine(self, lease: CaptureLease) -> DrainProcessStatus:
        try:
            self._queue.quarantine(
                lease,
                at=self._now(),
                error_code=QueueErrorCode.IMMUTABLE_CONFLICT.value,
            )
        except Exception:
            return DrainProcessStatus.RECOVERY_PENDING
        return DrainProcessStatus.QUARANTINED

    def _now(self) -> datetime:
        return self._clock()


def _request_for(item: CaptureWorkItem) -> object:
    envelope = item.envelope
    if envelope.source_type is SourceType.SOCIAL:
        return SocialExtractionRequest(url=envelope.source_url or "")
    return ExtractionRequest(
        capture_id=str(envelope.capture_id),
        url=envelope.source_url or "",
        text=envelope.shared_text,
    )


def _status_for(state: DrainItemState) -> DrainProcessStatus:
    return {
        DrainItemState.COMPLETE: DrainProcessStatus.COMPLETED,
        DrainItemState.STUBBED: DrainProcessStatus.STUBBED,
        DrainItemState.PRIVACY_HOLD: DrainProcessStatus.PRIVACY_HOLD,
    }[state]


def _decode_mapping(payload: bytes) -> Mapping[str, object]:
    import json

    try:
        return _mapping(json.loads(payload.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid drain outcome") from error


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid mapping")
    return cast(Mapping[str, object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid string")
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid string")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid integer")
    return value


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short drain-outcome write")
        remaining = remaining[written:]


__all__ = [
    "CaptureDrain",
    "DrainItemState",
    "DrainOutcome",
    "DrainOutcomeStore",
    "DrainProcessStatus",
    "FilesystemDrainOutcomeStore",
]
