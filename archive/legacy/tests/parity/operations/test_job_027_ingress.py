from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from open_brain_legacy._compat.open_brain.capture.http import HttpRequest, ShareHttpHandler
from open_brain_legacy.operations.capture_jobs import CaptureWrite, get_capture_job
from open_brain_legacy.operations.models import JobState, TriggerKind
from open_brain_legacy.services.application import SingleUserLocalApplication

FIXED_TIME = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
TOKEN = "synthetic-bearer-token"


def _request(body: bytes, *, token: str | None, path: str = "/share") -> HttpRequest:
    headers = [
        ("Content-Length", str(len(body))),
        ("Content-Type", "application/json"),
    ]
    if token is not None:
        headers.append(("Authorization", "Bearer " + token))
    return HttpRequest(method="POST", path=path, headers=tuple(headers))


def test_job_027_is_enabled_safe_bind_queue_only_ingress(tmp_path: Path) -> None:
    application = get_capture_job("JOB-027")
    body = json.dumps(
        {
            "url": "https://example.test/synthetic-share",
            "why": "Retain this synthetic reference",
            "text": "Synthetic shared body",
        },
        separators=(",", ":"),
    ).encode()
    local = SingleUserLocalApplication.open(tmp_path / "brain")
    handler = ShareHttpHandler(
        expected_bearer_token=TOKEN,
        capture=local.public_job_sink("JOB-027"),
        clock=lambda: FIXED_TIME,
        body_reader=lambda maximum_bytes, timeout_seconds: body,
    )

    unauthorized = handler.handle(_request(body, token=None))
    created = handler.handle(_request(body, token=TOKEN))
    duplicate = handler.handle(_request(body, token=TOKEN))

    assert application.argv == (
        "open-brain",
        "capture",
        "serve",
        "--bind=CONFIGURED_PRIVATE_BIND",
        "--port=CONFIGURED_PORT",
    )
    assert application.job.state is JobState.ENABLED
    assert application.job.trigger.kind is TriggerKind.KEEPALIVE
    assert application.job.env_refs == (
        "OPEN_BRAIN_CONFIG",
        "OPEN_BRAIN_INGRESS_CONFIG",
        "OPEN_BRAIN_INGRESS_TOKEN",
    )
    assert application.allowed_writes == frozenset({CaptureWrite.ENGINE_CAPTURE})
    assert application.service_actions == ()
    assert (unauthorized.status, unauthorized.body) == (401, b'{"code":"unauthorized"}')
    assert created.status == duplicate.status == 202
    assert json.loads(created.body)["status"] == "queued"
    assert json.loads(duplicate.body)["status"] == "duplicate"
    assert len(local.tasks.inbox.list()) == 1
    assert TOKEN.encode() not in created.body + duplicate.body
    assert b"Synthetic shared body" not in created.body + duplicate.body


def test_job_027_does_not_claim_a_health_surface(tmp_path: Path) -> None:
    body = b"{}"
    local = SingleUserLocalApplication.open(tmp_path / "brain")
    handler = ShareHttpHandler(
        expected_bearer_token=TOKEN,
        capture=local.public_job_sink("JOB-027"),
        clock=lambda: FIXED_TIME,
        body_reader=lambda maximum_bytes, timeout_seconds: body,
    )

    response = handler.handle(_request(body, token=TOKEN, path="/health"))

    assert (response.status, response.body) == (400, b'{"code":"invalid_request"}')
    assert local.tasks.inbox.list() == ()
