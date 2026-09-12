from __future__ import annotations

import stat
import sys
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.models import RawAssetRef

from open_brain_legacy._compat.open_brain.config import AppConfig, RetainedRoots
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeMediaResult,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import (
    DEFAULT_MEDIA_LIMITS,
    MediaCommand,
    MediaRunResult,
    MediaTool,
)
from open_brain_legacy.capture.extractors.social import SocialMediaResult
from open_brain_legacy.production.errors import ProductionRuntimeError, RuntimeFailureCode
from open_brain_legacy.production.media import (
    BoundedCaptureMediaAdapter,
    compose_production_capture_media_adapter,
)


def _asset(data: bytes, media_type: str = "text/vtt") -> RawAssetRef:
    digest = sha256(data).hexdigest()
    return RawAssetRef.create(
        asset_id="asset_" + digest,
        sha256=digest,
        media_type=media_type,
        byte_length=len(data),
    )


@dataclass
class _Runner:
    results: list[MediaRunResult]
    commands: list[MediaCommand] = field(default_factory=list)

    def run(self, command: MediaCommand) -> MediaRunResult:
        self.commands.append(command)
        return self.results.pop(0)


@dataclass
class _Reader:
    blobs: dict[str, bytes]

    def read(self, asset: RawAssetRef) -> bytes:
        return self.blobs[str(asset.asset_id)]


def _config(tmp_path: Path, *, egress_enabled: bool) -> AppConfig:
    tmp_path.mkdir(parents=True)
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
        egress_enabled=egress_enabled,
    )


def test_youtube_media_uses_persisted_vtt_and_bounded_metadata() -> None:
    vtt = b"WEBVTT\n\n00:00.000 --> 00:01.000\nSynthetic caption"
    ref = _asset(vtt)
    runner = _Runner(
        [MediaRunResult((ref,), b"Synthetic title\nSynthetic author\n", b"", None, True)]
    )
    adapter = BoundedCaptureMediaAdapter(
        runner=runner,
        asset_reader=_Reader({str(ref.asset_id): vtt}),
    )

    result = adapter.media(
        "dQw4w9WgXcQ",
        command=MediaCommand(
            argv=("yt-dlp", "--skip-download", "--no-playlist"),
            limits=DEFAULT_MEDIA_LIMITS,
        ),
    )

    assert result == YouTubeMediaResult(
        title="Synthetic title",
        author="Synthetic author",
        caption_vtt=vtt.decode(),
        captions_pending=False,
    )
    command = runner.commands[0]
    assert command.argv[0] == "yt-dlp"
    assert command.argv[-1] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert "--write-auto-subs" in command.argv


def test_playlist_discovery_and_social_download_are_bounded_and_closed() -> None:
    image = _asset(b"image", "image/jpeg")
    runner = _Runner(
        [
            MediaRunResult((), b"video000001\nvideo000002\n", b"", None, True),
            MediaRunResult((image,), b"", b"", None, True),
        ]
    )
    adapter = BoundedCaptureMediaAdapter(
        runner=runner,
        asset_reader=_Reader({str(image.asset_id): b"image"}),
    )

    playlist = adapter.playlist_items(
        "https://www.youtube.com/playlist?list=synthetic",
        command=MediaCommand(
            argv=("yt-dlp", "--skip-download", "--no-playlist"),
            limits=DEFAULT_MEDIA_LIMITS,
        ),
    )
    social = adapter.download(
        "https://x.com/synthetic/status/1",
        tool=MediaTool.YT_DLP,
        command=MediaCommand(
            argv=("yt-dlp", "--no-playlist"),
            limits=DEFAULT_MEDIA_LIMITS,
        ),
    )

    assert playlist == ("video000001", "video000002")
    assert social == SocialMediaResult(
        assets=(image,),
        used_tool=MediaTool.YT_DLP,
        failure=None,
    )
    assert "--playlist-end" in runner.commands[0].argv
    assert runner.commands[1].argv[-1] == "https://x.com/synthetic/status/1"


def test_production_media_factory_requires_egress_and_owner_only_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ProductionRuntimeError) as disabled:
        compose_production_capture_media_adapter(
            config=_config(tmp_path / "disabled", egress_enabled=False)
        )
    assert disabled.value.code is RuntimeFailureCode.DISABLED

    executable_root = tmp_path / "bin"
    executable_root.mkdir()
    yt_dlp = executable_root / "yt-dlp"
    yt_dlp.symlink_to(Path(sys.executable))
    monkeypatch.setattr(
        'open_brain_legacy.production.media._MEDIA_EXECUTABLE_CANDIDATES',
        {"yt-dlp": (yt_dlp,), "gallery-dl": ()},
    )
    config = _config(tmp_path / "enabled", egress_enabled=True)

    adapter = compose_production_capture_media_adapter(config=config)

    assert isinstance(adapter, BoundedCaptureMediaAdapter)
    for path in (
        config.state_root / "derived-assets",
        config.state_root / "media-stage",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
