# Private legacy compatibility snapshot; excluded from every shipping artifact.
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from open_brain_engine.engine import (
    ContentKind,
    ExtractionFailure,
    ExtractionMetadata,
    ExtractionState,
    ExtractorKind,
    NormalizedExtraction,
    PrivacyDecision,
    SourceType,
    TranscriptState,
)

from open_brain_legacy._compat.open_brain_connectors.capture.media import (
    DEFAULT_MEDIA_LIMITS,
    MediaCommand,
)

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}


@dataclass(frozen=True, slots=True)
class YouTubeMediaResult:
    title: str | None = None
    author: str | None = None
    caption_vtt: str | None = None
    captions_pending: bool = True
    failure: ExtractionFailure | None = None


@dataclass(frozen=True, slots=True)
class YouTubeExtractionRequest:
    url: str
    supplied_transcript: str = ""


class YouTubeMediaAdapter(Protocol):
    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult: ...

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]: ...


class YouTubeExtractor:
    def __init__(
        self,
        media_adapter: YouTubeMediaAdapter | None = None,
    ) -> None:
        self._media_adapter = media_adapter

    def extract(
        self, request: YouTubeExtractionRequest, *, privacy: PrivacyDecision
    ) -> NormalizedExtraction:
        video_id = video_id_from_url(request.url)
        if video_id is None:
            return _closed(ExtractionState.REJECTED, ExtractionFailure.UNSUPPORTED_URL)
        supplied = request.supplied_transcript.strip()
        if supplied:
            return _complete(
                video_id, transcript=supplied, transcript_state=TranscriptState.SUPPLIED
            )
        if not privacy.authority.external_egress:
            return _closed(
                ExtractionState.REJECTED, ExtractionFailure.PRIVACY_DENIED, video_id=video_id
            )
        if self._media_adapter is None:
            return _closed(
                ExtractionState.FAILED, ExtractionFailure.TOOL_UNAVAILABLE, video_id=video_id
            )
        try:
            result = self._media_adapter.media(video_id, command=self._command())
        except Exception:
            return _closed(
                ExtractionState.FAILED, ExtractionFailure.TOOL_UNAVAILABLE, video_id=video_id
            )
        if not isinstance(result, YouTubeMediaResult):
            return _closed(
                ExtractionState.FAILED, ExtractionFailure.MALFORMED_TOOL_OUTPUT, video_id=video_id
            )
        if result.failure is not None:
            return _closed(ExtractionState.FAILED, result.failure, video_id=video_id)
        metadata = _metadata(video_id, result)
        if result.caption_vtt:
            transcript = clean_vtt(result.caption_vtt)
            if transcript:
                return _complete(
                    video_id,
                    metadata=metadata,
                    transcript=transcript,
                    transcript_state=TranscriptState.ACQUIRED,
                )
        if result.captions_pending or not result.caption_vtt:
            return NormalizedExtraction.create(
                extractor=ExtractorKind.YOUTUBE,
                state=ExtractionState.PENDING_TRANSCRIPT,
                source_type=SourceType.YOUTUBE,
                content_kind=ContentKind.VIDEO,
                metadata=metadata,
                text="",
                transcript=None,
                transcript_state=TranscriptState.PENDING,
                assets=(),
                failure=None,
            )
        return _closed(
            ExtractionState.FAILED, ExtractionFailure.MALFORMED_TOOL_OUTPUT, video_id=video_id
        )

    def playlist_items(
        self, url: str, *, max_items: int, privacy: PrivacyDecision
    ) -> tuple[str, ...]:
        if not privacy.authority.external_egress or self._media_adapter is None or max_items < 1:
            return ()
        parsed = urlsplit(url)
        if parsed.hostname not in _YOUTUBE_HOSTS or not parse_qs(parsed.query).get("list"):
            return ()
        try:
            items = self._media_adapter.playlist_items(url, command=self._command())
        except Exception:
            return ()
        if (
            not isinstance(items, tuple)
            or len(items) > max_items
            or any(not isinstance(item, str) or not item for item in items)
        ):
            return ()
        return tuple(reversed(items))

    def _command(self) -> MediaCommand:
        return MediaCommand(
            argv=("yt-dlp", "--skip-download", "--no-playlist"),
            limits=DEFAULT_MEDIA_LIMITS,
        )


def clean_vtt(value: str) -> str:
    lines: list[str] = []
    previous: str | None = None
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if (
            not line
            or line == "WEBVTT"
            or "-->" in line
            or line.startswith(("NOTE", "STYLE", "REGION"))
        ):
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text and text != previous:
            lines.append(text)
            previous = text
    return "\n".join(lines)


def video_id_from_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    candidate: str | None = None
    if host == "youtu.be":
        candidate = parsed.path.strip("/")
    elif host in _YOUTUBE_HOSTS:
        path = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            candidate = values[0] if len(values) == 1 else None
        elif len(path) == 2 and path[0] in {"shorts", "embed", "live"}:
            candidate = path[1]
    return candidate if candidate and _VIDEO_ID.fullmatch(candidate) else None


def _metadata(video_id: str, result: YouTubeMediaResult | None = None) -> ExtractionMetadata:
    return ExtractionMetadata.create(
        title=None if result is None else result.title,
        author=None if result is None else result.author,
        platform="youtube",
        video_id=video_id,
    )


def _complete(
    video_id: str,
    *,
    metadata: ExtractionMetadata | None = None,
    transcript: str,
    transcript_state: TranscriptState,
) -> NormalizedExtraction:
    return NormalizedExtraction.create(
        extractor=ExtractorKind.YOUTUBE,
        state=ExtractionState.COMPLETE,
        source_type=SourceType.YOUTUBE,
        content_kind=ContentKind.VIDEO,
        metadata=_metadata(video_id) if metadata is None else metadata,
        text="",
        transcript=transcript,
        transcript_state=transcript_state,
        assets=(),
        failure=None,
    )


def _closed(
    state: ExtractionState, failure: ExtractionFailure, *, video_id: str | None = None
) -> NormalizedExtraction:
    return NormalizedExtraction.create(
        extractor=ExtractorKind.YOUTUBE,
        state=state,
        source_type=SourceType.YOUTUBE,
        content_kind=ContentKind.VIDEO,
        metadata=ExtractionMetadata.create(platform="youtube", video_id=video_id),
        text="",
        transcript=None,
        transcript_state=TranscriptState.NOT_APPLICABLE,
        assets=(),
        failure=failure,
    )
