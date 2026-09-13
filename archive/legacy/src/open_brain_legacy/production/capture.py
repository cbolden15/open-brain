"""Explicit production composition for bounded capture and distillation batches."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, cast

from open_brain_engine.capture.models import Extractor
from open_brain_engine.core.ids import (
    CaptureId,
    ReviewId,
    canonical_json_bytes,
    capture_id_for,
    review_id_for,
)
from open_brain_engine.core.models import Intent, PrivacyDecision, SourceType
from open_brain_engine.core.ports import (
    Clock,
    FetchRequest,
    FetchResponse,
    IdGenerator,
    OutboundFetcher,
)
from open_brain_engine.events.store import SqliteEventStore
from open_brain_engine.providers.base import ProviderService
from open_brain_engine.storage.filesystem import AtomicFilesystemRawStore

from open_brain_legacy._compat.open_brain.config import AppConfig
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeExtractor,
    YouTubeMediaAdapter,
)
from open_brain_legacy.capture.distillation import DistillationService, FilesystemDistillationStore
from open_brain_legacy.capture.distillation_worker import (
    DistillationProcessStatus,
    DistillationWorker,
)
from open_brain_legacy.capture.extractors.article import ArticleExtractor
from open_brain_legacy.capture.extractors.social import SocialExtractor, SocialMediaAdapter
from open_brain_legacy.capture.extractors.text import TextExtractor
from open_brain_legacy.capture.queue import (
    FilesystemCaptureQueue,
    FilesystemDistillationQueue,
)
from open_brain_legacy.capture.service import CaptureService, ProcessStatus
from open_brain_legacy.production.capture_publication import CaptureDestinationPublisher
from open_brain_legacy.production.personal_capture import (
    PersonalCaptureStatus,
    PersonalCaptureWorker,
)


class CaptureMediaAdapter(SocialMediaAdapter, YouTubeMediaAdapter, Protocol):
    """The exact combined media capability accepted by production capture."""


@dataclass(frozen=True, slots=True)
class ProductionCaptureRunResult:
    capture_statuses: tuple[ProcessStatus, ...]
    personal_statuses: tuple[PersonalCaptureStatus, ...]
    distillation_statuses: tuple[DistillationProcessStatus, ...]
    private_hold_count: int
    queue_empty: bool

    @property
    def distilled_count(self) -> int:
        return sum(
            status is DistillationProcessStatus.COMPLETED
            for status in self.distillation_statuses
        )


class ProductionCaptureRuntime:
    """An explicit, closable batch runtime with no ambient provider or network lookup."""

    def __init__(
        self,
        *,
        intake: FilesystemCaptureQueue,
        private_hold: FilesystemCaptureQueue,
        distillation: FilesystemDistillationQueue,
        capture_service: CaptureService,
        personal_worker: PersonalCaptureWorker,
        distillation_worker: DistillationWorker,
        event_store: SqliteEventStore,
    ) -> None:
        self._intake = intake
        self._private_hold = private_hold
        self._distillation = distillation
        self._capture_service = capture_service
        self._personal_worker = personal_worker
        self._distillation_worker = distillation_worker
        self._event_store = event_store
        self._closed = False

    def __enter__(self) -> ProductionCaptureRuntime:
        if self._closed:
            raise RuntimeError("capture runtime is closed")
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._event_store.close()
            self._closed = True

    def run(self, *, max_items: int = 100) -> ProductionCaptureRunResult:
        if (
            self._closed
            or not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or not 1 <= max_items <= 1_000
        ):
            raise ValueError("invalid capture batch")
        capture_statuses: list[ProcessStatus] = []
        for _ in range(max_items):
            capture_status = self._capture_service.process_one(worker_id="capture-worker")
            if capture_status is None:
                break
            capture_statuses.append(capture_status)
            if capture_status is ProcessStatus.RECOVERY_PENDING:
                break

        distillation_statuses: list[DistillationProcessStatus] = []
        personal_statuses: list[PersonalCaptureStatus] = []
        for _ in range(max_items):
            personal_status = self._personal_worker.process_one(
                worker_id="personal-capture-worker"
            )
            if personal_status is None:
                break
            personal_statuses.append(personal_status)
            if personal_status is PersonalCaptureStatus.RECOVERY_PENDING:
                break

        for _ in range(max_items):
            distillation_status = self._distillation_worker.process_one(
                worker_id="distillation-worker"
            )
            if distillation_status is None:
                break
            distillation_statuses.append(distillation_status)
            if distillation_status is DistillationProcessStatus.RECOVERY_PENDING:
                break

        intake_snapshot = self._intake.pending_snapshot()
        hold_snapshot = self._private_hold.pending_snapshot()
        return ProductionCaptureRunResult(
            capture_statuses=tuple(capture_statuses),
            personal_statuses=tuple(personal_statuses),
            distillation_statuses=tuple(distillation_statuses),
            private_hold_count=hold_snapshot.pending_count,
            queue_empty=(
                intake_snapshot.pending_count == 0
                and intake_snapshot.malformed_count == 0
            ),
        )


def compose_production_capture_runtime(
    *,
    config: AppConfig,
    provider: ProviderService,
    clock: Clock,
    fetcher: OutboundFetcher | None = None,
    media_adapter: CaptureMediaAdapter | None = None,
) -> ProductionCaptureRuntime:
    """Open durable stores and compose a runtime without processing queued content."""
    if (
        not isinstance(config, AppConfig)
        or not isinstance(provider, ProviderService)
        or (media_adapter is not None and not config.egress_enabled)
    ):
        raise ValueError("invalid production capture composition")
    raw_root = config.capture_root / "raw"
    raw_root.mkdir(mode=0o700, exist_ok=True)
    intake = FilesystemCaptureQueue(config.capture_root)
    private_hold = FilesystemCaptureQueue(config.capture_root / "private-hold")
    classification_hold = FilesystemCaptureQueue(
        config.capture_root / "classification-hold"
    )
    distillation = FilesystemDistillationQueue(config.state_root / "distillation-queue")
    raw_store = AtomicFilesystemRawStore(root=raw_root)
    events = SqliteEventStore(
        root=config.state_root,
        database_name="events.sqlite3",
        clock=clock,
    )
    selected_fetcher = _UnavailableFetcher() if fetcher is None else fetcher
    extractors = cast(
        Mapping[SourceType, Extractor[object]],
        {
            SourceType.TEXT: TextExtractor(),
            SourceType.WEB: ArticleExtractor(selected_fetcher),
            SourceType.SOCIAL: SocialExtractor(
                fetcher=selected_fetcher,
                media_adapter=media_adapter,
            ),
            SourceType.YOUTUBE: YouTubeExtractor(media_adapter),
        },
    )
    capture_service = CaptureService(
        intake_queue=intake,
        private_hold_queue=private_hold,
        distillation_queue=distillation,
        raw_store=raw_store,
        event_store=events,
        extractors=extractors,
        clock=clock,
        ids=ContentAddressedIds(),
    )
    personal_worker = PersonalCaptureWorker(
        queue=private_hold,
        classification_hold=classification_hold,
        raw_store=raw_store,
        distillation=DistillationService(
            store=FilesystemDistillationStore(
                config.state_root / "personal-distilled"
            ),
            provider=provider,
        ),
        personal_root=config.personal_root,
        clock=clock,
    )
    distillation_worker = DistillationWorker(
        queue=distillation,
        raw_store=raw_store,
        event_store=events,
        service=DistillationService(
            store=FilesystemDistillationStore(config.state_root / "distilled"),
            provider=provider,
        ),
        clock=clock,
        publisher=CaptureDestinationPublisher(
            work_root=config.work_root,
            saved_content_root=config.saved_content_root,
        ),
    )
    return ProductionCaptureRuntime(
        intake=intake,
        private_hold=private_hold,
        distillation=distillation,
        capture_service=capture_service,
        personal_worker=personal_worker,
        distillation_worker=distillation_worker,
        event_store=events,
    )


class ContentAddressedIds(IdGenerator):
    def capture_id(self, identity: Mapping[str, object]) -> CaptureId:
        return capture_id_for(dict(identity))

    def review_id(self, capture_id: CaptureId, intent: Intent) -> ReviewId:
        return review_id_for(capture_id, intent.value)

    def event_id(self, stream_id: str, event_type: str, payload_digest: str) -> str:
        return "event-" + sha256(
            canonical_json_bytes(
                {
                    "stream_id": stream_id,
                    "event_type": event_type,
                    "payload_digest": payload_digest,
                }
            )
        ).hexdigest()

    def decision_id(self) -> str:
        return "decision-" + secrets.token_hex(32)


class _UnavailableFetcher:
    def fetch(self, request: FetchRequest, *, privacy: PrivacyDecision) -> FetchResponse:
        del request, privacy
        raise RuntimeError("outbound fetch capability unavailable")


__all__ = [
    "CaptureMediaAdapter",
    "ContentAddressedIds",
    "ProductionCaptureRunResult",
    "ProductionCaptureRuntime",
    "compose_production_capture_runtime",
]
