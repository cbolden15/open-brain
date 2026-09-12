"""Bounded production adapters for playlist and social downloaded media."""

from __future__ import annotations

import os
import re
import stat
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from open_brain_engine.capture.models import ExtractionFailure
from open_brain_engine.core.ids import canonicalize_source_url
from open_brain_engine.core.models import RawAssetRef

from open_brain_legacy._compat.open_brain.config import AppConfig
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeMediaResult,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import (
    BoundedMediaRunner,
    MediaCommand,
    MediaRunResult,
    MediaTool,
)
from open_brain_legacy.capture.extractors.social import SocialMediaResult
from open_brain_legacy.production.assets import ContentAddressedRawAssetStore
from open_brain_legacy.production.errors import ProductionRuntimeError, RuntimeFailureCode

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_MAX_PLAYLIST_ITEMS = 50
_MEDIA_EXECUTABLE_CANDIDATES = {
    "yt-dlp": (
        Path("/opt/homebrew/bin/yt-dlp"),
        Path("/usr/local/bin/yt-dlp"),
        Path("/usr/bin/yt-dlp"),
    ),
    "gallery-dl": (
        Path("/opt/homebrew/bin/gallery-dl"),
        Path("/usr/local/bin/gallery-dl"),
        Path("/usr/bin/gallery-dl"),
    ),
}


class MediaRunner(Protocol):
    def run(self, command: MediaCommand) -> MediaRunResult: ...


class MediaAssetReader(Protocol):
    def read(self, asset: RawAssetRef) -> bytes: ...


class BoundedCaptureMediaAdapter:
    """Build only static media argv and verify every persisted asset before return."""

    def __init__(self, *, runner: MediaRunner, asset_reader: MediaAssetReader) -> None:
        if not callable(getattr(runner, "run", None)) or not callable(
            getattr(asset_reader, "read", None)
        ):
            raise ValueError("invalid capture media adapter")
        self._runner = runner
        self._asset_reader = asset_reader

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        if (
            not isinstance(video_id, str)
            or _VIDEO_ID.fullmatch(video_id) is None
            or not _youtube_command(command)
        ):
            return YouTubeMediaResult(failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT)
        url = "https://www.youtube.com/watch?v=" + video_id
        result = self._runner.run(
            MediaCommand(
                argv=(
                    "yt-dlp",
                    "--ignore-config",
                    "--skip-download",
                    "--no-playlist",
                    "--no-progress",
                    "--no-warnings",
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-langs",
                    "en",
                    "--sub-format",
                    "vtt",
                    "--output",
                    "caption.%(ext)s",
                    "--print",
                    "%(title)s",
                    "--print",
                    "%(uploader)s",
                    url,
                ),
                limits=command.limits,
            )
        )
        if result.failure is not None:
            return YouTubeMediaResult(failure=result.failure)
        if not result.reaped or result.stderr:
            return YouTubeMediaResult(failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT)
        lines = _lines(result.stdout, maximum=2)
        if lines is None or len(result.assets) > 1:
            return YouTubeMediaResult(failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT)
        caption: str | None = None
        if result.assets:
            asset = result.assets[0]
            if asset.media_type != "text/vtt":
                return YouTubeMediaResult(failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT)
            payload = _verified_asset(self._asset_reader, asset)
            if payload is None:
                return YouTubeMediaResult(failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT)
            try:
                caption = payload.decode("utf-8")
            except UnicodeDecodeError:
                return YouTubeMediaResult(failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT)
        return YouTubeMediaResult(
            title=lines[0] if lines else None,
            author=lines[1] if len(lines) == 2 else None,
            caption_vtt=caption,
            captions_pending=caption is None,
            failure=None,
        )

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        if not _youtube_command(command):
            return ()
        try:
            canonical = canonicalize_source_url(url)
            parsed = urlsplit(canonical)
        except ValueError:
            return ()
        if parsed.hostname not in _YOUTUBE_HOSTS or not parse_qs(parsed.query).get("list"):
            return ()
        result = self._runner.run(
            MediaCommand(
                argv=(
                    "yt-dlp",
                    "--ignore-config",
                    "--flat-playlist",
                    "--playlist-end",
                    str(_MAX_PLAYLIST_ITEMS),
                    "--no-progress",
                    "--no-warnings",
                    "--print",
                    "%(id)s",
                    canonical,
                ),
                limits=command.limits,
            )
        )
        if result.failure is not None or result.assets or result.stderr or not result.reaped:
            return ()
        lines = _lines(result.stdout, maximum=_MAX_PLAYLIST_ITEMS)
        if lines is None or any(_VIDEO_ID.fullmatch(line) is None for line in lines):
            return ()
        return tuple(lines)

    def download(
        self,
        url: str,
        *,
        tool: MediaTool,
        command: MediaCommand,
    ) -> SocialMediaResult:
        try:
            canonical = canonicalize_source_url(url)
        except ValueError:
            return SocialMediaResult(used_tool=tool, failure=ExtractionFailure.INVALID_INPUT)
        if not isinstance(tool, MediaTool) or not _social_command(command, tool):
            return SocialMediaResult(
                used_tool=tool,
                failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT,
            )
        argv = (
            (
                "yt-dlp",
                "--ignore-config",
                "--no-playlist",
                "--no-progress",
                "--no-warnings",
                "--max-downloads",
                "1",
                "--output",
                "asset.%(ext)s",
                canonical,
            )
            if tool is MediaTool.YT_DLP
            else (
                "gallery-dl",
                "--config-ignore",
                "--no-mtime",
                "--directory",
                ".",
                "--filename",
                "asset-{num}.{extension}",
                canonical,
            )
        )
        result = self._runner.run(MediaCommand(argv=argv, limits=command.limits))
        if result.failure is not None:
            return SocialMediaResult(used_tool=tool, failure=result.failure)
        if not result.reaped or result.stderr or not result.assets:
            return SocialMediaResult(
                used_tool=tool,
                failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT,
            )
        if any(_verified_asset(self._asset_reader, asset) is None for asset in result.assets):
            return SocialMediaResult(
                used_tool=tool,
                failure=ExtractionFailure.MALFORMED_TOOL_OUTPUT,
            )
        return SocialMediaResult(assets=result.assets, used_tool=tool, failure=None)


def compose_production_capture_media_adapter(
    *,
    config: AppConfig,
) -> BoundedCaptureMediaAdapter:
    """Bind fixed downloader paths to owner-only staging and immutable objects."""
    if not isinstance(config, AppConfig):
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
    if not config.egress_enabled:
        raise ProductionRuntimeError(RuntimeFailureCode.DISABLED)
    assets_root = config.state_root / "derived-assets"
    staging_root = config.state_root / "media-stage"
    _ensure_owner_directory(assets_root)
    _ensure_owner_directory(staging_root)
    executables: list[str] = []
    for tool in ("yt-dlp", "gallery-dl"):
        selected = next(
            (
                candidate
                for candidate in _MEDIA_EXECUTABLE_CANDIDATES[tool]
                if candidate.is_file() and os.access(candidate, os.X_OK)
            ),
            None,
        )
        if selected is not None:
            executables.append(str(selected))
        elif tool == "yt-dlp":
            raise ProductionRuntimeError(RuntimeFailureCode.EXECUTION_FAILED)
    try:
        asset_store = ContentAddressedRawAssetStore(root=assets_root, enabled=True)
        runner = BoundedMediaRunner(
            allowed_executables=tuple(executables),
            staging_parent=staging_root,
            asset_store=asset_store,
        )
        return BoundedCaptureMediaAdapter(runner=runner, asset_reader=asset_store)
    except (OSError, ValueError) as error:
        raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT) from error


def _ensure_owner_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ProductionRuntimeError(RuntimeFailureCode.CONFINEMENT)


def _youtube_command(command: object) -> bool:
    return (
        isinstance(command, MediaCommand)
        and command.argv == ("yt-dlp", "--skip-download", "--no-playlist")
        and command.environment == ()
    )


def _social_command(command: object, tool: MediaTool) -> bool:
    expected = (
        ("yt-dlp", "--no-playlist")
        if tool is MediaTool.YT_DLP
        else ("gallery-dl",)
    )
    return (
        isinstance(command, MediaCommand)
        and command.argv == expected
        and command.environment == ()
    )


def _lines(payload: bytes, *, maximum: int) -> list[str] | None:
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = [line.strip() for line in decoded.splitlines() if line.strip()]
    if len(lines) > maximum or any(len(line.encode("utf-8")) > 512 for line in lines):
        return None
    return lines


def _verified_asset(reader: MediaAssetReader, asset: RawAssetRef) -> bytes | None:
    try:
        payload = reader.read(asset)
    except Exception:
        return None
    if (
        not isinstance(payload, bytes)
        or len(payload) != asset.byte_length
        or sha256(payload).hexdigest() != asset.sha256
    ):
        return None
    return payload

__all__ = [
    "BoundedCaptureMediaAdapter",
    "MediaAssetReader",
    "MediaRunner",
    "compose_production_capture_media_adapter",
]
