from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

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
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.capture_jobs import CaptureWrite, get_capture_job
from open_brain_legacy.operations.models import HostRole, WriterScope
from open_brain_legacy.services.application import SingleUserLocalApplication

FIXED_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _message_envelope() -> CaptureEnvelope:
    text = "Synthetic message body"
    return CaptureEnvelope.create(
        source_type=SourceType.TEXT,
        content_kind=ContentKind.OTHER,
        source_url=None,
        title=None,
        shared_text=text,
        captured_at=FIXED_TIME,
        capture_why="Retain this synthetic context",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.INTEGRATION,
        provenance=Provenance.create(
            source_ref="urn:open-brain:text:sha256:" + sha256(text.encode()).hexdigest(),
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL,
            reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=False),
        ),
    )


def test_job_005_submits_one_idempotent_redacted_engine_capture(tmp_path: Path) -> None:
    application = get_capture_job("JOB-005")
    local = SingleUserLocalApplication.open(tmp_path / "brain")
    envelope = _message_envelope()

    created = application.submit(sink=local.public_job_sink("JOB-005"), envelope=envelope)
    duplicate = application.submit(sink=local.public_job_sink("JOB-005"), envelope=envelope)

    assert application.argv == (
        "open-brain",
        "capture",
        "imessage-ingress",
        "--append",
        "--json",
    )
    assert application.job.host_role is HostRole.INGRESS
    assert application.job.writer_scope is WriterScope.CAPTURE_INGRESS
    assert application.job.lock_scope is LockScope.INGRESS
    assert application.job.env_refs == (
        "OPEN_BRAIN_CONFIG",
        "OPEN_BRAIN_IMESSAGE_CONFIG",
    )
    assert application.allowed_writes == frozenset({CaptureWrite.ENGINE_CAPTURE})
    assert application.service_actions == ()
    assert created.to_dict() == {
        "capture_id": created.capture_id,
        "disposition": "created",
        "job_id": "JOB-005",
    }
    assert duplicate.to_dict() == {
        **created.to_dict(),
        "disposition": "duplicate",
    }

    assert [item.capture_id for item in local.tasks.inbox.list()] == [created.capture_id]
    report = repr((created.to_dict(), duplicate.to_dict()))
    assert envelope.shared_text not in report
    assert envelope.capture_why not in report
