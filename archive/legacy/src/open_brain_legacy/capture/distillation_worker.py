"""Crash-recoverable worker for exact-event capture distillation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, cast

from open_brain_engine.capture.models import (
    DistillationLease,
    DistillationWorkItem,
    NormalizedExtraction,
    QueueErrorCode,
)
from open_brain_engine.core.ids import CaptureId
from open_brain_engine.core.models import CaptureEnvelope, PrivacyDecision
from open_brain_engine.core.ports import (
    CaptureQueue,
    Clock,
    EventRecord,
    EventStore,
    PutDisposition,
    PutResult,
    RawStore,
)

from open_brain_legacy.capture.distillation import (
    DistillationInput,
    DistillationService,
    DistilledCapture,
)


class DistillationPublisher(Protocol):
    """Publish one event-bound distillation before its queue item is acknowledged."""

    def publish(
        self,
        *,
        envelope: CaptureEnvelope,
        extraction: NormalizedExtraction,
        distilled: DistilledCapture,
    ) -> PutResult | None: ...


class DistillationProcessStatus(StrEnum):
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    QUARANTINED = "quarantined"
    RECOVERY_PENDING = "recovery_pending"


class DistillationWorker:
    """Bind queued identity to raw/event stores before invoking one provider."""

    def __init__(
        self,
        *,
        queue: CaptureQueue[DistillationWorkItem, DistillationLease],
        raw_store: RawStore,
        event_store: EventStore,
        service: DistillationService,
        clock: Clock,
        publisher: DistillationPublisher | None = None,
        retry_delay_seconds: int = 30,
    ) -> None:
        if (
            not isinstance(service, DistillationService)
            or not isinstance(retry_delay_seconds, int)
            or isinstance(retry_delay_seconds, bool)
            or not 1 <= retry_delay_seconds <= 3_600
        ):
            raise ValueError("invalid distillation worker configuration")
        self._queue = queue
        self._raw_store = raw_store
        self._event_store = event_store
        self._service = service
        self._clock = clock
        self._publisher = publisher
        self._retry_delay_seconds = retry_delay_seconds

    def process_one(self, *, worker_id: str) -> DistillationProcessStatus | None:
        try:
            lease = self._queue.claim(worker_id=worker_id, now=self._clock.now())
        except Exception:
            return DistillationProcessStatus.RECOVERY_PENDING
        if lease is None:
            return None
        bound = self._bind(lease.item)
        if bound is None:
            return self._quarantine(lease)
        item, privacy, envelope = bound
        try:
            result = self._service.distill(item, privacy=privacy)
        except Exception:
            return self._retry(lease)
        if result.value is None or result.error_code is not None:
            return self._retry(lease)
        if self._publisher is not None:
            try:
                receipt = self._publisher.publish(
                    envelope=envelope,
                    extraction=item.extraction,
                    distilled=result.value,
                )
                if receipt is not None and (
                    not isinstance(receipt, PutResult)
                    or receipt.record_id != result.value.capture_id
                    or receipt.disposition
                    not in {PutDisposition.CREATED, PutDisposition.DUPLICATE}
                ):
                    raise ValueError("invalid publication receipt")
            except Exception:
                return self._retry(lease)
        try:
            self._queue.acknowledge(lease, completed_at=self._clock.now())
        except Exception:
            return DistillationProcessStatus.RECOVERY_PENDING
        return DistillationProcessStatus.COMPLETED

    def _bind(
        self, item: DistillationWorkItem
    ) -> tuple[DistillationInput, PrivacyDecision, CaptureEnvelope] | None:
        try:
            capture_id = CaptureId(str(item.capture_id))
            raw = self._raw_store.get(capture_id)
            if raw is None or raw.envelope.capture_id != capture_id:
                return None
            records = tuple(
                record
                for record in self._event_store.read(capture_id)
                if record.event_id == item.event_id
            )
            if len(records) != 1:
                return None
            record = records[0]
            if (
                record.stream_id != capture_id
                or record.event_type != "capture.extracted"
                or record.occurred_at != raw.envelope.captured_at
                or record.privacy_decision != raw.envelope.privacy_decision
                or sha256(record.canonical_bytes()).hexdigest()
                != item.redacted_event_digest_sha256
            ):
                return None
            extraction = _extraction_from_event(record)
            distilled = DistillationInput.create(
                capture_id=str(capture_id),
                capture_why=raw.envelope.capture_why,
                extraction=extraction,
            )
            return distilled, record.privacy_decision, raw.envelope
        except Exception:
            return None

    def _retry(self, lease: DistillationLease) -> DistillationProcessStatus:
        try:
            self._queue.retry(
                lease,
                available_at=self._clock.now()
                + timedelta(seconds=self._retry_delay_seconds),
                error_code=QueueErrorCode.RETRYABLE_FAILURE.value,
            )
        except Exception:
            return DistillationProcessStatus.RECOVERY_PENDING
        return DistillationProcessStatus.RETRY_SCHEDULED

    def _quarantine(self, lease: DistillationLease) -> DistillationProcessStatus:
        try:
            self._queue.quarantine(
                lease,
                at=self._clock.now(),
                error_code=QueueErrorCode.IMMUTABLE_CONFLICT.value,
            )
        except Exception:
            return DistillationProcessStatus.RECOVERY_PENDING
        return DistillationProcessStatus.QUARANTINED


def _extraction_from_event(record: EventRecord) -> NormalizedExtraction:
    payload = _mapping(record.to_dict()["payload"])
    if set(payload) != {
        "extractor",
        "state",
        "source_type",
        "content_kind",
        "metadata",
        "text",
        "transcript",
        "transcript_state",
    }:
        raise ValueError("invalid redacted extraction event")
    value = dict(payload)
    value["assets"] = []
    value["failure"] = None
    return NormalizedExtraction.from_dict(value)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid redacted extraction event")
    return cast(Mapping[str, object], value)


__all__ = ["DistillationProcessStatus", "DistillationPublisher", "DistillationWorker"]
