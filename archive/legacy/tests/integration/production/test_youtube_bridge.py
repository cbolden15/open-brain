from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from open_brain_engine.capture.models import (
    CapturePipeline,
    ShareRequest,
    ShareResponse,
    ShareStatus,
)

from open_brain_legacy.production.youtube_bridge import (
    PublicJobShareSubmitter,
    consume_youtube_spool,
)
from open_brain_legacy.services.application import SingleUserLocalApplication


@dataclass
class _Submitter:
    requests: list[ShareRequest] = field(default_factory=list)

    def submit(self, request: ShareRequest) -> ShareResponse:
        self.requests.append(request)
        return ShareResponse.create(
            capture_id="cap_" + "a" * 64,
            pipeline=CapturePipeline.YOUTUBE,
            duplicate=False,
            status=ShareStatus.QUEUED,
        )


def _write_record(path: Path, **overrides: object) -> None:
    value: dict[str, object] = {
        "privacy": "work",
        "schema_version": 1,
        "text": "Locally acquired transcript",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "why": "Saved to the YouTube transcript playlist.",
    }
    value.update(overrides)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_consumer_submits_supplied_transcript_and_removes_durable_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "youtube"
    root.mkdir()
    record = root / "dQw4w9WgXcQ.json"
    _write_record(record)
    submitter = _Submitter()

    result = consume_youtube_spool(root, submitter=submitter)

    assert result.processed == 1
    assert result.failed == 0
    assert not record.exists()
    assert len(submitter.requests) == 1
    assert submitter.requests[0].text == "Locally acquired transcript"
    assert submitter.requests[0].privacy_tier.value == "work"


def test_consumer_leaves_invalid_record_for_recovery(tmp_path: Path) -> None:
    root = tmp_path / "youtube"
    root.mkdir()
    record = root / "invalid.json"
    _write_record(record, schema_version=2)
    submitter = _Submitter()

    result = consume_youtube_spool(root, submitter=submitter)

    assert result.processed == 0
    assert result.failed == 1
    assert record.exists()
    assert submitter.requests == []


def test_public_youtube_submitter_uses_the_injected_job_sink(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    submitter = PublicJobShareSubmitter(application.public_job_sink("JOB-029"))

    response = submitter.submit(
        ShareRequest.create(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            why="Synthetic retained reason",
            text="Synthetic retained transcript",
            privacy_tier="work",
        )
    )

    assert response.capture_id.startswith("capture_")
    assert [item.capture_id for item in application.tasks.inbox.list()] == [response.capture_id]
