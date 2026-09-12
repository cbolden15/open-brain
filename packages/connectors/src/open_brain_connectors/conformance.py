"""Reference conformance proof for the provisional connector worker."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from open_brain_engine.engine import (
    CaptureReceipt,
    CaptureSubmission,
    CaptureTask,
    PrivacyDecision,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    canonical_json_bytes,
)

from open_brain_connectors.capture.extractors.youtube import YouTubeMediaResult
from open_brain_connectors.capture.media import MediaCommand
from open_brain_connectors.capture.poll import FilesystemYouTubePollState
from open_brain_connectors.production.youtube_poll import (
    YouTubePollCheckpoint,
    YouTubeReferenceConnector,
    YouTubeReferenceTransport,
    YouTubeSubscription,
)
from open_brain_connectors.runtime.connector_worker_v1 import (
    ConnectorNetworkMode,
    ConnectorWorkerProtocolError,
    ConnectorWorkerReceipt,
    ConnectorWorkerRequest,
    connector_manifest_sha256,
)
from open_brain_connectors.runtime.connectors import (
    ConnectorBudget,
    ConnectorCaptureIdentity,
    ConnectorCaptureSink,
    ConnectorMetadataLogger,
    ConnectorRunContext,
    ConnectorRunEvidence,
)

_FIXED_TIME = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class _ConformanceCaptureTask:
    def __init__(self) -> None:
        self._receipts: dict[str, tuple[str, CaptureReceipt]] = {}

    @property
    def capture_count(self) -> int:
        return len(self._receipts)

    def submit(self, submission: CaptureSubmission) -> CaptureReceipt:
        request_sha256 = sha256(canonical_json_bytes(submission.request_value())).hexdigest()
        existing = self._receipts.get(submission.delivery_id)
        if existing is not None:
            if existing[0] != request_sha256:
                raise ValueError("conflicting conformance delivery")
            return replace(existing[1], duplicate=True)
        receipt = CaptureReceipt(
            capture_id="capture_" + sha256(submission.delivery_id.encode("utf-8")).hexdigest(),
            payload_family=submission.payload.family,
            state="accepted",
            enrichment_state="pending_enrichment",
            space_id=None,
            canonical_path=None,
            duplicate=False,
        )
        self._receipts[submission.delivery_id] = (request_sha256, receipt)
        return receipt


@dataclass(slots=True)
class _ConformanceMediaAdapter:
    calls: int = 0

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        del url, command
        self.calls += 1
        return ("video000001",)

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        del video_id, command
        self.calls += 1
        return YouTubeMediaResult(
            title="Synthetic connector conformance",
            caption_vtt="WEBVTT\n\n00:00.000 --> 00:01.000\nSynthetic connector proof",
            captions_pending=False,
        )


class YouTubeConnectorPlugin:
    """Installed entry-point object exposing isolated conformance only."""

    manifest = YouTubeReferenceConnector.manifest

    def conformance(self, request: ConnectorWorkerRequest) -> ConnectorWorkerReceipt:
        if (
            type(request) is not ConnectorWorkerRequest
            or request.connector_name != self.manifest.name
            or request.manifest != self.manifest
            or request.network_mode is not ConnectorNetworkMode.HOST_MEDIATED
            or request.manifest.secrets
            or request.manifest.action_authorities
        ):
            raise ConnectorWorkerProtocolError("invalid reference conformance request")
        capture_task = _ConformanceCaptureTask()
        media = _ConformanceMediaAdapter()
        with TemporaryDirectory(prefix="open-brain-connector-conformance-") as raw_root:
            state = FilesystemYouTubePollState(Path(raw_root))
            first = YouTubeReferenceConnector().run(
                _context(request, state=state, capture_task=capture_task, media=media)
            )
            replay = YouTubeReferenceConnector().run(
                _context(request, state=state, capture_task=capture_task, media=media)
            )
            checkpoint_receipt = sha256(
                canonical_json_bytes([record.to_dict() for record in state.records()])
            ).hexdigest()
        return ConnectorWorkerReceipt(
            schema_version=1,
            invocation_id=request.invocation_id,
            connector_name=request.connector_name,
            manifest_sha256=connector_manifest_sha256(self.manifest),
            first_run=first,
            replay_run=replay,
            checkpoint_receipt_sha256=checkpoint_receipt,
            capture_count=capture_task.capture_count,
            direct_network_attempts=0,
        )


def _context(
    request: ConnectorWorkerRequest,
    *,
    state: FilesystemYouTubePollState,
    capture_task: _ConformanceCaptureTask,
    media: _ConformanceMediaAdapter,
) -> ConnectorRunContext:
    budget = ConnectorBudget(request.budget_limits)
    evidence = ConnectorRunEvidence()
    actor = PublicJobCaptureContext(
        tenant_id="tenant_00000000-0000-4000-8000-000000000001",
        actor_id="actor_00000000-0000-4000-8000-000000000002",
        role_claim={
            "actor_id": "actor_00000000-0000-4000-8000-000000000002",
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000003",
            "role_id": "role_00000000-0000-4000-8000-000000000004",
            "tenant_id": "tenant_00000000-0000-4000-8000-000000000001",
        },
    )
    return ConnectorRunContext(
        capture_identity=ConnectorCaptureIdentity("youtube", "JOB-029", actor),
        capture_sink=ConnectorCaptureSink(
            PublicJobCaptureSink(cast(CaptureTask, capture_task), context=actor),
            budget,
            evidence,
        ),
        transport=YouTubeReferenceTransport(
            subscriptions=(
                YouTubeSubscription(
                    url="https://www.youtube.com/playlist?list=conformance",
                    privacy=_privacy(),
                ),
            ),
            media_adapter=media,
        ).bind_budget(budget),
        checkpoint=YouTubePollCheckpoint(state).bind_run(budget, evidence),
        clock=lambda: _FIXED_TIME,
        budget=budget,
        metadata_logger=ConnectorMetadataLogger(),
    )


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }
    )


connector = YouTubeConnectorPlugin()

__all__ = ["YouTubeConnectorPlugin", "connector"]
