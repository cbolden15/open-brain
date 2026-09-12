from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from open_brain_engine.engine import CaptureReceipt

from open_brain.integrations.ports import (
    FeedbackOutcome,
    PageDocument,
    PageReadRequest,
    RedactedText,
    RetrievalBatch,
    RetrievalFeedbackReceipt,
    RetrievalFeedbackRequest,
    RetrievalRequest,
    TrustLabel,
)
from open_brain.services.composition import (
    ProductionServiceDependencies,
    ProductionServices,
    compose_production_services,
)
from open_brain.services.http_server import HttpRouteMode


class _Retriever:
    def __init__(self) -> None:
        self.requests: list[RetrievalRequest] = []

    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        self.requests.append(request)
        return RetrievalBatch("retrieval.synthetic-001", (), False)


class _Feedback:
    def __init__(self) -> None:
        self.requests: list[RetrievalFeedbackRequest] = []

    def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt:
        self.requests.append(request)
        return RetrievalFeedbackReceipt(
            request.retrieval_id,
            request.outcome,
            len(request.result_ids),
        )


class _Pages:
    def read(self, request: PageReadRequest) -> PageDocument | None:
        return PageDocument(
            page_id=request.page_id,
            title=RedactedText.redact("Synthetic work page"),
            markdown=RedactedText.redact("Safe page content"),
            trust=TrustLabel.VERIFIED_WORK,
        )


@dataclass
class _Capture:
    accepted: int = 0

    def accept(self, payload: object, **kwargs: object) -> CaptureReceipt:
        del payload, kwargs
        self.accepted += 1
        return CaptureReceipt(
            capture_id="capture_synthetic",
            payload_family="reference_or_file",
            state="accepted",
            enrichment_state="unavailable",
            space_id=None,
            canonical_path=None,
        )


@dataclass
class _Server:
    served: bool = False
    closed: bool = False

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        del poll_interval
        self.served = True

    def shutdown(self) -> None:
        return None

    def server_close(self) -> None:
        self.closed = True


def _services(
    route_mode: HttpRouteMode = HttpRouteMode.COMBINED,
) -> tuple[ProductionServices, _Retriever, _Feedback, _Capture]:
    retriever = _Retriever()
    feedback = _Feedback()
    capture = _Capture()
    services = compose_production_services(
        ProductionServiceDependencies(
            retriever=retriever,
            feedback=feedback,
            page_reader=_Pages(),
            capture=capture,
            expected_bearer_token="synthetic-test-token",
            clock=lambda: datetime(2026, 8, 25, tzinfo=UTC),
            http_route_mode=route_mode,
        )
    )
    return services, retriever, feedback, capture


def test_composition_exposes_unstarted_work_only_mcp_and_lifecycle() -> None:
    services, retriever, feedback, _ = _services()
    incoming = io.BytesIO(
        b"\n".join(
            json.dumps(message).encode("utf-8")
            for message in (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "brain_query", "arguments": {"question": "synthetic topic"}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "brain_retrieval_feedback",
                        "arguments": {
                            "retrieval_id": "retrieval.synthetic-001",
                            "outcome": "cited",
                        },
                    },
                },
            )
        )
        + b"\n"
    )
    outgoing = io.BytesIO()

    services.mcp.serve(input_stream=incoming, output_stream=outgoing)
    server = services.http.start(
        server_factory=lambda _address, _handler: _Server()
    )

    assert retriever.requests == [RetrievalRequest(question="synthetic topic")]
    assert feedback.requests == [
        RetrievalFeedbackRequest("retrieval.synthetic-001", FeedbackOutcome.CITED)
    ]
    assert b"personal" not in outgoing.getvalue().lower()
    assert isinstance(server, _Server) is False


def test_http_service_authenticates_ui_and_share_without_secret_residue() -> None:
    services, _, _, capture = _services()

    unauthenticated = services.http.dispatch(
        method="GET", path="/pages/page.synthetic-001", headers=(), body_reader=lambda _a, _b: b""
    )
    authenticated = services.http.dispatch(
        method="GET",
        path="/pages/page.synthetic-001",
        headers=(("Authorization", "Bearer synthetic-test-token"),),
        body_reader=lambda _a, _b: b"",
    )
    payload = b'{"url":"https://example.test/article","why":"synthetic reason"}'
    shared = services.http.dispatch(
        method="POST",
        path="/share",
        headers=(
            ("Authorization", "Bearer synthetic-test-token"),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(payload))),
        ),
        body_reader=lambda _a, _b: payload,
    )

    assert unauthenticated.status == 401
    assert authenticated.status == 200
    assert shared.status == 202
    assert capture.accepted == 1
    rendered = repr((authenticated, shared))
    assert "synthetic-test-token" not in rendered
    assert "synthetic reason" not in rendered


def test_http_route_modes_keep_ui_read_only_and_share_queue_only() -> None:
    ui_services, _, _, ui_capture = _services(HttpRouteMode.UI_ONLY)
    share_services, _, _, share_capture = _services(HttpRouteMode.SHARE_ONLY)
    payload = b'{"url":"https://example.test/article","why":"synthetic reason"}'
    headers = (
        ("Authorization", "Bearer synthetic-test-token"),
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(payload))),
    )

    blocked_share = ui_services.http.dispatch(
        method="POST",
        path="/share",
        headers=headers,
        body_reader=lambda _a, _b: payload,
    )
    blocked_ui = share_services.http.dispatch(
        method="GET",
        path="/health",
        headers=(("Authorization", "Bearer synthetic-test-token"),),
        body_reader=lambda _a, _b: b"",
    )
    accepted_share = share_services.http.dispatch(
        method="POST",
        path="/share",
        headers=headers,
        body_reader=lambda _a, _b: payload,
    )

    assert blocked_share.status == blocked_ui.status == 405
    assert accepted_share.status == 202
    assert ui_capture.accepted == 0
    assert share_capture.accepted == 1


def test_composition_rejects_missing_concrete_dependencies() -> None:
    try:
        compose_production_services(ProductionServiceDependencies())
    except ValueError as error:
        assert str(error) == "invalid production service dependencies"
    else:
        raise AssertionError("expected missing dependencies to fail closed")
