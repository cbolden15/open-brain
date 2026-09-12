from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
)
from open_brain_engine.engine import PublicJobCaptureSink

from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeMediaResult,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import MediaCommand
from open_brain_legacy._compat.open_brain_connectors.capture.poll import PollItemState
from open_brain_legacy._compat.open_brain_connectors.production.youtube_poll import (
    YouTubePollConfigError,
    compose_production_youtube_poll_runtime,
    load_private_youtube_config,
)
from open_brain_legacy.services.application import SingleUserLocalApplication

FIXED_TIME = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@dataclass
class _MediaAdapter:
    playlist: tuple[str, ...] = ("video000001", "video000002")
    calls: list[str] = field(default_factory=list)

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        del url, command
        self.calls.append("playlist")
        return self.playlist

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        del command
        self.calls.append(video_id)
        return YouTubeMediaResult(
            title="Synthetic video " + video_id,
            caption_vtt="WEBVTT\n\n00:00.000 --> 00:01.000\nSynthetic transcript",
            captions_pending=False,
        )


def _privacy(*, egress: bool = True) -> PrivacyDecision:
    if egress:
        return PrivacyDecision.create(
            tier=PrivacyTier.PUBLIC,
            reason=PrivacyReason.POLICY_PUBLIC,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=True),
        )
    return PrivacyDecision.create(
        tier=PrivacyTier.UNKNOWN,
        reason=PrivacyReason.CLASSIFICATION_MISSING,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _config(path: Path, *, privacy: PrivacyDecision | None = None) -> Path:
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "subscriptions": [
                    {
                        "url": "https://www.youtube.com/playlist?list=synthetic",
                        "privacy": (privacy or _privacy()).to_dict(),
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def test_poll_runtime_accepts_transcripts_once_after_durable_engine_submission(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "youtube.json")
    state_root = tmp_path / "state"
    local = SingleUserLocalApplication.open(tmp_path / "brain")
    media = _MediaAdapter()

    first = compose_production_youtube_poll_runtime(
        config_path=config,
        state_root=state_root,
        sink=local.public_job_sink("JOB-029"),
        media_adapter=media,
        clock=lambda: FIXED_TIME,
    ).run(max_items=10)
    replay = compose_production_youtube_poll_runtime(
        config_path=config,
        state_root=state_root,
        sink=local.public_job_sink("JOB-029"),
        media_adapter=media,
        clock=lambda: FIXED_TIME,
    ).run(max_items=10)

    assert first.discovered_count == 2
    assert first.polled_count == 2
    assert first.created_count == 2
    assert replay.polled_count == 0
    assert replay.created_count == 0
    assert media.calls == ["playlist", "video000001", "video000002", "playlist"]
    assert len(local.tasks.inbox.list()) == 2


def test_local_only_subscription_causes_zero_media_or_engine_effects(tmp_path: Path) -> None:
    config = _config(tmp_path / "youtube.json", privacy=_privacy(egress=False))
    media = _MediaAdapter()
    local = SingleUserLocalApplication.open(tmp_path / "brain")

    result = compose_production_youtube_poll_runtime(
        config_path=config,
        state_root=tmp_path / "state",
        sink=local.public_job_sink("JOB-029"),
        media_adapter=media,
        clock=lambda: FIXED_TIME,
    ).run(max_items=10)

    assert result.discovered_count == 0
    assert result.polled_count == 0
    assert result.created_count == 0
    assert media.calls == []
    assert local.tasks.inbox.list() == ()


def test_poll_checkpoint_stays_unaccepted_when_sink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "youtube.json")
    state_root = tmp_path / "state"
    local = SingleUserLocalApplication.open(tmp_path / "brain")
    runtime = compose_production_youtube_poll_runtime(
        config_path=config,
        state_root=state_root,
        sink=local.public_job_sink("JOB-029"),
        media_adapter=_MediaAdapter(),
        clock=lambda: FIXED_TIME,
    )

    with monkeypatch.context() as patched:
        patched.setattr(
            PublicJobCaptureSink,
            "submit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("sink failure")),
        )
        with pytest.raises(RuntimeError, match="sink failure"):
            runtime.run(max_items=10)

    assert {record.state for record in runtime._state.records()} == {PollItemState.SEEN}
    retried = runtime.run(max_items=10)
    assert retried.created_count == 2
    assert {record.state for record in runtime._state.records()} == {PollItemState.ACCEPTED}


def test_private_youtube_config_requires_owner_only_regular_canonical_file(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "youtube.json")
    config.chmod(0o644)

    with pytest.raises(YouTubePollConfigError, match="private YouTube config"):
        load_private_youtube_config(config)

    config.chmod(0o600)
    linked = tmp_path / "linked.json"
    linked.symlink_to(config)
    with pytest.raises(YouTubePollConfigError, match="private YouTube config"):
        load_private_youtube_config(linked)
