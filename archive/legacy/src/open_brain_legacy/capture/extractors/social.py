from __future__ import annotations

import html
import re
from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.parse import urlsplit

from open_brain_engine.capture.models import (
    ExtractionFailure,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    TranscriptState,
)
from open_brain_engine.core.models import ContentKind, PrivacyDecision, RawAssetRef, SourceType
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.core.ports import FetchRequest, OutboundFetcher

from open_brain_legacy._compat.open_brain_connectors.capture.media import (
    DEFAULT_MEDIA_LIMITS,
    MediaCommand,
    MediaTool,
)
from open_brain_legacy.providers.transcription import TranscriptionRequest, TranscriptionService

_PLATFORMS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "threads.net": "threads",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
}
_EVENT_HOSTS = {"eventbrite.com", "meetup.com"}


@dataclass(frozen=True, slots=True)
class SocialMediaResult:
    assets: tuple[object, ...] = ()
    used_tool: MediaTool | None = None
    failure: ExtractionFailure | None = None


@dataclass(frozen=True, slots=True)
class SocialExtractionRequest:
    url: str
    supplied_text: str = ""
    acquire_media: bool = False
    transcribe_audio: bool = False
    fetch_max_bytes: int = 262_144


class SocialMediaAdapter(Protocol):
    def download(
        self,
        url: str,
        *,
        tool: MediaTool,
        command: MediaCommand,
    ) -> SocialMediaResult: ...


class SocialExtractor:
    def __init__(
        self,
        *,
        fetcher: OutboundFetcher | None = None,
        media_adapter: SocialMediaAdapter | None = None,
        timeout_seconds: int = 30,
        transcription_service: TranscriptionService | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._media_adapter = media_adapter
        self._timeout_seconds = timeout_seconds
        self._transcription_service = transcription_service

    def extract(
        self, request: SocialExtractionRequest, *, privacy: PrivacyDecision
    ) -> NormalizedExtraction:
        classification = _classify(request.url)
        if classification is None:
            return _closed(ExtractionState.REJECTED, ExtractionFailure.INVALID_INPUT, request.url)
        if request.supplied_text.strip():
            if len(request.supplied_text.encode("utf-8")) > request.fetch_max_bytes:
                return _closed(
                    ExtractionState.FAILED,
                    ExtractionFailure.BODY_LIMIT,
                    request.url,
                    classification,
                )
            source_type, content_kind, platform = classification
            if content_kind is ContentKind.OTHER and platform == "generic":
                content_kind = _generic_content_kind(request.supplied_text)
            return NormalizedExtraction.create(
                extractor=ExtractorKind.SOCIAL,
                state=ExtractionState.COMPLETE,
                source_type=source_type,
                content_kind=content_kind,
                metadata=ExtractionMetadata.create(
                    title=_title(request.supplied_text),
                    canonical_url=request.url,
                    platform=platform,
                ),
                text=request.supplied_text,
                transcript=None,
                transcript_state=TranscriptState.NOT_APPLICABLE,
                assets=(),
                failure=None,
            )
        if not privacy.authority.external_egress:
            return _closed(ExtractionState.REJECTED, ExtractionFailure.PRIVACY_DENIED, request.url)
        if request.transcribe_audio and self._transcription_service is None:
            return _closed(
                ExtractionState.FAILED,
                ExtractionFailure.EXECUTOR_DENIED,
                request.url,
                classification,
            )
        source_type, content_kind, platform = classification
        metadata = ExtractionMetadata.create(canonical_url=request.url, platform=platform)
        text = ""
        if self._fetcher is None:
            return _closed(
                ExtractionState.FAILED,
                ExtractionFailure.TOOL_UNAVAILABLE,
                request.url,
                classification,
            )
        try:
            response = self._fetcher.fetch(
                FetchRequest(
                    request_id="social-extract",
                    url=request.url,
                    timeout_seconds=float(self._timeout_seconds),
                    max_bytes=request.fetch_max_bytes,
                    max_redirects=3,
                ),
                privacy=privacy,
            )
        except Exception:
            return _closed(
                ExtractionState.FAILED, ExtractionFailure.FETCH_FAILED, request.url, classification
            )
        if not isinstance(response.body, bytes) or len(response.body) > request.fetch_max_bytes:
            return _closed(
                ExtractionState.FAILED, ExtractionFailure.BODY_LIMIT, request.url, classification
            )
        text = response.body.decode("utf-8", errors="replace")
        if content_kind is ContentKind.OTHER and platform == "generic":
            content_kind = _generic_content_kind(text)
            classification = source_type, content_kind, platform
        metadata = ExtractionMetadata.create(
            title=_title(text), canonical_url=response.final_url, platform=platform
        )
        assets: tuple[RawAssetRef, ...] = ()
        if request.acquire_media:
            media = self._media(request, privacy, classification)
            if isinstance(media, NormalizedExtraction):
                return media
            assets = media
        transcript: str | None = None
        transcript_state = TranscriptState.NOT_APPLICABLE
        if request.transcribe_audio:
            transcribed = self._transcribe(request, privacy, classification, assets)
            if isinstance(transcribed, NormalizedExtraction):
                return transcribed
            transcript, transcript_state = transcribed
        return NormalizedExtraction.create(
            extractor=ExtractorKind.SOCIAL,
            state=ExtractionState.COMPLETE,
            source_type=source_type,
            content_kind=content_kind,
            metadata=metadata,
            text=text,
            transcript=transcript,
            transcript_state=transcript_state,
            assets=assets,
            failure=None,
        )

    def _media(
        self,
        request: SocialExtractionRequest,
        privacy: PrivacyDecision,
        classification: tuple[SourceType, ContentKind, str],
    ) -> tuple[RawAssetRef, ...] | NormalizedExtraction:
        if self._media_adapter is None:
            return _closed(
                ExtractionState.FAILED,
                ExtractionFailure.TOOL_UNAVAILABLE,
                request.url,
                classification,
            )
        for tool, argv in (
            (MediaTool.YT_DLP, ("yt-dlp", "--no-playlist")),
            (MediaTool.GALLERY_DL, ("gallery-dl",)),
        ):
            command = MediaCommand(argv=argv, limits=DEFAULT_MEDIA_LIMITS)
            try:
                result = self._media_adapter.download(
                    request.url,
                    tool=tool,
                    command=command,
                )
            except Exception:
                if tool is MediaTool.YT_DLP:
                    continue
                return _closed(
                    ExtractionState.FAILED,
                    ExtractionFailure.TOOL_UNAVAILABLE,
                    request.url,
                    classification,
                )
            if not isinstance(result, SocialMediaResult) or result.used_tool is not tool:
                return _closed(
                    ExtractionState.FAILED,
                    ExtractionFailure.MALFORMED_TOOL_OUTPUT,
                    request.url,
                    classification,
                )
            if result.failure is not None:
                if (
                    tool is MediaTool.YT_DLP
                    and result.failure is ExtractionFailure.TOOL_UNAVAILABLE
                ):
                    continue
                return _closed(
                    ExtractionState.FAILED,
                    result.failure,
                    request.url,
                    classification,
                )
            if not all(isinstance(asset, RawAssetRef) for asset in result.assets):
                return _closed(
                    ExtractionState.FAILED,
                    ExtractionFailure.MALFORMED_TOOL_OUTPUT,
                    request.url,
                    classification,
                )
            assets = tuple(cast(RawAssetRef, asset) for asset in result.assets)
            if not assets:
                continue
            limits = DEFAULT_MEDIA_LIMITS
            if (
                len(assets) > limits.max_files
                or any(asset.byte_length > limits.max_single_file_bytes for asset in assets)
                or sum(asset.byte_length for asset in assets) > limits.max_total_bytes
                or sum(asset.media_type.startswith("video/") for asset in assets)
                > limits.max_videos
            ):
                return _closed(
                    ExtractionState.FAILED,
                    ExtractionFailure.MEDIA_LIMIT,
                    request.url,
                    classification,
                )
            return tuple(sorted(assets, key=lambda asset: str(asset.asset_id)))
        return ()

    def _transcribe(
        self,
        request: SocialExtractionRequest,
        privacy: PrivacyDecision,
        classification: tuple[SourceType, ContentKind, str],
        assets: tuple[RawAssetRef, ...],
    ) -> tuple[str, TranscriptState] | NormalizedExtraction:
        service = self._transcription_service
        if service is None or not request.acquire_media:
            return _closed(
                ExtractionState.FAILED,
                ExtractionFailure.EXECUTOR_DENIED,
                request.url,
                classification,
            )
        asset = next(
            (
                candidate
                for candidate in assets
                if candidate.media_type.startswith(("audio/", "video/"))
            ),
            None,
        )
        if asset is None:
            return _closed(
                ExtractionState.FAILED,
                ExtractionFailure.TOOL_UNAVAILABLE,
                request.url,
                classification,
            )
        try:
            transcription_request = TranscriptionRequest.create(
                request_id="social-transcription."
                + sha256(str(asset.asset_id).encode("utf-8")).hexdigest(),
                asset=asset,
                timeout_seconds=float(self._timeout_seconds),
                max_output_bytes=2 * 1024 * 1024,
            )
        except ValueError:
            return _closed(
                ExtractionState.FAILED,
                ExtractionFailure.MEDIA_LIMIT,
                request.url,
                classification,
            )
        result = service.transcribe(transcription_request, privacy=privacy)
        if result.value is not None:
            return result.value.text, TranscriptState.ACQUIRED
        failure_by_code = {
            BoundaryErrorCode.CLOUD_AUTHORITY_REQUIRED: ExtractionFailure.PRIVACY_DENIED,
            BoundaryErrorCode.EGRESS_AUTHORITY_REQUIRED: ExtractionFailure.PRIVACY_DENIED,
            BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE: ExtractionFailure.TOOL_UNAVAILABLE,
            BoundaryErrorCode.CREDENTIAL_UNAVAILABLE: ExtractionFailure.TOOL_UNAVAILABLE,
            BoundaryErrorCode.LOCAL_UNAVAILABLE: ExtractionFailure.TOOL_UNAVAILABLE,
            BoundaryErrorCode.PROVIDER_TIMEOUT: ExtractionFailure.TOOL_TIMEOUT,
            BoundaryErrorCode.OUTPUT_LIMIT: ExtractionFailure.BODY_LIMIT,
            BoundaryErrorCode.MALFORMED_RESPONSE: ExtractionFailure.MALFORMED_TOOL_OUTPUT,
        }
        failure = (
            ExtractionFailure.EXECUTOR_FAILED
            if result.error_code is None
            else failure_by_code.get(result.error_code, ExtractionFailure.EXECUTOR_FAILED)
        )
        return _closed(ExtractionState.FAILED, failure, request.url, classification)


def _classify(url: str) -> tuple[SourceType, ContentKind, str] | None:
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().rstrip(".")
    for domain, platform in _PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            if platform == "youtube":
                return SourceType.YOUTUBE, ContentKind.VIDEO, platform
            return SourceType.SOCIAL, ContentKind.POST, platform
    if any(host == domain or host.endswith("." + domain) for domain in _EVENT_HOSTS):
        return SourceType.WEB, ContentKind.EVENT, "generic"
    return SourceType.WEB, ContentKind.OTHER, "generic"


def _title(value: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", value, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    return title or None


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.content_type: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "meta" or self.content_type is not None:
            return
        values = {name.casefold(): value for name, value in attrs if value is not None}
        name = values.get("property", values.get("name", "")).casefold()
        if name in {"og:type", "open-brain:content-kind"}:
            self.content_type = values.get("content", "").strip().casefold()[:64]


def _generic_content_kind(value: str) -> ContentKind:
    parser = _MetadataParser()
    try:
        parser.feed(value)
        parser.close()
    except (ValueError, AssertionError):
        return ContentKind.OTHER
    content_type = parser.content_type or ""
    if content_type == "event" or content_type.startswith("event."):
        return ContentKind.EVENT
    if content_type == "article" or content_type.startswith("article."):
        return ContentKind.ARTICLE
    if content_type == "product" or content_type.startswith("product."):
        return ContentKind.PRODUCT
    if content_type == "place" or content_type.startswith("place."):
        return ContentKind.PLACE
    if content_type == "post" or content_type.startswith("post."):
        return ContentKind.POST
    if content_type == "video" or content_type.startswith("video."):
        return ContentKind.VIDEO
    return ContentKind.OTHER


def _closed(
    state: ExtractionState,
    failure: ExtractionFailure,
    url: str,
    classification: tuple[SourceType, ContentKind, str] | None = None,
) -> NormalizedExtraction:
    source_type, content_kind, platform = classification or (
        SourceType.WEB,
        ContentKind.OTHER,
        "generic",
    )
    metadata = (
        ExtractionMetadata.create(canonical_url=url, platform=platform)
        if classification
        else ExtractionMetadata.create(platform=platform)
    )
    return NormalizedExtraction.create(
        extractor=ExtractorKind.SOCIAL,
        state=state,
        source_type=source_type,
        content_kind=content_kind,
        metadata=metadata,
        text="",
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=(),
        failure=failure,
    )
