from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.capture.models import (
    CaptureWorkItem,
    ExtractionFailure,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
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
    SourceType,
)

from open_brain_legacy.capture.drain import (
    CaptureDrain,
    DrainItemState,
    DrainProcessStatus,
    FilesystemDrainOutcomeStore,
)
from open_brain_legacy.capture.queue import FilesystemCaptureQueue

FIXED_TIME = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _privacy(*, egress: bool = True) -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=egress),
    )


def _envelope(source_type: SourceType, *, egress: bool = True) -> CaptureEnvelope:
    url = (
        "https://social.example.test/post/synthetic"
        if source_type is SourceType.SOCIAL
        else "https://example.test/article/synthetic"
    )
    return CaptureEnvelope.create(
        source_type=source_type,
        content_kind=ContentKind.POST
        if source_type is SourceType.SOCIAL
        else ContentKind.ARTICLE,
        source_url=url,
        title=None,
        shared_text="",
        captured_at=FIXED_TIME,
        capture_why="Keep the synthetic source context",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.SHORTCUT,
        provenance=Provenance.create(
            source_ref=url,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=_privacy(egress=egress),
    )


def _extraction(source_type: SourceType, *, failed: bool = False) -> NormalizedExtraction:
    return NormalizedExtraction.create(
        extractor=ExtractorKind.SOCIAL
        if source_type is SourceType.SOCIAL
        else ExtractorKind.ARTICLE,
        state=ExtractionState.FAILED if failed else ExtractionState.COMPLETE,
        source_type=source_type,
        content_kind=ContentKind.POST
        if source_type is SourceType.SOCIAL
        else ContentKind.ARTICLE,
        metadata=ExtractionMetadata.create(title="Synthetic capture"),
        text="" if failed else "Synthetic normalized content",
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=(),
        failure=ExtractionFailure.FETCH_FAILED if failed else None,
    )


class _Extractor:
    def __init__(self, result: NormalizedExtraction) -> None:
        self.result = result
        self.calls = 0

    def extract(self, request: object, *, privacy: PrivacyDecision) -> NormalizedExtraction:
        del request, privacy
        self.calls += 1
        return self.result


def _enqueue(queue: FilesystemCaptureQueue, envelope: CaptureEnvelope) -> CaptureWorkItem:
    item = CaptureWorkItem.create(envelope=envelope, available_at=FIXED_TIME)
    queue.enqueue(
        item,
        item_id=str(envelope.capture_id),
        payload_digest=item.payload_digest_sha256(),
    )
    return item


@pytest.mark.parametrize("source_type", [SourceType.SOCIAL, SourceType.WEB])
def test_social_and_article_success_is_terminal_reason_preserving_and_replay_safe(
    tmp_path: Path, source_type: SourceType
) -> None:
    queue = FilesystemCaptureQueue(tmp_path / "queue")
    envelope = _envelope(source_type)
    item = _enqueue(queue, envelope)
    extractor = _Extractor(_extraction(source_type))
    outcomes = FilesystemDrainOutcomeStore(tmp_path / "outcomes")
    drain = CaptureDrain(
        queue=queue,
        outcome_store=outcomes,
        extractors={source_type: extractor},
        clock=lambda: FIXED_TIME,
    )

    assert drain.process_one(worker_id="worker-001") is DrainProcessStatus.COMPLETED
    outcome = outcomes.get(str(envelope.capture_id))
    assert outcome is not None
    assert outcome.state is DrainItemState.COMPLETE
    assert outcome.capture_why == envelope.capture_why
    assert outcome.extraction == _extraction(source_type)

    queue.enqueue(
        item,
        item_id=str(envelope.capture_id),
        payload_digest=item.payload_digest_sha256(),
    )
    assert drain.process_one(worker_id="worker-001") is DrainProcessStatus.COMPLETED
    assert extractor.calls == 1


def test_retry_exhaustion_writes_one_failure_stub_and_replay_never_refetches(
    tmp_path: Path,
) -> None:
    queue = FilesystemCaptureQueue(tmp_path / "queue")
    envelope = _envelope(SourceType.SOCIAL)
    item = _enqueue(queue, envelope)
    extractor = _Extractor(_extraction(SourceType.SOCIAL, failed=True))
    outcomes = FilesystemDrainOutcomeStore(tmp_path / "outcomes")
    drain = CaptureDrain(
        queue=queue,
        outcome_store=outcomes,
        extractors={SourceType.SOCIAL: extractor},
        clock=lambda: FIXED_TIME,
        max_attempts=3,
    )

    assert drain.process_one(worker_id="worker-001") is DrainProcessStatus.RETRY_SCHEDULED
    assert drain.process_one(worker_id="worker-001") is DrainProcessStatus.RETRY_SCHEDULED
    assert drain.process_one(worker_id="worker-001") is DrainProcessStatus.STUBBED
    outcome = outcomes.get(str(envelope.capture_id))
    assert outcome is not None
    assert outcome.state is DrainItemState.STUBBED
    assert outcome.failure_code == ExtractionFailure.FETCH_FAILED.value
    assert outcome.attempt_count == 3
    assert outcome.capture_why == envelope.capture_why

    queue.enqueue(
        item,
        item_id=str(envelope.capture_id),
        payload_digest=item.payload_digest_sha256(),
    )
    assert drain.process_one(worker_id="worker-001") is DrainProcessStatus.STUBBED
    assert extractor.calls == 3


def test_private_capture_routes_to_terminal_hold_without_extractor_or_network(
    tmp_path: Path,
) -> None:
    queue = FilesystemCaptureQueue(tmp_path / "queue")
    envelope = _envelope(SourceType.WEB, egress=False)
    _enqueue(queue, envelope)
    extractor = _Extractor(_extraction(SourceType.WEB))
    outcomes = FilesystemDrainOutcomeStore(tmp_path / "outcomes")

    status = CaptureDrain(
        queue=queue,
        outcome_store=outcomes,
        extractors={SourceType.WEB: extractor},
        clock=lambda: FIXED_TIME,
    ).process_one(worker_id="worker-001")

    outcome = outcomes.get(str(envelope.capture_id))
    assert status is DrainProcessStatus.PRIVACY_HOLD
    assert outcome is not None and outcome.state is DrainItemState.PRIVACY_HOLD
    assert outcome.extraction is None
    assert extractor.calls == 0

