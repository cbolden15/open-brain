from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
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

from open_brain_legacy.operations.capture_jobs import (
    CaptureJobApplication,
    CaptureJobContractError,
    CaptureWrite,
    get_capture_job,
)
from open_brain_legacy.operations.models import JobState, TriggerKind
from open_brain_legacy.operations.render import render_systemd_service, render_systemd_timer
from open_brain_legacy.services.application import SingleUserLocalApplication

FIXED_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _youtube_envelope() -> CaptureEnvelope:
    url = "https://www.youtube.com/watch?v=synthetic001"
    return CaptureEnvelope.create(
        source_type=SourceType.YOUTUBE,
        content_kind=ContentKind.VIDEO,
        source_url=url,
        title=None,
        shared_text="",
        captured_at=FIXED_TIME,
        capture_why="Review before the synthetic planning session",
        capture_why_origin=CaptureWhyOrigin.OWNER_AUTHORED,
        capture_source=CaptureSource.PLAYLIST,
        provenance=Provenance.create(
            source_ref=url,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.OWNER_AUTHORED,
        ),
        raw_assets=(),
        privacy_decision=PrivacyDecision.create(
            tier=PrivacyTier.PUBLIC,
            reason=PrivacyReason.POLICY_PUBLIC,
            policy_version="privacy-v1",
            authority=Authority(cloud=False, external_egress=True),
        ),
    )


def test_job_029_is_enabled_engine_capture_poll_with_persistent_timer(tmp_path: Path) -> None:
    application = get_capture_job("JOB-029")
    service = render_systemd_service(application.job)
    timer = render_systemd_timer(application.job)
    local = SingleUserLocalApplication.open(tmp_path / "brain")
    envelope = _youtube_envelope()

    created = application.submit(sink=local.public_job_sink("JOB-029"), envelope=envelope)
    duplicate = application.submit(sink=local.public_job_sink("JOB-029"), envelope=envelope)

    assert application.argv == (
        "open-brain",
        "capture",
        "poll",
        "--source=youtube",
        "--mode=ingress",
        "--json",
    )
    assert application.job.state is JobState.ENABLED
    assert application.job.trigger.kind is TriggerKind.CALENDAR_INTERVAL
    assert application.job.trigger.interval_seconds == 300
    assert application.job.trigger.persistent is True
    assert application.allowed_writes == frozenset({CaptureWrite.ENGINE_CAPTURE})
    assert application.service_actions == ()
    assert created.disposition.value == "created"
    assert duplicate.disposition.value == "duplicate"
    assert "Type=oneshot" in service
    assert "Restart=no" in service
    assert "OnCalendar=*-*-* *:0/5:00" in timer
    assert "Persistent=true" in timer
    assert "[Install]" not in service + timer
    assert [item.capture_id for item in local.tasks.inbox.list()] == [created.capture_id]
    assert not tuple(tmp_path.glob("*.md"))
    assert not tuple(tmp_path.glob("*.sqlite"))


def test_job_029_rejects_note_state_writes_or_deactivation() -> None:
    application = get_capture_job("JOB-029")

    with pytest.raises(CaptureJobContractError, match="public CLI argv"):
        CaptureJobApplication(
            replace(application.job, command=(*application.argv, "--write-notes"))
        )
    with pytest.raises(CaptureJobContractError, match="remain enabled"):
        CaptureJobApplication(replace(application.job, state=JobState.DISABLED))
