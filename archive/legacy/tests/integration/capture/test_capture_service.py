from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.capture.models import (
    CaptureLease,
    CaptureRedactionResult,
    CaptureWorkItem,
    DistillationLease,
    DistillationWorkItem,
    ExtractionMetadata,
    ExtractionState,
    Extractor,
    ExtractorKind,
    NormalizedExtraction,
    QueueErrorCode,
    TranscriptState,
)
from open_brain_engine.capture.redaction import REDACTION_POLICY_VERSION, VersionedCaptureRedactor
from open_brain_engine.core.ids import CaptureId, ReviewId
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
    RawAssetRef,
    RawCapture,
    SourceType,
)
from open_brain_engine.core.ports import (
    CaptureQueue,
    EventRecord,
    EventStore,
    IdGenerator,
    PutDisposition,
    PutResult,
    RawStore,
    RedactionReceipt,
)
from open_brain_engine.engine import open_local_engine
from open_brain_engine.storage.filesystem import AtomicFilesystemRawStore

from open_brain_legacy._compat.open_brain.capture.http import HttpRequest, ShareHttpHandler
from open_brain_legacy._compat.open_brain.profile import compile_single_user_local
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeExtractionRequest,
)
from open_brain_legacy.capture.service import CaptureService, ProcessStatus

FIXED_TIME = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class Crash(BaseException):
    pass


class FixedClock:
    def now(self) -> datetime:
        return FIXED_TIME


class DeterministicIds:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.event_calls: list[tuple[str, str, str]] = []

    def event_id(self, stream_id: str, event_type: str, payload_digest: str) -> str:
        self.operations.append("event-id")
        self.event_calls.append((stream_id, event_type, payload_digest))
        return "event-" + payload_digest[:24]

    def capture_id(self, identity: Mapping[str, object]) -> CaptureId:
        del identity
        raise AssertionError("capture ID is not used by CaptureService")

    def review_id(self, capture_id: CaptureId, intent: object) -> ReviewId:
        del capture_id, intent
        raise AssertionError("review ID is not used by CaptureService")

    def decision_id(self) -> str:
        raise AssertionError("decision ID is not used by CaptureService")


class CaptureQueueFake:
    def __init__(
        self,
        operations: list[str],
        *,
        items: Sequence[CaptureWorkItem] = (),
        label: str,
    ) -> None:
        self.operations = operations
        self.label = label
        self.pending = list(items)
        self.processing: dict[str, CaptureWorkItem] = {}
        self.records = {
            str(item.envelope.capture_id): (item, item.payload_digest_sha256()) for item in items
        }
        self.quarantined: list[tuple[CaptureWorkItem, QueueErrorCode]] = []
        self._lease_count = 0

    def enqueue(self, item: CaptureWorkItem, *, item_id: str, payload_digest: str) -> PutResult:
        self.operations.append(self.label + ".enqueue")
        existing = self.records.get(item_id)
        if existing is not None:
            if existing[1] == payload_digest:
                return PutResult(PutDisposition.DUPLICATE, item_id, payload_digest)
            raise RuntimeError("synthetic immutable conflict")
        self.records[item_id] = (item, payload_digest)
        self.pending.append(item)
        return PutResult(PutDisposition.CREATED, item_id, payload_digest)

    def claim(self, *, worker_id: str, now: datetime) -> CaptureLease | None:
        self.operations.append(self.label + ".claim")
        if not self.pending:
            return None
        item = self.pending.pop(0)
        self._lease_count += 1
        token = "lease-" + str(self._lease_count)
        self.processing[token] = item
        return CaptureLease.create(
            item=item,
            item_id=str(item.envelope.capture_id),
            payload_digest_sha256=item.payload_digest_sha256(),
            worker_id=worker_id,
            lease_token=token,
            claimed_at=now,
        )

    def acknowledge(self, lease: CaptureLease, *, completed_at: datetime) -> None:
        self.operations.append(self.label + ".ack")
        token = lease.lease_token
        self.processing.pop(token)

    def retry(self, lease: CaptureLease, *, available_at: datetime, error_code: str) -> None:
        self.operations.append(self.label + ".retry")
        token = lease.lease_token
        item = self.processing.pop(token)
        retry = CaptureWorkItem.create(
            envelope=item.envelope,
            available_at=available_at,
            attempt_count=item.attempt_count + 1,
            last_error_code=error_code,
        )
        self.records[str(item.envelope.capture_id)] = (retry, retry.payload_digest_sha256())
        self.pending.append(retry)

    def quarantine(self, lease: CaptureLease, *, at: datetime, error_code: str) -> None:
        self.operations.append(self.label + ".quarantine")
        token = lease.lease_token
        item = self.processing.pop(token)
        self.quarantined.append((item, QueueErrorCode(error_code)))

    def rotate_processing(self) -> None:
        self.pending.extend(self.processing.values())
        self.processing.clear()


class DistillationQueueFake:
    def __init__(self, operations: list[str], *, crash_after_enqueue: bool = False) -> None:
        self.operations = operations
        self.crash_after_enqueue = crash_after_enqueue
        self.records: dict[str, tuple[DistillationWorkItem, str]] = {}

    def enqueue(
        self, item: DistillationWorkItem, *, item_id: str, payload_digest: str
    ) -> PutResult:
        self.operations.append("distillation.enqueue")
        existing = self.records.get(item_id)
        if existing is not None:
            if existing[1] != payload_digest:
                raise RuntimeError("synthetic immutable conflict")
            result = PutResult(PutDisposition.DUPLICATE, item_id, payload_digest)
        else:
            self.records[item_id] = (item, payload_digest)
            result = PutResult(PutDisposition.CREATED, item_id, payload_digest)
        if self.crash_after_enqueue:
            self.crash_after_enqueue = False
            raise Crash
        return result

    def claim(self, *, worker_id: str, now: datetime) -> DistillationLease | None:
        del worker_id, now
        return None

    def acknowledge(self, lease: DistillationLease, *, completed_at: datetime) -> None:
        del lease, completed_at

    def retry(self, lease: DistillationLease, *, available_at: datetime, error_code: str) -> None:
        del lease, available_at, error_code

    def quarantine(self, lease: DistillationLease, *, at: datetime, error_code: str) -> None:
        del lease, at, error_code


class RawStoreFake:
    def __init__(self, operations: list[str], *, crash_after_put: bool = False) -> None:
        self.operations = operations
        self.crash_after_put = crash_after_put
        self.captures: dict[str, RawCapture] = {}

    def put_if_absent(self, capture: RawCapture) -> PutResult:
        self.operations.append("raw.put")
        capture_id = str(capture.envelope.capture_id)
        existing = self.captures.get(capture_id)
        if existing is None:
            self.captures[capture_id] = capture
            disposition = PutDisposition.CREATED
        elif existing == capture:
            disposition = PutDisposition.DUPLICATE
        else:
            raise RuntimeError("synthetic immutable conflict")
        if self.crash_after_put:
            self.crash_after_put = False
            raise Crash
        return PutResult(
            disposition,
            capture_id,
            sha256(capture.envelope.canonical_bytes()).hexdigest(),
        )

    def get(self, capture_id: CaptureId) -> RawCapture | None:
        return self.captures.get(str(capture_id))


class EventStoreFake:
    def __init__(self, operations: list[str], *, crash_after_append: bool = False) -> None:
        self.operations = operations
        self.crash_after_append = crash_after_append
        self.records: dict[str, EventRecord] = {}

    def append(self, record: EventRecord) -> PutResult:
        self.operations.append("event.append")
        existing = self.records.get(record.event_id)
        if existing is None:
            self.records[record.event_id] = record
            disposition = PutDisposition.CREATED
        elif existing == record:
            disposition = PutDisposition.DUPLICATE
        else:
            raise RuntimeError("synthetic immutable conflict")
        if self.crash_after_append:
            self.crash_after_append = False
            raise Crash
        return PutResult(
            disposition,
            record.event_id,
            sha256(record.canonical_bytes()).hexdigest(),
        )

    def read(self, stream_id: CaptureId, *, after_sequence: int = 0) -> Sequence[EventRecord]:
        del stream_id, after_sequence
        return tuple(self.records.values())


class ReadableEventStoreFake(EventStoreFake):
    def read(self, stream_id: CaptureId, *, after_sequence: int = 0) -> Sequence[EventRecord]:
        del stream_id, after_sequence
        self.operations.append("event.read")
        return tuple(self.records.values())


class ExtractorFake:
    def __init__(
        self, operations: list[str], extraction: NormalizedExtraction | None = None
    ) -> None:
        self.operations = operations
        self.calls = 0
        self.extraction = extraction or _extraction()

    def extract(self, request: object, *, privacy: PrivacyDecision) -> NormalizedExtraction:
        del request, privacy
        self.operations.append("extract")
        self.calls += 1
        return self.extraction


class RedactorFake:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.calls = 0

    def redact(
        self, extraction: NormalizedExtraction, envelope: CaptureEnvelope
    ) -> CaptureRedactionResult:
        self.operations.append("redact")
        self.calls += 1
        payload = {"text": "[redacted]", "source_type": extraction.source_type.value}
        return CaptureRedactionResult.create(
            payload=payload,
            receipt=RedactionReceipt.create(
                source_digest_sha256=sha256(extraction.canonical_bytes()).hexdigest(),
                output_digest_sha256=EventRecord.output_digest_sha256(payload),
                policy_version="synthetic-v1",
            ),
        )


class RequestRecordingExtractor(ExtractorFake):
    def __init__(self, operations: list[str], extraction: NormalizedExtraction) -> None:
        super().__init__(operations, extraction=extraction)
        self.requests: list[object] = []

    def extract(self, request: object, *, privacy: PrivacyDecision) -> NormalizedExtraction:
        self.requests.append(request)
        return super().extract(request, privacy=privacy)


def _privacy(reason: PrivacyReason = PrivacyReason.POLICY_WORK) -> PrivacyDecision:
    tiers = {
        PrivacyReason.POLICY_WORK: PrivacyTier.WORK,
        PrivacyReason.PERSONAL_LOCAL_ONLY: PrivacyTier.PERSONAL,
        PrivacyReason.SECRET_DETECTED: PrivacyTier.SECRET,
        PrivacyReason.CLASSIFICATION_MISSING: PrivacyTier.UNKNOWN,
        PrivacyReason.CLASSIFICATION_INVALID: PrivacyTier.UNKNOWN,
        PrivacyReason.CLASSIFICATION_AMBIGUOUS: PrivacyTier.UNKNOWN,
    }
    return PrivacyDecision.create(
        tier=tiers[reason],
        reason=reason,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _envelope(
    *,
    shared_text: str = "Synthetic capture text",
    title: str | None = None,
    source_type: SourceType = SourceType.TEXT,
    capture_source: CaptureSource = CaptureSource.CLI,
    privacy: PrivacyDecision | None = None,
) -> CaptureEnvelope:
    source_url = None if source_type is SourceType.TEXT else "https://example.test/shared"
    source_ref = (
        "urn:open-brain:text:sha256:" + sha256(shared_text.encode()).hexdigest()
        if source_url is None
        else source_url
    )
    return CaptureEnvelope.create(
        source_type=source_type,
        content_kind=ContentKind.OTHER,
        source_url=source_url,
        title=title,
        shared_text=shared_text,
        captured_at=FIXED_TIME,
        capture_why="Keep this synthetic reference",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=capture_source,
        provenance=Provenance.create(
            source_ref=source_ref,
            content_origin=ContentOrigin.OWNER_AUTHORED,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=privacy or _privacy(),
    )


def _item(envelope: CaptureEnvelope) -> CaptureWorkItem:
    return CaptureWorkItem.create(envelope=envelope, available_at=FIXED_TIME)


def _asset() -> RawAssetRef:
    digest = "a" * 64
    return RawAssetRef.create(
        asset_id="asset_" + digest,
        sha256=digest,
        media_type="text/plain",
        byte_length=1,
    )


def _extraction(
    *,
    source_type: SourceType = SourceType.TEXT,
    assets: tuple[RawAssetRef, ...] = (),
    text: str = "Normalized synthetic text",
) -> NormalizedExtraction:
    return NormalizedExtraction.create(
        extractor=ExtractorKind.TEXT if source_type is SourceType.TEXT else ExtractorKind.ARTICLE,
        state=ExtractionState.COMPLETE,
        source_type=source_type,
        content_kind=ContentKind.OTHER,
        metadata=ExtractionMetadata.create(title="Synthetic title"),
        text=text,
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=assets,
        failure=None,
    )


def _event_record(
    envelope: CaptureEnvelope,
    *,
    event_id: str,
    occurred_at: datetime,
    policy_version: str = REDACTION_POLICY_VERSION,
) -> EventRecord:
    payload = {"text": "[redacted]"}
    return EventRecord.create(
        event_id=event_id,
        stream_id=envelope.capture_id,
        event_type="capture.extracted",
        occurred_at=occurred_at,
        privacy_decision=envelope.privacy_decision,
        payload=payload,
        redaction_receipt=RedactionReceipt.create(
            source_digest_sha256="0" * 64,
            output_digest_sha256=EventRecord.output_digest_sha256(payload),
            policy_version=policy_version,
        ),
    )


def _service(
    *,
    intake: CaptureQueue[CaptureWorkItem, CaptureLease],
    hold: CaptureQueue[CaptureWorkItem, CaptureLease],
    distillation: CaptureQueue[DistillationWorkItem, DistillationLease],
    raw_store: RawStore,
    event_store: EventStore,
    extractor: Extractor[object],
    ids: IdGenerator,
    redactor: VersionedCaptureRedactor | None = None,
) -> CaptureService:
    return CaptureService(
        intake_queue=intake,
        private_hold_queue=hold,
        distillation_queue=distillation,
        raw_store=raw_store,
        event_store=event_store,
        extractors={source_type: extractor for source_type in SourceType},
        redactor=redactor,
        clock=FixedClock(),
        ids=ids,
    )


def test_s01_authorized_capture_persists_in_exact_durable_order() -> None:
    operations: list[str] = []
    intake = CaptureQueueFake(operations, items=[_item(_envelope())], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    raw = RawStoreFake(operations)
    event = EventStoreFake(operations)
    extractor = ExtractorFake(operations)
    ids = DeterministicIds(operations)

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw,
            event_store=event,
            extractor=extractor,
            ids=ids,
        ).process_one(worker_id="worker-001")
        is ProcessStatus.ACKNOWLEDGED
    )

    assert operations == [
        "intake.claim",
        "raw.put",
        "extract",
        "event-id",
        "event.append",
        "distillation.enqueue",
        "intake.ack",
    ]
    assert len(raw.captures) == len(event.records) == len(distillation.records) == 1
    assert next(iter(event.records.values())).redaction_receipt.policy_version == (
        REDACTION_POLICY_VERSION
    )
    assert not intake.processing


def test_capture_service_rejects_arbitrary_redactor_before_processing() -> None:
    operations: list[str] = []

    with pytest.raises(ValueError, match="approved capture redactor required"):
        _service(
            intake=CaptureQueueFake(operations, items=[_item(_envelope())], label="intake"),
            hold=CaptureQueueFake(operations, label="hold"),
            distillation=DistillationQueueFake(operations),
            raw_store=RawStoreFake(operations),
            event_store=EventStoreFake(operations),
            extractor=ExtractorFake(operations),
            redactor=RedactorFake(operations),  # type: ignore[arg-type]
            ids=DeterministicIds(operations),
        )

    assert operations == []


def test_s03_private_capture_persists_raw_then_holds_without_work_tier() -> None:
    operations: list[str] = []
    envelope = _envelope(privacy=_privacy(PrivacyReason.SECRET_DETECTED))
    intake = CaptureQueueFake(operations, items=[_item(envelope)], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    raw = RawStoreFake(operations)
    event = EventStoreFake(operations)
    extractor = ExtractorFake(operations)
    ids = DeterministicIds(operations)

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw,
            event_store=event,
            extractor=extractor,
            ids=ids,
        ).process_one(worker_id="worker-001")
        is ProcessStatus.ACKNOWLEDGED
    )

    assert operations == ["intake.claim", "raw.put", "hold.enqueue", "intake.ack"]
    assert raw.captures[str(envelope.capture_id)] == RawCapture.create(envelope=envelope, assets=())
    assert extractor.calls == 0
    assert not event.records and not distillation.records


def test_s06_replay_after_raw_persistence_is_idempotent() -> None:
    _assert_replay_boundary("raw")


def test_s06_replay_after_event_persistence_is_idempotent() -> None:
    _assert_replay_boundary("event")


def test_s06_replay_after_distillation_enqueue_is_idempotent() -> None:
    _assert_replay_boundary("distillation")


def test_f01_event_replay_uses_durable_event_without_second_extraction() -> None:
    operations: list[str] = []
    intake = CaptureQueueFake(operations, items=[_item(_envelope())], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    raw = RawStoreFake(operations)
    event = ReadableEventStoreFake(operations, crash_after_append=True)
    extractor = ExtractorFake(operations, extraction=_extraction(text="first normalized result"))
    service = _service(
        intake=intake,
        hold=hold,
        distillation=distillation,
        raw_store=raw,
        event_store=event,
        extractor=extractor,
        ids=DeterministicIds(operations),
    )

    with pytest.raises(Crash):
        service.process_one(worker_id="worker-001")
    extractor.extraction = _extraction(text="changed normalized result")
    intake.rotate_processing()

    assert service.process_one(worker_id="worker-001") is ProcessStatus.ACKNOWLEDGED
    assert len(event.records) == len(distillation.records) == 1
    assert extractor.calls == 1


@pytest.mark.parametrize(
    "records",
    (
        lambda envelope: (
            _event_record(
                envelope,
                event_id="event-conflict",
                occurred_at=FIXED_TIME + timedelta(seconds=1),
            ),
        ),
        lambda envelope: (
            _event_record(envelope, event_id="event-first", occurred_at=FIXED_TIME),
            _event_record(envelope, event_id="event-second", occurred_at=FIXED_TIME),
        ),
        lambda envelope: (
            _event_record(
                envelope,
                event_id="event-unapproved",
                occurred_at=FIXED_TIME,
                policy_version="synthetic-unapproved",
            ),
        ),
    ),
)
def test_existing_conflicting_or_multiple_events_quarantine_before_extraction(
    records: Callable[[CaptureEnvelope], tuple[EventRecord, ...]],
) -> None:
    operations: list[str] = []
    envelope = _envelope()
    intake = CaptureQueueFake(operations, items=[_item(envelope)], label="intake")
    event = ReadableEventStoreFake(operations)
    for record in records(envelope):
        event.records[record.event_id] = record
    extractor = ExtractorFake(operations)

    assert (
        _service(
            intake=intake,
            hold=CaptureQueueFake(operations, label="hold"),
            distillation=DistillationQueueFake(operations),
            raw_store=RawStoreFake(operations),
            event_store=event,
            extractor=extractor,
            ids=DeterministicIds(operations),
        ).process_one(worker_id="worker-001")
        is ProcessStatus.QUARANTINED
    )

    assert operations == ["intake.claim", "raw.put", "event.read", "intake.quarantine"]
    assert extractor.calls == 0


def test_s02_changed_raw_conflict_preserves_original_and_quarantines(tmp_path: Path) -> None:
    operations: list[str] = []
    original = _envelope(title="Original synthetic title")
    changed = _envelope(title="Changed synthetic title")
    assert original.capture_id == changed.capture_id
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    raw_store = AtomicFilesystemRawStore(root=raw_root)
    raw_store.put_if_absent(RawCapture.create(envelope=original, assets=()))
    intake = CaptureQueueFake(operations, items=[_item(changed)], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    event = EventStoreFake(operations)
    extractor = ExtractorFake(operations)

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw_store,
            event_store=event,
            extractor=extractor,
            ids=DeterministicIds(operations),
        ).process_one(worker_id="worker-001")
        is ProcessStatus.QUARANTINED
    )

    assert raw_store.get(original.capture_id) == RawCapture.create(envelope=original, assets=())
    assert intake.quarantined == [(_item(changed), QueueErrorCode.IMMUTABLE_CONFLICT)]
    assert extractor.calls == 0
    assert not event.records and not distillation.records


def test_s03_raw_marker_stays_private_and_event_receipt_binds_extraction(tmp_path: Path) -> None:
    operations: list[str] = []
    marker = "synthetic-private-marker"
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    raw_store = AtomicFilesystemRawStore(root=raw_root)
    extraction = _extraction(text="normalized synthetic result")
    intake = CaptureQueueFake(
        operations,
        items=[_item(_envelope(shared_text=marker))],
        label="intake",
    )
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    event = EventStoreFake(operations)

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw_store,
            event_store=event,
            extractor=ExtractorFake(operations, extraction=extraction),
            ids=DeterministicIds(operations),
        ).process_one(worker_id="worker-001")
        is ProcessStatus.ACKNOWLEDGED
    )

    record = next(iter(event.records.values()))
    stored = raw_store.get(record.stream_id)
    assert stored is not None and stored.envelope.shared_text == marker
    assert marker not in record.payload.values()
    assert (
        record.redaction_receipt.source_digest_sha256
        == sha256(extraction.canonical_bytes()).hexdigest()
    )
    assert record.redaction_receipt.policy_version == REDACTION_POLICY_VERSION


def test_default_policy_removes_runtime_markers_from_event_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    operations: list[str] = []
    credential = "service" + "D4" * 18
    bearer = "header" + "E5" * 18
    email = "person" + "@" + "example.test"
    long_token = "opaque" + "F6" * 18
    extraction = NormalizedExtraction.create(
        extractor=ExtractorKind.TEXT,
        state=ExtractionState.COMPLETE,
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        metadata=ExtractionMetadata.create(
            title="password=" + credential,
            author=email,
            platform="Bearer " + bearer,
        ),
        text="api_key=" + credential + " " + long_token,
        transcript="Contact " + email,
        transcript_state=TranscriptState.SUPPLIED,
        assets=(),
        failure=None,
    )
    envelope = _envelope(shared_text="raw-" + credential)
    intake = CaptureQueueFake(operations, items=[_item(envelope)], label="intake")
    raw = RawStoreFake(operations)
    event = EventStoreFake(operations)

    status = _service(
        intake=intake,
        hold=CaptureQueueFake(operations, label="hold"),
        distillation=DistillationQueueFake(operations),
        raw_store=raw,
        event_store=event,
        extractor=ExtractorFake(operations, extraction=extraction),
        ids=DeterministicIds(operations),
    ).process_one(worker_id="worker-001")

    assert status is ProcessStatus.ACKNOWLEDGED
    record = next(iter(event.records.values()))
    event_bytes = record.canonical_bytes()
    for marker in (credential, bearer, email, long_token):
        assert marker.encode() not in event_bytes
        assert marker not in caplog.text
        assert marker not in str(status)
    assert raw.captures[str(envelope.capture_id)].envelope.shared_text == "raw-" + credential
    assert record.redaction_receipt.policy_version == REDACTION_POLICY_VERSION


@pytest.mark.parametrize(
    "reason",
    (
        PrivacyReason.CLASSIFICATION_MISSING,
        PrivacyReason.CLASSIFICATION_INVALID,
        PrivacyReason.CLASSIFICATION_AMBIGUOUS,
        PrivacyReason.PERSONAL_LOCAL_ONLY,
    ),
)
def test_s04_s05_local_only_decisions_hold_without_work_tier_calls(reason: PrivacyReason) -> None:
    operations: list[str] = []
    envelope = _envelope(privacy=_privacy(reason))
    intake = CaptureQueueFake(operations, items=[_item(envelope)], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    raw = RawStoreFake(operations)
    event = ReadableEventStoreFake(operations)
    extractor = ExtractorFake(operations)

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw,
            event_store=event,
            extractor=extractor,
            ids=DeterministicIds(operations),
        ).process_one(worker_id="worker-001")
        is ProcessStatus.ACKNOWLEDGED
    )

    assert operations == ["intake.claim", "raw.put", "hold.enqueue", "intake.ack"]
    assert extractor.calls == 0
    assert not event.records and not distillation.records


def test_youtube_request_forwards_supplied_transcript() -> None:
    operations: list[str] = []
    transcript = "Synthetic supplied transcript"
    intake = CaptureQueueFake(
        operations,
        items=[_item(_envelope(source_type=SourceType.YOUTUBE, shared_text=transcript))],
        label="intake",
    )
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    raw = RawStoreFake(operations)
    event = EventStoreFake(operations)
    extractor = RequestRecordingExtractor(
        operations, extraction=_extraction(source_type=SourceType.YOUTUBE)
    )

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw,
            event_store=event,
            extractor=extractor,
            ids=DeterministicIds(operations),
        ).process_one(worker_id="worker-001")
        is ProcessStatus.ACKNOWLEDGED
    )

    request = extractor.requests[0]
    assert isinstance(request, YouTubeExtractionRequest)
    assert request.supplied_transcript == transcript


def test_pending_youtube_transcript_retries_without_publishing_immutable_event() -> None:
    operations: list[str] = []
    envelope = _envelope(source_type=SourceType.YOUTUBE, shared_text="")
    intake = CaptureQueueFake(operations, items=[_item(envelope)], label="intake")
    event = EventStoreFake(operations)
    distillation = DistillationQueueFake(operations)
    pending = NormalizedExtraction.create(
        extractor=ExtractorKind.YOUTUBE,
        state=ExtractionState.PENDING_TRANSCRIPT,
        source_type=SourceType.YOUTUBE,
        content_kind=ContentKind.VIDEO,
        metadata=ExtractionMetadata.create(title="Synthetic pending video"),
        text="",
        transcript=None,
        transcript_state=TranscriptState.PENDING,
        assets=(),
        failure=None,
    )

    status = _service(
        intake=intake,
        hold=CaptureQueueFake(operations, label="hold"),
        distillation=distillation,
        raw_store=RawStoreFake(operations),
        event_store=event,
        extractor=ExtractorFake(operations, extraction=pending),
        ids=DeterministicIds(operations),
    ).process_one(worker_id="worker-001")

    assert status is ProcessStatus.RETRY_SCHEDULED
    assert operations == ["intake.claim", "raw.put", "extract", "intake.retry"]
    assert not event.records
    assert not distillation.records


def test_g03_ios_handler_to_durable_engine_capture_preserves_source_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    body = json.dumps(
        {
            "url": "https://example.test/shared",
            "why": "Synthetic iOS share reason",
            "text": "Synthetic iOS shared text",
        },
        separators=(",", ":"),
    ).encode()
    response = ShareHttpHandler(
        expected_bearer_token="synthetic-token",
        capture=tasks.capture,
        clock=lambda: FIXED_TIME,
        body_reader=lambda _maximum, _timeout: body,
    ).handle(
        HttpRequest(
            method="POST",
            path="/share",
            headers=(
                ("Authorization", "Bearer synthetic-token"),
                ("Content-Length", str(len(body))),
                ("Content-Type", "application/json"),
            ),
        )
    )
    value = json.loads(response.body)
    capture_id = value["capture_id"]
    assert response.status == 202
    assert isinstance(capture_id, str)
    assert [item.capture_id for item in tasks.inbox.list()] == [capture_id]

    space = tasks.inbox.create_space("iOS", delivery_id="ios.space")
    tasks.inbox.route(capture_id, space.space_id, delivery_id="ios.route")
    results = tasks.retrieval.search("Synthetic iOS shared text")

    assert len(results) == 1
    assert results[0].capture_id == capture_id
    assert results[0].provenance.source_origin == "third_party"
    rendered = json.dumps(results[0].provenance.as_dict())
    assert "https://example.test/shared" not in rendered
    assert "Synthetic iOS share reason" not in rendered


def test_g04_service_has_no_markdown_task_review_provider_or_network_surface() -> None:
    parameters = tuple(inspect.signature(CaptureService).parameters)
    assert parameters == (
        "intake_queue",
        "private_hold_queue",
        "distillation_queue",
        "raw_store",
        "event_store",
        "extractors",
        "redactor",
        "clock",
        "ids",
    )
    source = inspect.getsource(CaptureService)
    assert not any(
        term in source
        for term in ("Markdown", "Task", "Review", "Provider", "Outbound", "Staged", "network")
    )


def _assert_replay_boundary(boundary: str) -> None:
    operations: list[str] = []
    intake = CaptureQueueFake(operations, items=[_item(_envelope())], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations, crash_after_enqueue=boundary == "distillation")
    raw = RawStoreFake(operations, crash_after_put=boundary == "raw")
    event = EventStoreFake(operations, crash_after_append=boundary == "event")
    extractor = ExtractorFake(operations)
    ids = DeterministicIds(operations)
    service = _service(
        intake=intake,
        hold=hold,
        distillation=distillation,
        raw_store=raw,
        event_store=event,
        extractor=extractor,
        ids=ids,
    )

    with pytest.raises(Crash):
        service.process_one(worker_id="worker-001")
    assert intake.processing

    intake.rotate_processing()
    assert service.process_one(worker_id="worker-001") is ProcessStatus.ACKNOWLEDGED

    assert not intake.processing and not intake.pending
    assert len(raw.captures) == len(event.records) == len(distillation.records) == 1
    completed = [
        "intake.claim",
        "raw.put",
        "extract",
        "event-id",
        "event.append",
        "distillation.enqueue",
        "intake.ack",
    ]
    recovery = ["intake.claim", "raw.put", "distillation.enqueue", "intake.ack"]
    expected = {
        "raw": completed[:2] + completed,
        "event": completed[:5] + recovery,
        "distillation": completed[:6] + recovery,
    }[boundary]
    assert operations == expected


def test_extracted_assets_fail_closed_without_work_tier_persistence() -> None:
    operations: list[str] = []
    intake = CaptureQueueFake(operations, items=[_item(_envelope())], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    raw = RawStoreFake(operations)
    event = EventStoreFake(operations)
    extractor = ExtractorFake(operations, extraction=_extraction(assets=(_asset(),)))
    ids = DeterministicIds(operations)

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw,
            event_store=event,
            extractor=extractor,
            ids=ids,
        ).process_one(worker_id="worker-001")
        is ProcessStatus.RETRY_SCHEDULED
    )

    assert operations == ["intake.claim", "raw.put", "extract", "intake.retry"]
    assert intake.pending[0].last_error_code is QueueErrorCode.EXTRACTION_FAILED
    assert not event.records and not distillation.records


def test_g03_ios_provenance_survives_service_to_private_raw_storage(tmp_path: Path) -> None:
    operations: list[str] = []
    envelope = _envelope(source_type=SourceType.WEB, capture_source=CaptureSource.SHORTCUT)
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    raw_store = AtomicFilesystemRawStore(root=raw_root)
    intake = CaptureQueueFake(operations, items=[_item(envelope)], label="intake")
    hold = CaptureQueueFake(operations, label="hold")
    distillation = DistillationQueueFake(operations)
    event = EventStoreFake(operations)
    extractor = ExtractorFake(operations, extraction=_extraction(source_type=SourceType.WEB))
    ids = DeterministicIds(operations)

    assert (
        _service(
            intake=intake,
            hold=hold,
            distillation=distillation,
            raw_store=raw_store,
            event_store=event,
            extractor=extractor,
            ids=ids,
        ).process_one(worker_id="worker-001")
        is ProcessStatus.ACKNOWLEDGED
    )

    stored = raw_store.get(envelope.capture_id)
    assert stored is not None
    assert stored.envelope.capture_why == envelope.capture_why
    assert stored.envelope.capture_source is CaptureSource.SHORTCUT
    assert stored.envelope.captured_at == envelope.captured_at
    assert stored.envelope.privacy_decision == envelope.privacy_decision
