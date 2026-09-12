from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from open_brain_engine.capture.models import (
    DistillationWorkItem,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.capture.redaction import VersionedCaptureRedactor
from open_brain_engine.core.models import (
    Authority,
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
    RawCapture,
    SourceType,
)
from open_brain_engine.core.ports import EventRecord, TextModelRequest, TextModelResult
from open_brain_engine.events.store import SqliteEventStore
from open_brain_engine.providers.base import ProviderService
from open_brain_engine.storage.filesystem import AtomicFilesystemRawStore

from open_brain_legacy.capture.distillation import (
    DistillationService,
    FilesystemDistillationStore,
)
from open_brain_legacy.capture.distillation_worker import (
    DistillationProcessStatus,
    DistillationWorker,
)
from open_brain_legacy.capture.queue import FilesystemDistillationQueue

FIXED_TIME = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return FIXED_TIME


@dataclass
class _Provider:
    calls: int = 0

    def complete(
        self, request: TextModelRequest, *, privacy: PrivacyDecision
    ) -> TextModelResult:
        del request, privacy
        self.calls += 1
        return TextModelResult(
            text=json.dumps(
                {
                    "title": "Synthetic distilled title",
                    "summary": "Synthetic bounded summary.",
                    "topics": ["capture", "testing"],
                }
            ),
            provider_name="local",
        )


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.WORK,
        reason=PrivacyReason.POLICY_WORK,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _envelope() -> CaptureEnvelope:
    text = "Synthetic captured text"
    return CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=text,
        captured_at=FIXED_TIME,
        capture_why="Preserve the synthetic test context",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.CLI,
        provenance=Provenance.create(
            source_ref="urn:open-brain:text:sha256:"
            + sha256(text.encode("utf-8")).hexdigest(),
            content_origin=ContentOrigin.OWNER_AUTHORED,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=_privacy(),
    )


def _extraction() -> NormalizedExtraction:
    return NormalizedExtraction.create(
        extractor=ExtractorKind.TEXT,
        state=ExtractionState.COMPLETE,
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        metadata=ExtractionMetadata.create(title="Synthetic capture"),
        text="Synthetic captured text",
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=(),
        failure=None,
    )


def _fixture(
    tmp_path: Path,
    *,
    provider: _Provider | None,
    bound_digest: str | None = None,
) -> tuple[
    DistillationWorker,
    FilesystemDistillationQueue,
    FilesystemDistillationStore,
    DistillationWorkItem,
]:
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    raw = AtomicFilesystemRawStore(root=raw_root)
    envelope = _envelope()
    raw.put_if_absent(RawCapture.create(envelope=envelope, assets=()))

    redaction = VersionedCaptureRedactor().redact(_extraction(), envelope)
    event = EventRecord.create(
        event_id="event-synthetic-001",
        stream_id=envelope.capture_id,
        event_type="capture.extracted",
        occurred_at=FIXED_TIME,
        privacy_decision=envelope.privacy_decision,
        payload=redaction.payload,
        redaction_receipt=redaction.receipt,
    )
    state_root = tmp_path / "state"
    state_root.mkdir()
    events = SqliteEventStore(
        root=state_root,
        database_name="events.sqlite3",
        clock=_Clock(),
    )
    events.append(event)

    queue = FilesystemDistillationQueue(tmp_path / "queue")
    item = DistillationWorkItem.create(
        capture_id=envelope.capture_id,
        event_id=event.event_id,
        redacted_event_digest_sha256=(
            bound_digest
            if bound_digest is not None
            else sha256(event.canonical_bytes()).hexdigest()
        ),
    )
    queue.enqueue(item, item_id=item.event_id, payload_digest=item.payload_digest_sha256())

    store = FilesystemDistillationStore(tmp_path / "distilled")
    provider_service = None
    if provider is not None:
        provider_service = ProviderService(
            provider_name="local",
            cloud_enabled=False,
            local_factory=lambda: provider,
            cloud_factory=lambda credential: provider,
            resolve_cloud_secret=lambda: None,
        )
    return (
        DistillationWorker(
            queue=queue,
            raw_store=raw,
            event_store=events,
            service=DistillationService(store=store, provider=provider_service),
            clock=_Clock(),
        ),
        queue,
        store,
        item,
    )


def test_worker_binds_raw_event_and_distillation_then_replays_without_provider(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    worker, queue, store, item = _fixture(tmp_path, provider=provider)

    assert worker.process_one(worker_id="worker-001") is DistillationProcessStatus.COMPLETED
    assert store.get(str(_envelope().capture_id)) is not None

    queue.enqueue(item, item_id=item.event_id, payload_digest=item.payload_digest_sha256())
    assert worker.process_one(worker_id="worker-001") is DistillationProcessStatus.COMPLETED
    assert provider.calls == 1


def test_worker_quarantines_digest_mismatch_before_provider_call(tmp_path: Path) -> None:
    provider = _Provider()
    worker, _, _, _ = _fixture(tmp_path, provider=provider, bound_digest="f" * 64)

    assert worker.process_one(worker_id="worker-001") is DistillationProcessStatus.QUARANTINED
    assert provider.calls == 0


def test_worker_retries_when_local_provider_is_unavailable(tmp_path: Path) -> None:
    worker, _, store, _ = _fixture(tmp_path, provider=None)

    assert worker.process_one(worker_id="worker-001") is DistillationProcessStatus.RETRY_SCHEDULED
    assert store.get(str(_envelope().capture_id)) is None
