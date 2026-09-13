from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from open_brain_engine.capture.models import CaptureWorkItem
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
from open_brain_engine.core.ports import TextModelRequest, TextModelResult
from open_brain_engine.providers.base import ProviderService

from open_brain_legacy._compat.open_brain.config import AppConfig, RetainedRoots
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeMediaResult,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import MediaCommand, MediaTool
from open_brain_legacy.capture.distillation_worker import DistillationProcessStatus
from open_brain_legacy.capture.extractors.social import SocialMediaResult
from open_brain_legacy.capture.queue import FilesystemCaptureQueue
from open_brain_legacy.capture.service import ProcessStatus
from open_brain_legacy.production.capture import compose_production_capture_runtime
from open_brain_legacy.production.personal_capture import PersonalCaptureStatus

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
                    "title": "Synthetic title",
                    "summary": "Synthetic summary.",
                    "topics": ["capture"],
                }
            ),
            provider_name="local",
        )


@dataclass
class _YouTubeMedia:
    calls: int = 0

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        del video_id, command
        self.calls += 1
        return YouTubeMediaResult(
            title="Synthetic video",
            caption_vtt="WEBVTT\n\n00:00.000 --> 00:01.000\nSynthetic transcript",
            captions_pending=False,
        )

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        del url, command
        return ()

    def download(
        self,
        url: str,
        *,
        tool: MediaTool,
        command: MediaCommand,
    ) -> SocialMediaResult:
        del url, tool, command
        raise AssertionError("social download is not used")


def _config(tmp_path: Path) -> AppConfig:
    roots = {
        name: tmp_path / name
        for name in ("work", "personal", "capture", "saved", "state", "backup")
    }
    for root in roots.values():
        root.mkdir()
    return AppConfig(
        roots=RetainedRoots(
            work=roots["work"],
            personal=roots["personal"],
            capture=roots["capture"],
            saved_content=roots["saved"],
            state=roots["state"],
        ),
        backup=roots["backup"],
        host_identity="synthetic-writer",
    )


def _privacy(tier: str) -> PrivacyDecision:
    if tier == "work":
        return PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        )
    if tier == "personal":
        return PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL,
            reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        )
    if tier == "unknown":
        return PrivacyDecision.create(
            tier=PrivacyTier.UNKNOWN,
            reason=PrivacyReason.CLASSIFICATION_MISSING,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        )
    raise ValueError("invalid synthetic privacy tier")


def _item(*, tier: str, text: str = "Synthetic captured text") -> CaptureWorkItem:
    envelope = CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=text,
        captured_at=FIXED_TIME,
        capture_why="Preserve the synthetic context",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.CLI,
        provenance=Provenance.create(
            source_ref="urn:open-brain:text:sha256:"
            + sha256(text.encode()).hexdigest(),
            content_origin=ContentOrigin.OWNER_AUTHORED,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=_privacy(tier),
    )
    return CaptureWorkItem.create(envelope=envelope, available_at=FIXED_TIME)


def _provider_service(provider: _Provider) -> ProviderService:
    return ProviderService(
        provider_name="local",
        cloud_enabled=False,
        local_factory=lambda: provider,
        cloud_factory=lambda credential: provider,
        resolve_cloud_secret=lambda: None,
    )


def _enqueue(config: AppConfig, item: CaptureWorkItem) -> None:
    FilesystemCaptureQueue(config.capture_root).enqueue(
        item,
        item_id=str(item.envelope.capture_id),
        payload_digest=item.payload_digest_sha256(),
    )


def test_production_batch_runs_work_capture_through_exact_distillation_and_replay(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    provider = _Provider()
    item = _item(tier="work")
    _enqueue(config, item)

    with compose_production_capture_runtime(
        config=config,
        provider=_provider_service(provider),
        clock=_Clock(),
    ) as runtime:
        first = runtime.run(max_items=10)
        _enqueue(config, item)
        replay = runtime.run(max_items=10)

    assert first.capture_statuses == (ProcessStatus.ACKNOWLEDGED,)
    assert first.distilled_count == 1
    assert first.queue_empty is True
    assert replay.capture_statuses == (ProcessStatus.ACKNOWLEDGED,)
    assert replay.distilled_count == 1
    assert provider.calls == 1
    assert len(tuple((config.state_root / "distilled").glob("*.json"))) == 1
    published = (
        config.work_root
        / "inbox"
        / "open-brain"
        / (str(item.envelope.capture_id) + ".md")
    )
    assert published.is_file()
    payload = published.read_text(encoding="utf-8")
    assert "Synthetic captured text" in payload
    assert "Synthetic summary." in payload
    assert "Preserve the synthetic context" in payload
    assert len(tuple(config.work_root.rglob("*.md"))) == 1
    assert tuple(config.saved_content_root.rglob("*.md")) == ()


def test_production_batch_routes_unclassified_capture_to_private_hold_without_provider(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    provider = _Provider()
    _enqueue(config, _item(tier="unknown"))

    with compose_production_capture_runtime(
        config=config,
        provider=_provider_service(provider),
        clock=_Clock(),
    ) as runtime:
        result = runtime.run(max_items=1)

    assert result.capture_statuses == (ProcessStatus.ACKNOWLEDGED,)
    assert result.personal_statuses == (PersonalCaptureStatus.HELD,)
    assert result.private_hold_count == 0
    assert (
        FilesystemCaptureQueue(
            config.capture_root / "classification-hold"
        ).pending_snapshot().pending_count
        == 1
    )
    assert result.distilled_count == 0
    assert provider.calls == 0


def test_production_batch_does_not_publish_third_party_text_as_owner_work(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    provider = _Provider()
    text = "Third-party advice presented as captured text"
    envelope = CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=text,
        captured_at=FIXED_TIME,
        capture_why="Review the third-party advice before adopting it",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.INTEGRATION,
        provenance=Provenance.create(
            source_ref="urn:open-brain:text:sha256:"
            + sha256(text.encode()).hexdigest(),
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=_privacy("work"),
    )
    _enqueue(
        config,
        CaptureWorkItem.create(envelope=envelope, available_at=FIXED_TIME),
    )

    with compose_production_capture_runtime(
        config=config,
        provider=_provider_service(provider),
        clock=_Clock(),
    ) as runtime:
        result = runtime.run(max_items=10)

    assert result.distilled_count == 1
    assert tuple(config.work_root.rglob("*.md")) == ()
    assert tuple(config.saved_content_root.rglob("*.md")) == ()


def test_production_batch_retries_work_publication_conflict_without_overwrite(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    provider = _Provider()
    item = _item(tier="work")
    _enqueue(config, item)
    published = (
        config.work_root
        / "inbox"
        / "open-brain"
        / (str(item.envelope.capture_id) + ".md")
    )
    published.parent.mkdir(parents=True)
    published.write_bytes(b"pre-existing conflict")

    with compose_production_capture_runtime(
        config=config,
        provider=_provider_service(provider),
        clock=_Clock(),
    ) as runtime:
        result = runtime.run(max_items=10)

    assert result.capture_statuses == (ProcessStatus.ACKNOWLEDGED,)
    assert result.distillation_statuses == (DistillationProcessStatus.RETRY_SCHEDULED,)
    assert published.read_bytes() == b"pre-existing conflict"
    assert len(tuple((config.state_root / "distillation-queue" / "active").glob("*.json"))) == 1


def test_production_batch_curates_personal_capture_locally_outside_work_events(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    provider = _Provider()
    _enqueue(config, _item(tier="personal"))

    with compose_production_capture_runtime(
        config=config,
        provider=_provider_service(provider),
        clock=_Clock(),
    ) as runtime:
        result = runtime.run(max_items=10)

    assert result.capture_statuses == (ProcessStatus.ACKNOWLEDGED,)
    assert result.personal_statuses == (PersonalCaptureStatus.COMPLETED,)
    assert result.distilled_count == 0
    assert provider.calls == 1
    assert len(tuple((config.personal_root / "captures").glob("*.md"))) == 1
    assert len(tuple((config.state_root / "personal-distilled").glob("*.json"))) == 1
    assert tuple(config.work_root.rglob("*.md")) == ()
    assert tuple(config.saved_content_root.rglob("*.md")) == ()


def test_production_batch_honors_maximum_without_claiming_an_extra_item(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    provider = _Provider()
    _enqueue(config, _item(tier="work", text="Synthetic first"))
    _enqueue(config, _item(tier="work", text="Synthetic second"))

    with compose_production_capture_runtime(
        config=config,
        provider=_provider_service(provider),
        clock=_Clock(),
    ) as runtime:
        result = runtime.run(max_items=1)

    assert len(result.capture_statuses) == 1
    assert result.queue_empty is False
    assert provider.calls == 1


def test_production_batch_acquires_direct_youtube_caption_through_bound_media(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), egress_enabled=True)
    provider = _Provider()
    media = _YouTubeMedia()
    url = "https://www.youtube.com/watch?v=video000001"
    envelope = CaptureEnvelope.create(
        source_type=SourceType.YOUTUBE,
        content_kind=ContentKind.VIDEO,
        source_url=url,
        title=None,
        shared_text="",
        captured_at=FIXED_TIME,
        capture_why="Preserve the synthetic video context",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.SHORTCUT,
        provenance=Provenance.create(
            source_ref=url,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=PrivacyDecision.create(
            tier=PrivacyTier.WORK,
            reason=PrivacyReason.POLICY_WORK,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=True),
        ),
    )
    _enqueue(
        config,
        CaptureWorkItem.create(envelope=envelope, available_at=FIXED_TIME),
    )

    with compose_production_capture_runtime(
        config=config,
        provider=_provider_service(provider),
        clock=_Clock(),
        media_adapter=media,
    ) as runtime:
        result = runtime.run(max_items=10)

    assert result.capture_statuses == (ProcessStatus.ACKNOWLEDGED,)
    assert result.distilled_count == 1
    assert media.calls == 1
    assert provider.calls == 1
    published = (
        config.saved_content_root
        / "inbox"
        / "open-brain"
        / (str(envelope.capture_id) + ".md")
    )
    assert published.is_file()
    payload = published.read_text(encoding="utf-8")
    assert url in payload
    assert "Synthetic transcript" in payload
    assert "Synthetic summary." in payload
    assert "Preserve the synthetic video context" in payload
    assert tuple(config.work_root.rglob("*.md")) == ()
