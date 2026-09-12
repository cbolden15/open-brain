from __future__ import annotations

from open_brain_engine.capture.models import (
    CapturePipeline,
    ShareRequest,
    ShareResponse,
    ShareStatus,
)

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.capture import share_capture


class ShareSubmitterFake:
    def __init__(self) -> None:
        self.requests: list[ShareRequest] = []

    def submit(self, request: ShareRequest) -> ShareResponse:
        self.requests.append(request)
        return ShareResponse.create(
            capture_id="cap_" + "a" * 64,
            pipeline=CapturePipeline.WEB,
            duplicate=True,
            status=ShareStatus.DUPLICATE,
        )


def test_share_returns_only_opaque_status_and_duplicate_fields() -> None:
    submitter = ShareSubmitterFake()

    result = share_capture(
        submitter=submitter,
        url="HTTPS://Example.Test/article",
        why="Review this synthetic article",
        text="Synthetic third-party text",
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "capture_id": "cap_" + "a" * 64,
        "command": "share",
        "dry_run": False,
        "duplicate": True,
        "pipeline": "web",
        "status": "duplicate",
    }
    assert submitter.requests == [
        ShareRequest.create(
            url="https://example.test/article",
            why="Review this synthetic article",
            text="Synthetic third-party text",
        )
    ]
    assert "Synthetic third-party text" not in result.to_json()
    assert "Review this synthetic article" not in result.to_json()


def test_share_dry_run_validates_but_never_submits() -> None:
    submitter = ShareSubmitterFake()

    result = share_capture(
        submitter=submitter,
        url="https://example.test/article",
        why="Review this synthetic article",
        dry_run=True,
    )

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "command": "share",
        "dry_run": True,
        "status": "planned",
    }
    assert submitter.requests == []
