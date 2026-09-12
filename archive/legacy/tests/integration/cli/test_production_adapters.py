from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from open_brain_engine.capture.models import (
    CapturePipeline,
    CaptureWorkItem,
    ShareRequest,
    ShareResponse,
    ShareStatus,
)
from open_brain_engine.core.ports import PutDisposition, PutResult

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy._compat.open_brain.integrations.ports import RetrievalBatch, RetrievalRequest
from open_brain_legacy.cli.production_adapters import (
    ProductionCommandAdapter,
    ProductionCommandDependencies,
)
from open_brain_legacy.operations.status import StatusResult, collect_status


@dataclass
class _Queue:
    items: list[CaptureWorkItem]

    def enqueue(self, item: CaptureWorkItem, *, item_id: str, payload_digest: str) -> PutResult:
        del item_id, payload_digest
        self.items.append(item)
        return PutResult(PutDisposition.CREATED, "record.synthetic-001", "0" * 64)


class _ShareSubmitter:
    def submit(self, request: ShareRequest) -> ShareResponse:
        return ShareResponse.create(
            capture_id="cap_" + "a" * 64,
            pipeline=CapturePipeline.WEB,
            duplicate=False,
            status=ShareStatus.QUEUED,
        )


class _Retriever:
    def __init__(self) -> None:
        self.requests: list[RetrievalRequest] = []

    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        self.requests.append(request)
        return RetrievalBatch(
            retrieval_id="retrieval.synthetic-001",
            hits=(),
            truncated=False,
        )


class _Status:
    def collect(self, *, strict: bool) -> StatusResult:
        return collect_status(probes={}, timeout_seconds=1.0, strict=strict)


def _adapter() -> ProductionCommandAdapter:
    return ProductionCommandAdapter(
        ProductionCommandDependencies(
            capture_queue=_Queue([]),
            clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
            share_submitter=_ShareSubmitter(),
            retriever=_Retriever(),
            status=_Status(),
        )
    )


def test_dispatches_capture_share_query_status_explain_and_registry_without_input_residue() -> None:
    adapter = _adapter()

    capture = adapter.dispatch(("text", "synthetic capture body", "synthetic why"))
    share = adapter.dispatch(("https://example.test/article", "synthetic why"), family="share")
    query = adapter.dispatch(("synthetic work topic",), family="query")
    status = adapter.dispatch((), family="status")
    explain = adapter.dispatch(("no-network",), family="explain")
    registry = adapter.dispatch((), family="registry")

    assert capture.exit_code is ExitCode.SUCCESS
    assert capture.envelope["status"] == "queued"
    assert share.exit_code is ExitCode.SUCCESS
    assert share.envelope["command"] == "share"
    assert query.envelope == {
        "command": "query",
        "retrieval_id": "retrieval.synthetic-001",
        "results": [],
        "status": "ok",
        "truncated": False,
    }
    assert status.envelope["strict"] is False
    assert explain.envelope["network_access"] == "denied"
    assert registry.envelope["command"] == "registry"
    rendered = repr((capture.envelope, share.envelope, query.envelope))
    assert "synthetic capture body" not in rendered
    assert "synthetic why" not in rendered


def test_malformed_and_missing_dependencies_fail_closed_without_deferred_status() -> None:
    missing = ProductionCommandAdapter(ProductionCommandDependencies())

    malformed = missing.dispatch(("text", "only-one-value"))
    unavailable = missing.dispatch(("synthetic work topic",), family="query")

    assert malformed.exit_code is ExitCode.USAGE
    assert malformed.envelope["status"] == "invalid"
    assert unavailable.exit_code is ExitCode.FAILURE
    assert unavailable.envelope == {
        "command": "query",
        "error": {
            "code": "production_dependency_unavailable",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }
    assert "deferred" not in repr(unavailable.envelope)


def test_social_retention_and_delegate_families_never_report_readiness() -> None:
    adapter = _adapter()

    social = adapter.dispatch(("retain",), family="social")

    assert social.exit_code is ExitCode.SUCCESS
    assert social.envelope == {
        "command": "social.compatibility",
        "dry_run": False,
        "status": "ok",
    }
    assert "ready" not in repr(social.envelope).casefold()
