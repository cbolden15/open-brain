from __future__ import annotations

from dataclasses import dataclass

from open_brain_engine.capture.models import ExtractionFailure, ExtractionState, TranscriptState
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    RawAssetRef,
)
from open_brain_engine.core.ports import FetchRequest, FetchResponse

from open_brain_legacy._compat.open_brain_connectors.capture.media import MediaCommand, MediaTool
from open_brain_legacy.capture.extractors.social import (
    SocialExtractionRequest,
    SocialExtractor,
    SocialMediaResult,
)
from open_brain_legacy.providers.transcription import (
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionService,
)


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=True),
    )


def _asset() -> RawAssetRef:
    digest = "b" * 64
    return RawAssetRef.create(
        asset_id="asset_" + digest,
        sha256=digest,
        media_type="audio/mpeg",
        byte_length=1024,
    )


@dataclass
class _Fetcher:
    calls: int = 0

    def fetch(self, request: FetchRequest, *, privacy: PrivacyDecision) -> FetchResponse:
        del privacy
        self.calls += 1
        return FetchResponse(request.url, 200, "text/html", b"<title>Synthetic</title>")


@dataclass
class _Media:
    calls: int = 0

    def download(
        self,
        url: str,
        *,
        tool: MediaTool,
        command: MediaCommand,
    ) -> SocialMediaResult:
        del url, command
        self.calls += 1
        return SocialMediaResult(assets=(_asset(),), used_tool=tool)


@dataclass
class _Provider:
    calls: int = 0

    def transcribe(
        self,
        request: TranscriptionRequest,
        *,
        privacy: PrivacyDecision,
    ) -> TranscriptionResult:
        del request, privacy
        self.calls += 1
        return TranscriptionResult(text="Synthetic transcript", provider_name="local")


def test_social_transcription_runs_only_after_media_acquisition() -> None:
    fetcher = _Fetcher()
    media = _Media()
    provider = _Provider()
    service = TranscriptionService(
        enabled=True,
        provider_factory=lambda: provider,
        cloud_provider=False,
    )

    result = SocialExtractor(
        fetcher=fetcher,
        media_adapter=media,
        transcription_service=service,
    ).extract(
        SocialExtractionRequest(
            url="https://x.com/synthetic/status/1",
            acquire_media=True,
            transcribe_audio=True,
        ),
        privacy=_privacy(),
    )

    assert result.state is ExtractionState.COMPLETE
    assert result.transcript == "Synthetic transcript"
    assert result.transcript_state is TranscriptState.ACQUIRED
    assert result.assets == (_asset(),)
    assert (fetcher.calls, media.calls, provider.calls) == (1, 1, 1)


def test_disabled_transcription_never_constructs_provider() -> None:
    factory_calls = 0

    def factory() -> _Provider:
        nonlocal factory_calls
        factory_calls += 1
        return _Provider()

    result = SocialExtractor(
        fetcher=_Fetcher(),
        media_adapter=_Media(),
        transcription_service=TranscriptionService(provider_factory=factory),
    ).extract(
        SocialExtractionRequest(
            url="https://x.com/synthetic/status/1",
            acquire_media=True,
            transcribe_audio=True,
        ),
        privacy=_privacy(),
    )

    assert result.state is ExtractionState.FAILED
    assert result.failure is ExtractionFailure.TOOL_UNAVAILABLE
    assert result.transcript is None
    assert factory_calls == 0
