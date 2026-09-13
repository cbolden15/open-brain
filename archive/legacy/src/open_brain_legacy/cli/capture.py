"""Thin, redacted CLI adapters for text capture and share submission."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from open_brain_engine.capture.models import CaptureWorkItem, ShareRequest, ShareResponse
from open_brain_engine.core.models import (
    CaptureEnvelope,
    CaptureSource,
    CaptureWhyOrigin,
    ContentKind,
    ContentOrigin,
    PrivacyTier,
    Provenance,
    SourceType,
)
from open_brain_engine.core.policy import classify_privacy
from open_brain_engine.core.ports import PutDisposition, PutResult

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error

_PRIVACY_POLICY_VERSION = "privacy-v1"


class CaptureEnqueuer(Protocol):
    def enqueue(self, item: CaptureWorkItem, *, item_id: str, payload_digest: str) -> PutResult: ...


class ShareSubmitter(Protocol):
    def submit(self, request: ShareRequest) -> ShareResponse: ...


@dataclass(frozen=True, slots=True)
class CaptureCliResult:
    """A deterministic public result that excludes captured content and reasons."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize the opaque response for automation callers."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def capture_stdin(
    *,
    queue: CaptureEnqueuer,
    now: datetime,
    text: str,
    why: str,
    dry_run: bool = False,
    privacy_tier: PrivacyTier | str | None = None,
) -> CaptureCliResult:
    """Capture supplied standard-input text without exposing it in the response."""
    return capture_text(
        queue=queue,
        now=now,
        text=text,
        why=why,
        dry_run=dry_run,
        privacy_tier=privacy_tier,
    )


def capture_text(
    *,
    queue: CaptureEnqueuer,
    now: datetime,
    text: str,
    why: str,
    dry_run: bool = False,
    privacy_tier: PrivacyTier | str | None = None,
) -> CaptureCliResult:
    """Build a CLI text capture and enqueue it unless this is a dry run."""
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("invalid dry run")
        item = _text_item(
            now=now,
            text=text,
            why=why,
            privacy_tier=privacy_tier,
        )
    except Exception:
        return _failed("capture")

    envelope = item.envelope
    result_envelope = {
        "capture_id": str(envelope.capture_id),
        "command": "capture",
        "dry_run": dry_run,
        "privacy_tier": envelope.privacy_decision.tier.value,
        "source_type": envelope.source_type.value,
        "status": "planned" if dry_run else "queued",
    }
    if dry_run:
        return CaptureCliResult(ExitCode.SUCCESS, result_envelope)

    try:
        put_result = queue.enqueue(
            item,
            item_id=str(envelope.capture_id),
            payload_digest=item.payload_digest_sha256(),
        )
        if not isinstance(put_result, PutResult) or put_result.disposition not in {
            PutDisposition.CREATED,
            PutDisposition.DUPLICATE,
        }:
            raise ValueError("invalid queue result")
    except Exception:
        return _failed("capture")
    return CaptureCliResult(ExitCode.SUCCESS, result_envelope)


def share_capture(
    *,
    submitter: ShareSubmitter,
    url: str,
    why: str,
    text: str = "",
    dry_run: bool = False,
    privacy_tier: PrivacyTier | str | None = None,
) -> CaptureCliResult:
    """Validate and submit a share request without returning its private inputs."""
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("invalid dry run")
        request = ShareRequest.create(
            url=url,
            why=why,
            text=text,
            privacy_tier=privacy_tier,
        )
    except Exception:
        return _failed("share")
    if dry_run:
        return CaptureCliResult(
            ExitCode.SUCCESS,
            {"command": "share", "dry_run": True, "status": "planned"},
        )

    try:
        response = submitter.submit(request)
        if not isinstance(response, ShareResponse):
            raise ValueError("invalid share response")
    except Exception:
        return _failed("share")
    return CaptureCliResult(
        ExitCode.SUCCESS,
        {
            "capture_id": str(response.capture_id),
            "command": "share",
            "dry_run": False,
            "duplicate": response.duplicate,
            "pipeline": response.pipeline.value,
            "status": response.status.value,
        },
    )


def _text_item(
    *,
    now: datetime,
    text: str,
    why: str,
    privacy_tier: PrivacyTier | str | None,
) -> CaptureWorkItem:
    normalized_text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    envelope = CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=normalized_text,
        captured_at=now,
        capture_why=why,
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.CLI,
        provenance=Provenance.create(
            source_ref="urn:open-brain:text:sha256:"
            + sha256(normalized_text.encode("utf-8")).hexdigest(),
            content_origin=ContentOrigin.OWNER_AUTHORED,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=classify_privacy(
            privacy_tier,
            policy_version=_PRIVACY_POLICY_VERSION,
        ),
    )
    return CaptureWorkItem.create(envelope=envelope, available_at=envelope.captured_at)


def _failed(command: str) -> CaptureCliResult:
    return CaptureCliResult(
        ExitCode.FAILURE,
        {
            "command": command,
            "error": redacted_error(f"{command}_operation_failed"),
            "status": "failed",
        },
    )
