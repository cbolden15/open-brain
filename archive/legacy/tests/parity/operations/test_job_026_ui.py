import json

from open_brain_legacy._compat.open_brain.integrations.ports import (
    PageDocument,
    PageReadRequest,
    RedactedText,
    TrustLabel,
)
from open_brain_legacy._compat.open_brain.integrations.ui import UiBindConfig, UiHandler, UiRequest
from open_brain_legacy.operations.models import HostRole, JobState, WriterScope
from open_brain_legacy.operations.optional_jobs import compose_ui_job


class SyntheticPageReader:
    def read(self, request: PageReadRequest) -> PageDocument | None:
        return PageDocument(
            page_id=request.page_id,
            title=RedactedText.redact("Synthetic page"),
            markdown=RedactedText.redact("<script>alert('fixture')</script>"),
            trust=TrustLabel.VERIFIED_WORK,
        )


def _request(method: str, path: str) -> UiRequest:
    return UiRequest(
        method=method,
        path=path,
        headers=(("Authorization", "Bearer synthetic-ui-token"),),
    )


def test_job_026_is_enabled_loopback_only_and_read_only() -> None:
    job = compose_ui_job(UiBindConfig())

    assert job.state is JobState.ENABLED
    assert job.host_role is HostRole.SERVICE
    assert job.writer_scope is WriterScope.NONE
    assert job.command == (
        "open-brain",
        "ui",
        "serve",
        "--bind=" + "127.0.0.1",
        "--port=8788",
    )

    handler = UiHandler(
        expected_bearer_token="synthetic-ui-token",
        page_reader=SyntheticPageReader(),
    )
    health = handler.handle(_request("GET", "/health"))
    denied = handler.handle(_request("POST", "/pages/page.fixture"))
    page = handler.handle(_request("GET", "/pages/page.fixture"))

    assert json.loads(health.body) == {"status": "ok"}
    assert denied.status == 405
    assert page.status == 200
    assert b"<script>" not in page.body
    assert b"[redacted]" in page.body
