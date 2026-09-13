from __future__ import annotations

import socket
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import CaptureReceipt, PublicJobCaptureSink

from open_brain_legacy._compat.open_brain.services.connectors import (
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCaptureIdentity,
    ConnectorCaptureSink,
    ConnectorMetadataLogger,
    ConnectorRunContext,
    ConnectorRunEvidence,
)
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeMediaAdapter,
    YouTubeMediaResult,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import MediaCommand
from open_brain_legacy._compat.open_brain_connectors.capture.poll import (
    FilesystemYouTubePollState,
    PollItemState,
    PollRecord,
    PollRequestOrigin,
)
from open_brain_legacy._compat.open_brain_connectors.production.youtube_poll import (
    YouTubePollCheckpoint,
    YouTubeReferenceConnector,
    YouTubeReferenceTransport,
    YouTubeSubscription,
)
from open_brain_legacy.services.application import SingleUserLocalApplication

FIXED_TIME = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@dataclass
class _SyntheticTransport:
    playlist: tuple[str, ...] = ("video000001",)
    calls: list[str] = field(default_factory=list)

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        del url, command
        self.calls.append("playlist")
        return self.playlist

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        del command
        self.calls.append(video_id)
        return YouTubeMediaResult(
            title="Synthetic third-party video",
            caption_vtt="WEBVTT\n\n00:00.000 --> 00:01.000\nSynthetic transcript",
            captions_pending=False,
        )


@dataclass
class _MultiPlaylistTransport:
    pages: dict[str, tuple[str, ...]]
    calls: list[str] = field(default_factory=list)

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        del command
        self.calls.append(url)
        return self.pages[url]

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        del command
        self.calls.append(video_id)
        return YouTubeMediaResult(
            title="Synthetic bounded video",
            caption_vtt="WEBVTT\n\n00:00.000 --> 00:01.000\nBounded transcript",
            captions_pending=False,
        )


class _CrashAfterAcceptanceCheckpoint(YouTubePollCheckpoint):
    def __init__(self, state: FilesystemYouTubePollState) -> None:
        super().__init__(state)
        self._crash_once = True

    def commit_acceptance(
        self,
        previous: PollRecord,
        *,
        delivery_id: str,
        receipt: CaptureReceipt,
    ) -> PollRecord:
        if self._crash_once:
            self._crash_once = False
            raise RuntimeError("synthetic crash after engine acceptance")
        return super().commit_acceptance(
            previous,
            delivery_id=delivery_id,
            receipt=receipt,
        )


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=True),
    )


def _context(
    application: SingleUserLocalApplication,
    state: FilesystemYouTubePollState,
    transport: YouTubeMediaAdapter,
    *,
    limits: ConnectorBudgetLimits | None = None,
    checkpoint: YouTubePollCheckpoint | None = None,
) -> ConnectorRunContext:
    budget = ConnectorBudget(ConnectorBudgetLimits() if limits is None else limits)
    evidence = ConnectorRunEvidence()
    return ConnectorRunContext(
        capture_identity=ConnectorCaptureIdentity(
            "youtube",
            "JOB-029",
            application.public_job_context("JOB-029"),
        ),
        capture_sink=ConnectorCaptureSink(
            application.public_job_sink("JOB-029"),
            budget,
            evidence,
        ),
        transport=YouTubeReferenceTransport(
            subscriptions=(
                YouTubeSubscription(
                    url="https://www.youtube.com/playlist?list=synthetic",
                    privacy=_privacy(),
                ),
            ),
            media_adapter=transport,
        ).bind_budget(budget),
        checkpoint=(
            YouTubePollCheckpoint(state) if checkpoint is None else checkpoint
        ).bind_run(budget, evidence),
        clock=lambda: FIXED_TIME,
        budget=budget,
        metadata_logger=ConnectorMetadataLogger(),
    )


def test_synthetic_youtube_uses_injected_transport_with_global_bounds_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    state = FilesystemYouTubePollState(tmp_path / "state")
    transport = _SyntheticTransport()
    connector = YouTubeReferenceConnector()
    monkeypatch.setattr(socket, "create_connection", _deny_socket)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_socket)

    receipt = connector.run(
        _context(
            application,
            state,
            transport,
            limits=ConnectorBudgetLimits(max_fetches=1, max_extractions=1, max_submissions=1),
        )
    )
    results = application.tasks.retrieval.search("Synthetic transcript")

    assert transport.calls == ["playlist", "video000001"]
    assert (receipt.fetched_count, receipt.extracted_count, receipt.submitted_count) == (1, 1, 1)
    assert receipt.created_count == 1
    assert receipt.checkpoint_committed is True
    assert connector.manifest.payloads[0].value == "reference_or_file"
    assert connector.manifest.schedules == ("JOB-029",)
    assert connector.manifest.secrets == connector.manifest.action_authorities == ()
    assert [(result.record_type, result.trust) for result in results] == [("source", "third_party")]
    assert {record.state for record in state.records()} == {PollItemState.ACCEPTED}


def test_multiple_playlists_share_one_discovery_and_receipt_budget(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    state = FilesystemYouTubePollState(tmp_path / "state")
    urls = tuple(
        f"https://www.youtube.com/playlist?list=synthetic-{index}" for index in range(3)
    )
    transport = _MultiPlaylistTransport(
        pages={
            urls[0]: tuple(f"0{index:010d}" for index in range(3)),
            urls[1]: tuple(f"1{index:010d}" for index in range(2)),
            urls[2]: tuple(f"2{index:010d}" for index in range(3)),
        }
    )
    context = _context(
        application,
        state,
        transport,
        limits=ConnectorBudgetLimits(
            max_discoveries=5,
            max_fetches=3,
            max_extractions=5,
            max_submissions=5,
        ),
    )
    context = replace(
        context,
        transport=YouTubeReferenceTransport(
            subscriptions=tuple(
                YouTubeSubscription(url=url, privacy=_privacy()) for url in urls
            ),
            media_adapter=transport,
        ).bind_budget(context.budget),
    )

    receipt = YouTubeReferenceConnector().run(context)

    assert receipt.discovered_count == 5
    assert receipt.fetched_count == 2
    assert receipt.extracted_count == receipt.submitted_count == 5
    assert receipt.created_count == 5
    assert receipt.checkpoint_committed is True
    assert transport.calls[:2] == [urls[0], urls[1]]
    assert urls[2] not in transport.calls
    assert len(application.tasks.inbox.list()) == 5


def test_discovery_budget_is_reserved_before_checkpoint_mutation(tmp_path: Path) -> None:
    state = FilesystemYouTubePollState(tmp_path / "state")
    budget = ConnectorBudget(ConnectorBudgetLimits(max_discoveries=1))
    checkpoint = YouTubePollCheckpoint(state).bind_run(
        budget,
        ConnectorRunEvidence(),
    )
    records = tuple(
        PollRecord.create(
            video_id=f"bounded{index:04d}",
            source_url=f"https://www.youtube.com/watch?v=bounded{index:04d}",
            state=PollItemState.REQUESTED,
            origin=PollRequestOrigin.PLAYLIST,
            requested_at=FIXED_TIME,
            capture_id=None,
            capture_why="",
            privacy=_privacy(),
        )
        for index in range(3)
    )

    checkpoint.request(records[0])
    for record in records[1:]:
        with pytest.raises(ValueError, match="budget exhausted"):
            checkpoint.request(record)

    assert budget.discovered_count == 1
    assert state.records() == (records[0],)


def test_youtube_replay_has_one_capture_and_preserves_stable_delivery_checkpoint(
    tmp_path: Path,
) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    state = FilesystemYouTubePollState(tmp_path / "state")
    transport = _SyntheticTransport()
    connector = YouTubeReferenceConnector()

    first = connector.run(_context(application, state, transport))
    replay = connector.run(_context(application, state, transport))

    assert first.created_count == 1
    assert replay.created_count == replay.duplicate_count == replay.submitted_count == 0
    assert len(application.tasks.inbox.list()) == 1
    assert transport.calls == ["playlist", "video000001", "playlist"]
    accepted = state.records()[0]
    assert accepted.state is PollItemState.ACCEPTED
    assert accepted.capture_id == application.tasks.inbox.list()[0].capture_id


def test_empty_youtube_run_returns_bounded_empty_receipt(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    state = FilesystemYouTubePollState(tmp_path / "state")
    transport = _SyntheticTransport()
    context = _context(application, state, transport)
    context = replace(
        context,
        transport=YouTubeReferenceTransport(
            subscriptions=(), media_adapter=transport
        ).bind_budget(context.budget),
    )

    receipt = YouTubeReferenceConnector().run(context)

    assert receipt.outcome.value == "empty"
    assert receipt.fetched_count == receipt.extracted_count == receipt.submitted_count == 0
    assert receipt.checkpoint_committed is False
    assert transport.calls == []


def test_sink_failure_retains_seen_checkpoint_and_crash_replays_same_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    state = FilesystemYouTubePollState(tmp_path / "state")
    transport = _SyntheticTransport()
    connector = YouTubeReferenceConnector()

    with monkeypatch.context() as patched:
        patched.setattr(
            PublicJobCaptureSink,
            "submit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic sink failure")),
        )
        with pytest.raises(RuntimeError, match="synthetic sink failure"):
            connector.run(_context(application, state, transport))

    assert {record.state for record in state.records()} == {PollItemState.SEEN}
    with pytest.raises(RuntimeError, match="crash after engine acceptance"):
        connector.run(
            _context(
                application,
                state,
                transport,
                checkpoint=_CrashAfterAcceptanceCheckpoint(state),
            )
        )

    assert len(application.tasks.inbox.list()) == 1
    replay = connector.run(_context(application, state, transport))

    assert replay.created_count == 0
    assert replay.duplicate_count == replay.submitted_count == 1
    assert state.records()[0].state is PollItemState.ACCEPTED
    assert len(application.tasks.inbox.list()) == 1


def test_checkpoint_acceptance_requires_sink_issued_capture_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    state = FilesystemYouTubePollState(tmp_path / "state")
    with monkeypatch.context() as patched:
        patched.setattr(
            PublicJobCaptureSink,
            "submit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("synthetic sink failure")
            ),
        )
        with pytest.raises(RuntimeError, match="synthetic sink failure"):
            YouTubeReferenceConnector().run(
                _context(application, state, _SyntheticTransport())
            )
    seen = state.records()[0]
    forged_capture_id = "capture_" + "a" * 64
    forged = PollRecord.from_dict(
        {
            **seen.to_dict(),
            "capture_id": forged_capture_id,
            "state": PollItemState.ACCEPTED.value,
        }
    )
    evidence = ConnectorRunEvidence()
    checkpoint = YouTubePollCheckpoint(state).bind_run(
        ConnectorBudget(ConnectorBudgetLimits()),
        evidence,
    )
    forged_receipt = CaptureReceipt(
        capture_id=forged_capture_id,
        payload_family="reference_or_file",
        state="queued",
        enrichment_state="not_requested",
        space_id=None,
        canonical_path=None,
    )

    with pytest.raises(ValueError, match="capture receipt required"):
        checkpoint.replace(seen, forged)
    with pytest.raises(ValueError, match="capture receipt required"):
        checkpoint.commit_acceptance(
            seen,
            delivery_id=f"connector.youtube.{seen.video_id}",
            receipt=forged_receipt,
        )

    assert state.records() == (seen,)
    assert checkpoint.checkpoint_committed is False
    assert application.tasks.inbox.list() == ()


def _deny_socket(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("synthetic connector must not use socket or DNS")
