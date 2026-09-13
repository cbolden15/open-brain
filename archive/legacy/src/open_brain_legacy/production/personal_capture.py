"""Strictly local processing and curation for personal capture holds."""

from __future__ import annotations

import html
from datetime import timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath

from open_brain_engine.capture.models import (
    CaptureLease,
    CaptureWorkItem,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    QueueErrorCode,
    TranscriptState,
)
from open_brain_engine.core.models import (
    PrivacyReason,
    PrivacyTier,
    RawCapture,
    SourceType,
)
from open_brain_engine.core.ports import CaptureQueue, Clock, RawStore
from open_brain_engine.storage.filesystem import atomic_write_new

from open_brain_legacy._compat.open_brain_connectors.capture.extractors import ExtractionRequest
from open_brain_legacy.capture.distillation import (
    DistillationInput,
    DistillationService,
    DistilledCapture,
)
from open_brain_legacy.capture.extractors.text import TextExtractor

_PERSONAL_REASONS = {
    PrivacyReason.PERSONAL_LOCAL_ONLY,
    PrivacyReason.EXPLICIT_LOCAL_ONLY,
}


class PersonalCaptureStatus(StrEnum):
    COMPLETED = "completed"
    HELD = "held"
    RETRY_SCHEDULED = "retry_scheduled"
    QUARANTINED = "quarantined"
    RECOVERY_PENDING = "recovery_pending"


class PersonalCaptureWorker:
    """Process only local personal bytes; all other classifications stay held."""

    def __init__(
        self,
        *,
        queue: CaptureQueue[CaptureWorkItem, CaptureLease],
        classification_hold: CaptureQueue[CaptureWorkItem, CaptureLease],
        raw_store: RawStore,
        distillation: DistillationService,
        personal_root: Path,
        clock: Clock,
    ) -> None:
        if not isinstance(distillation, DistillationService) or not isinstance(
            personal_root, Path
        ):
            raise ValueError("invalid personal capture worker")
        self._queue = queue
        self._classification_hold = classification_hold
        self._raw_store = raw_store
        self._distillation = distillation
        self._personal_root = personal_root
        self._clock = clock

    def process_one(self, *, worker_id: str) -> PersonalCaptureStatus | None:
        try:
            lease = self._queue.claim(worker_id=worker_id, now=self._clock.now())
        except Exception:
            return PersonalCaptureStatus.RECOVERY_PENDING
        if lease is None:
            return None
        try:
            raw = self._raw_store.get(lease.item.envelope.capture_id)
        except Exception:
            return self._retry(lease)
        if raw is None or raw.envelope != lease.item.envelope:
            return self._quarantine(lease)
        privacy = raw.envelope.privacy_decision
        if privacy.tier is not PrivacyTier.PERSONAL or privacy.reason not in _PERSONAL_REASONS:
            return self._hold(lease)
        extraction = _local_extraction(raw)
        if extraction is None:
            return self._hold(lease)
        try:
            distilled = self._distillation.distill(
                DistillationInput.create(
                    capture_id=str(raw.envelope.capture_id),
                    capture_why=raw.envelope.capture_why,
                    extraction=extraction,
                ),
                privacy=privacy,
            )
        except Exception:
            return self._retry(lease)
        if distilled.error_code is not None or distilled.value is None:
            return self._retry(lease)
        try:
            _write_personal_capture(
                root=self._personal_root,
                raw=raw,
                extraction=extraction,
                distilled=distilled.value,
            )
            self._queue.acknowledge(lease, completed_at=self._clock.now())
        except Exception:
            return self._retry(lease)
        return PersonalCaptureStatus.COMPLETED

    def _hold(self, lease: CaptureLease) -> PersonalCaptureStatus:
        try:
            self._classification_hold.enqueue(
                lease.item,
                item_id=lease.item_id,
                payload_digest=lease.item.payload_digest_sha256(),
            )
            self._queue.acknowledge(lease, completed_at=self._clock.now())
        except Exception:
            return PersonalCaptureStatus.RECOVERY_PENDING
        return PersonalCaptureStatus.HELD

    def _retry(self, lease: CaptureLease) -> PersonalCaptureStatus:
        try:
            self._queue.retry(
                lease,
                available_at=self._clock.now() + timedelta(seconds=30),
                error_code=QueueErrorCode.RETRYABLE_FAILURE.value,
            )
        except Exception:
            return PersonalCaptureStatus.RECOVERY_PENDING
        return PersonalCaptureStatus.RETRY_SCHEDULED

    def _quarantine(self, lease: CaptureLease) -> PersonalCaptureStatus:
        try:
            self._queue.quarantine(
                lease,
                at=self._clock.now(),
                error_code=QueueErrorCode.IMMUTABLE_CONFLICT.value,
            )
        except Exception:
            return PersonalCaptureStatus.RECOVERY_PENDING
        return PersonalCaptureStatus.QUARANTINED


def _local_extraction(raw: RawCapture) -> NormalizedExtraction | None:
    envelope = raw.envelope
    if raw.assets:
        return None
    if envelope.source_type is SourceType.TEXT:
        extraction = TextExtractor().extract(
            ExtractionRequest(
                capture_id=str(envelope.capture_id),
                text=envelope.shared_text,
            ),
            privacy=envelope.privacy_decision,
        )
        return extraction if extraction.state is ExtractionState.COMPLETE else None
    if not envelope.shared_text.strip() or envelope.source_url is None:
        return None
    extractor = {
        SourceType.WEB: ExtractorKind.ARTICLE,
        SourceType.SOCIAL: ExtractorKind.SOCIAL,
        SourceType.YOUTUBE: ExtractorKind.YOUTUBE,
    }.get(envelope.source_type)
    if extractor is None:
        return None
    transcript = envelope.shared_text if envelope.source_type is SourceType.YOUTUBE else None
    return NormalizedExtraction.create(
        extractor=extractor,
        state=ExtractionState.COMPLETE,
        source_type=envelope.source_type,
        content_kind=envelope.content_kind,
        metadata=ExtractionMetadata.create(
            title=envelope.title,
            canonical_url=envelope.source_url,
        ),
        text="" if transcript is not None else envelope.shared_text,
        transcript=transcript,
        transcript_state=(
            TranscriptState.SUPPLIED
            if transcript is not None
            else TranscriptState.NOT_APPLICABLE
        ),
        assets=(),
        failure=None,
    )


def _write_personal_capture(
    *,
    root: Path,
    raw: RawCapture,
    extraction: NormalizedExtraction,
    distilled: DistilledCapture,
) -> None:
    envelope = raw.envelope
    if (
        distilled.capture_id != str(envelope.capture_id)
        or distilled.capture_why != envelope.capture_why
        or distilled.content_kind is not envelope.content_kind
        or envelope.privacy_decision.tier is not PrivacyTier.PERSONAL
    ):
        raise ValueError("invalid personal capture binding")
    body = extraction.transcript or extraction.text
    payload = (
        "<!-- open-brain-personal-capture-v1 "
        + str(envelope.capture_id)
        + " -->\n\n# "
        + _escape(distilled.title)
        + "\n\n"
        + _escape(distilled.summary)
        + "\n\n## Why this was captured\n\n"
        + _escape(envelope.capture_why)
        + "\n\n## Captured text\n\n"
        + _escape(body)
        + "\n"
    ).encode("utf-8")
    atomic_write_new(
        root=root,
        relative=PurePosixPath("captures") / (str(envelope.capture_id) + ".md"),
        data=payload,
    )


def _escape(value: str) -> str:
    return html.escape(value, quote=False).replace("\x00", "")


__all__ = ["PersonalCaptureStatus", "PersonalCaptureWorker"]
