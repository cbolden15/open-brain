from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from open_brain_legacy._compat.open_brain.capture.http import HttpRequest, ShareHttpHandler
from open_brain_legacy.operations.capture_jobs import CaptureWrite, get_capture_job
from open_brain_legacy.operations.models import JobState, RetryPolicy
from open_brain_legacy.operations.render import render_systemd_service
from open_brain_legacy.services.application import SingleUserLocalApplication

FIXED_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def test_job_028_renders_enabled_ingress_only_service_and_queues_share(
    tmp_path: Path,
) -> None:
    application = get_capture_job("JOB-028")
    service = render_systemd_service(application.job)
    body = json.dumps(
        {
            "url": "https://example.test/synthetic-video",
            "why": "Review this synthetic video",
            "text": "Synthetic transcript seed",
        },
        separators=(",", ":"),
    ).encode()
    local = SingleUserLocalApplication.open(tmp_path / "brain")
    handler = ShareHttpHandler(
        expected_bearer_token="synthetic-ingress-token",
        capture=local.public_job_sink("JOB-028"),
        clock=lambda: FIXED_TIME,
        body_reader=lambda maximum_bytes, timeout_seconds: body,
    )
    request = HttpRequest(
        method="POST",
        path="/share",
        headers=(
            ("Authorization", "Bearer synthetic-ingress-token"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
        ),
    )

    response = handler.handle(request)

    assert application.argv == (
        "open-brain",
        "capture",
        "serve",
        "--mode=ingress",
        "--bind=CONFIGURED_PRIVATE_BIND",
        "--port=CONFIGURED_PORT",
    )
    assert application.job.state is JobState.ENABLED
    assert application.job.retry is RetryPolicy.ON_FAILURE
    assert application.allowed_writes == frozenset({CaptureWrite.ENGINE_CAPTURE})
    assert application.service_actions == ()
    assert "Wants=network-online.target" in service
    assert "After=network-online.target" in service
    assert "Type=simple" in service
    assert "Restart=on-failure" in service
    assert "ExecStart=open-brain capture serve --mode=ingress" in service
    assert "Environment=OPEN_BRAIN_CONFIG=<OPEN_BRAIN_CONFIG>" in service
    assert "Environment=OPEN_BRAIN_INGRESS_CONFIG=<OPEN_BRAIN_INGRESS_CONFIG>" in service
    assert "Environment=OPEN_BRAIN_INGRESS_TOKEN=<OPEN_BRAIN_INGRESS_TOKEN>" in service
    assert "X-OpenBrain-State=enabled" in service
    assert "[Install]" not in service
    assert "WantedBy=" not in service
    assert response.status == 202
    assert len(local.tasks.inbox.list()) == 1
    assert not tuple(tmp_path.glob("*.md"))
    assert not tuple(tmp_path.glob("*.sqlite"))
