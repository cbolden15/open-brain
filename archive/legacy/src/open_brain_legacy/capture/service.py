"""Durable, privacy-first orchestration for capture work."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256

from open_brain_engine.capture.models import (
    CaptureLease,
    CaptureRedactionResult,
    CaptureWorkItem,
    DistillationLease,
    DistillationWorkItem,
    ExtractionState,
    Extractor,
    NormalizedExtraction,
    QueueErrorCode,
)
from open_brain_engine.capture.redaction import REDACTION_POLICY_VERSION, VersionedCaptureRedactor
from open_brain_engine.core.models import (
    CaptureEnvelope,
    PrivacyReason,
    RawCapture,
    SourceType,
)
from open_brain_engine.core.ports import (
    CaptureQueue,
    Clock,
    EventRecord,
    EventStore,
    IdGenerator,
    PutDisposition,
    RawStore,
)

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeExtractionRequest,
)
from open_brain_legacy.capture.extractors.social import SocialExtractionRequest

_PRIVATE_HOLD_REASONS = frozenset(
    {
        PrivacyReason.PERSONAL_LOCAL_ONLY,
        PrivacyReason.SECRET_DETECTED,
        PrivacyReason.CLASSIFICATION_MISSING,
        PrivacyReason.CLASSIFICATION_INVALID,
        PrivacyReason.CLASSIFICATION_AMBIGUOUS,
        PrivacyReason.EXPLICIT_LOCAL_ONLY,
    }
)
_SUCCESSFUL_EXTRACTIONS = frozenset(
    {
        ExtractionState.COMPLETE,
        ExtractionState.NO_CONTENT,
    }
)


class ProcessStatus(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    RETRY_SCHEDULED = "retry_scheduled"
    QUARANTINED = "quarantined"
    RECOVERY_PENDING = "recovery_pending"


class CaptureService:
    def __init__(
        self,
        *,
        intake_queue: CaptureQueue[CaptureWorkItem, CaptureLease],
        private_hold_queue: CaptureQueue[CaptureWorkItem, CaptureLease],
        distillation_queue: CaptureQueue[DistillationWorkItem, DistillationLease],
        raw_store: RawStore,
        event_store: EventStore,
        extractors: Mapping[SourceType, Extractor[object]],
        redactor: VersionedCaptureRedactor | None = None,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        if redactor is not None and type(redactor) is not VersionedCaptureRedactor:
            raise ValueError("approved capture redactor required")
        self._intake_queue = intake_queue
        self._private_hold_queue = private_hold_queue
        self._distillation_queue = distillation_queue
        self._raw_store = raw_store
        self._event_store = event_store
        self._extractors = dict(extractors)
        self._redactor = redactor if redactor is not None else VersionedCaptureRedactor()
        self._clock = clock
        self._ids = ids

    def process_one(self, *, worker_id: str) -> ProcessStatus | None:
        lease = self._intake_queue.claim(worker_id=worker_id, now=self._clock.now())
        if lease is None:
            return None
        capture = RawCapture.create(envelope=lease.item.envelope, assets=())
        raw_status = self._persist_raw(lease, capture)
        if raw_status is not None:
            return raw_status
        if lease.item.envelope.privacy_decision.reason in _PRIVATE_HOLD_REASONS:
            return self._persist_private_hold(lease)
        return self._persist_authorized_work(lease)

    def _persist_raw(self, lease: CaptureLease, capture: RawCapture) -> ProcessStatus | None:
        try:
            result = self._raw_store.put_if_absent(capture)
        except Exception:
            return self._raw_failure_status(lease, capture)
        if result.disposition is PutDisposition.DUPLICATE:
            try:
                existing = self._raw_store.get(capture.envelope.capture_id)
            except Exception:
                return self._retry(lease, QueueErrorCode.DURABILITY_FAILED)
            if existing != capture:
                return self._quarantine(lease, QueueErrorCode.IMMUTABLE_CONFLICT)
        return None

    def _raw_failure_status(self, lease: CaptureLease, capture: RawCapture) -> ProcessStatus:
        try:
            existing = self._raw_store.get(capture.envelope.capture_id)
        except Exception:
            existing = None
        if existing is not None and existing != capture:
            return self._quarantine(lease, QueueErrorCode.IMMUTABLE_CONFLICT)
        return self._retry(lease, QueueErrorCode.DURABILITY_FAILED)

    def _persist_private_hold(self, lease: CaptureLease) -> ProcessStatus:
        try:
            self._private_hold_queue.enqueue(
                lease.item,
                item_id=lease.item_id,
                payload_digest=lease.item.payload_digest_sha256(),
            )
        except Exception:
            return self._retry(lease, QueueErrorCode.DURABILITY_FAILED)
        return self._acknowledge(lease)

    def _persist_authorized_work(self, lease: CaptureLease) -> ProcessStatus:
        envelope = lease.item.envelope
        existing = self._existing_event(lease)
        if isinstance(existing, ProcessStatus):
            return existing
        if existing is not None:
            return self._enqueue_distillation(lease, existing)
        extraction = self._extract(envelope)
        if extraction is None:
            return self._retry(lease, QueueErrorCode.EXTRACTION_FAILED)
        redaction = self._redact(extraction, envelope)
        if redaction is None:
            return self._retry(lease, QueueErrorCode.REDACTION_FAILED)
        record = self._event_record(envelope, redaction)
        if record is None:
            return self._retry(lease, QueueErrorCode.REDACTION_FAILED)
        try:
            self._event_store.append(record)
        except Exception:
            return self._retry(lease, QueueErrorCode.DURABILITY_FAILED)
        return self._enqueue_distillation(lease, record)

    def _existing_event(self, lease: CaptureLease) -> EventRecord | ProcessStatus | None:
        envelope = lease.item.envelope
        try:
            relevant = tuple(
                record
                for record in self._event_store.read(envelope.capture_id)
                if record.event_type == "capture.extracted"
            )
        except Exception:
            return self._retry(lease, QueueErrorCode.DURABILITY_FAILED)
        if not relevant:
            return None
        if len(relevant) != 1:
            return self._quarantine(lease, QueueErrorCode.IMMUTABLE_CONFLICT)
        record = relevant[0]
        if (
            record.stream_id != envelope.capture_id
            or record.occurred_at != envelope.captured_at
            or record.privacy_decision != envelope.privacy_decision
            or record.redaction_receipt.policy_version != REDACTION_POLICY_VERSION
        ):
            return self._quarantine(lease, QueueErrorCode.IMMUTABLE_CONFLICT)
        return record

    def _enqueue_distillation(self, lease: CaptureLease, record: EventRecord) -> ProcessStatus:
        try:
            item = DistillationWorkItem.create(
                capture_id=record.stream_id,
                event_id=record.event_id,
                redacted_event_digest_sha256=sha256(record.canonical_bytes()).hexdigest(),
            )
            self._distillation_queue.enqueue(
                item,
                item_id=item.event_id,
                payload_digest=item.payload_digest_sha256(),
            )
        except Exception:
            return self._retry(lease, QueueErrorCode.DURABILITY_FAILED)
        return self._acknowledge(lease)

    def _extract(self, envelope: CaptureEnvelope) -> NormalizedExtraction | None:
        extractor = self._extractors.get(envelope.source_type)
        if extractor is None:
            return None
        try:
            extraction = extractor.extract(
                _request_for(envelope), privacy=envelope.privacy_decision
            )
        except Exception:
            return None
        if (
            extraction.state not in _SUCCESSFUL_EXTRACTIONS
            or extraction.assets
            or extraction.source_type is not envelope.source_type
        ):
            return None
        return extraction

    def _redact(
        self,
        extraction: NormalizedExtraction,
        envelope: CaptureEnvelope,
    ) -> CaptureRedactionResult | None:
        try:
            redaction = self._redactor.redact(extraction, envelope)
            source_digest = sha256(extraction.canonical_bytes()).hexdigest()
            if (
                redaction.receipt.source_digest_sha256 != source_digest
                or redaction.receipt.policy_version != REDACTION_POLICY_VERSION
            ):
                return None
            return redaction
        except Exception:
            return None

    def _event_record(
        self,
        envelope: CaptureEnvelope,
        redaction: CaptureRedactionResult,
    ) -> EventRecord | None:
        try:
            payload_digest = EventRecord.output_digest_sha256(redaction.payload)
            return EventRecord.create(
                event_id=self._ids.event_id(
                    str(envelope.capture_id),
                    "capture.extracted",
                    payload_digest,
                ),
                stream_id=envelope.capture_id,
                event_type="capture.extracted",
                occurred_at=envelope.captured_at,
                privacy_decision=envelope.privacy_decision,
                payload=redaction.payload,
                redaction_receipt=redaction.receipt,
            )
        except Exception:
            return None

    def _acknowledge(self, lease: CaptureLease) -> ProcessStatus:
        try:
            self._intake_queue.acknowledge(lease, completed_at=self._clock.now())
        except Exception:
            return ProcessStatus.RECOVERY_PENDING
        return ProcessStatus.ACKNOWLEDGED

    def _retry(self, lease: CaptureLease, code: QueueErrorCode) -> ProcessStatus:
        try:
            self._intake_queue.retry(
                lease,
                available_at=self._clock.now(),
                error_code=code.value,
            )
        except Exception:
            return ProcessStatus.RECOVERY_PENDING
        return ProcessStatus.RETRY_SCHEDULED

    def _quarantine(self, lease: CaptureLease, code: QueueErrorCode) -> ProcessStatus:
        try:
            self._intake_queue.quarantine(
                lease,
                at=self._clock.now(),
                error_code=code.value,
            )
        except Exception:
            return ProcessStatus.RECOVERY_PENDING
        return ProcessStatus.QUARANTINED


def _request_for(envelope: CaptureEnvelope) -> object:
    if envelope.source_type in {SourceType.TEXT, SourceType.WEB}:
        return ExtractionRequest(
            capture_id=str(envelope.capture_id),
            url=envelope.source_url or "",
            text=envelope.shared_text,
        )
    if envelope.source_type is SourceType.YOUTUBE:
        return YouTubeExtractionRequest(
            url=envelope.source_url or "", supplied_transcript=envelope.shared_text
        )
    return SocialExtractionRequest(
        url=envelope.source_url or "",
        supplied_text=envelope.shared_text,
    )
