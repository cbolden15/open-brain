from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.engine import CaptureReceipt, open_local_engine

from open_brain.capture.http import BodyReader, RequestReadTimeout, ShareHttpHandler
from open_brain.integrations.mcp import LocalStdioMcpAdapter
from open_brain.integrations.phase1_ui import BrowserSessionStore, Phase1UiHandler
from open_brain.integrations.ports import (
    FeedbackOutcome,
    PageDocument,
    PageReadRequest,
    RedactedText,
    RetrievalBatch,
    RetrievalFeedbackReceipt,
    RetrievalFeedbackRequest,
    RetrievalHit,
    RetrievalRequest,
    TrustLabel,
)
from open_brain.integrations.ui import UiHandler
from open_brain.profile import compile_single_user_local
from open_brain.services.appliance_auth import (
    ApplianceBrowserSessionStore,
    derive_appliance_credential,
)
from open_brain.services.http_server import (
    BoundedBodyReader,
    HttpService,
    HttpServiceConfig,
    ShareHandlerFactory,
    create_http_server,
)
from open_brain.services.mcp_stdio import serve_stdio_mcp


class _Retriever:
    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        raise AssertionError(f"unexpected retrieval: {request!r}")


class _Feedback:
    def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt:
        raise AssertionError(f"unexpected feedback: {request!r}")


def test_stdio_mcp_initializes_over_json_rpc_without_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = LocalStdioMcpAdapter(retriever=_Retriever(), feedback=_Feedback())
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
    }
    incoming = io.BytesIO(json.dumps(request).encode("utf-8") + b"\n")
    outgoing = io.BytesIO()

    serve_stdio_mcp(adapter, input_stream=incoming, output_stream=outgoing)

    response = json.loads(outgoing.getvalue())
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    assert response["result"]["capabilities"] == {"tools": {}}
    assert response["result"]["serverInfo"]["name"] == "open-brain"
    assert capsys.readouterr().err == ""


def test_stdio_mcp_accepts_supported_client_protocol_versions() -> None:
    adapter = LocalStdioMcpAdapter(retriever=_Retriever(), feedback=_Feedback())
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}},
    }
    outgoing = io.BytesIO()

    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(json.dumps(request).encode("utf-8") + b"\n"),
        output_stream=outgoing,
    )

    assert json.loads(outgoing.getvalue())["result"]["protocolVersion"] == "2024-11-05"


class _RecordingRetriever:
    def __init__(self) -> None:
        self.requests: list[RetrievalRequest] = []

    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        self.requests.append(request)
        return RetrievalBatch(
            retrieval_id="retrieval.synthetic-001",
            hits=(
                RetrievalHit(
                    result_id="result.synthetic-001",
                    rank=1,
                    title=RedactedText.redact("Synthetic work result"),
                    excerpt=RedactedText.redact("Safe synthetic excerpt"),
                    trust=TrustLabel.VERIFIED_WORK,
                ),
            ),
            truncated=False,
        )


class _RecordingFeedback:
    def __init__(self) -> None:
        self.requests: list[RetrievalFeedbackRequest] = []

    def record(self, request: RetrievalFeedbackRequest) -> RetrievalFeedbackReceipt:
        self.requests.append(request)
        return RetrievalFeedbackReceipt(
            retrieval_id=request.retrieval_id,
            outcome=request.outcome,
            result_count=len(request.result_ids),
        )


class _NoPageReader:
    def read(self, request: PageReadRequest) -> PageDocument | None:
        del request
        return None


def _mcp_responses(
    *messages: object, maximum_message_bytes: int = 65_536
) -> list[dict[str, object]]:
    adapter = LocalStdioMcpAdapter(
        retriever=_RecordingRetriever(), feedback=_RecordingFeedback()
    )
    incoming = io.BytesIO(
        b"".join(
            message if isinstance(message, bytes) else json.dumps(message).encode("utf-8") + b"\n"
            for message in messages
        )
    )
    outgoing = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=incoming,
        output_stream=outgoing,
        maximum_message_bytes=maximum_message_bytes,
    )
    return [cast(dict[str, object], json.loads(line)) for line in outgoing.getvalue().splitlines()]


def test_stdio_mcp_lists_and_calls_only_bounded_work_tools() -> None:
    retriever = _RecordingRetriever()
    feedback = _RecordingFeedback()
    adapter = LocalStdioMcpAdapter(retriever=retriever, feedback=feedback)
    requests = (
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": "query",
            "method": "tools/call",
            "params": {"name": "brain_query", "arguments": {"question": "synthetic work topic"}},
        },
        {
            "jsonrpc": "2.0",
            "id": "feedback",
            "method": "tools/call",
            "params": {
                "name": "brain_retrieval_feedback",
                "arguments": {"retrieval_id": "retrieval.synthetic-001", "outcome": "cited"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "unknown-tool",
            "method": "tools/call",
            "params": {"name": "personal_query", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": "unknown-method", "method": "resources/list"},
    )
    incoming = io.BytesIO(b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in requests))
    outgoing = io.BytesIO()

    serve_stdio_mcp(adapter, input_stream=incoming, output_stream=outgoing)

    responses = [
        cast(dict[str, object], json.loads(line)) for line in outgoing.getvalue().splitlines()
    ]
    assert [response["id"] for response in responses] == [
        "init",
        "list",
        "query",
        "feedback",
        "unknown-tool",
        "unknown-method",
    ]
    listed = cast(dict[str, object], responses[1]["result"])
    tools = cast(list[dict[str, object]], listed["tools"])
    assert {tool["name"] for tool in tools} == {"brain_query", "brain_retrieval_feedback"}
    assert all("personal" not in repr(tool).casefold() for tool in tools)
    query_schema = next(tool["inputSchema"] for tool in tools if tool["name"] == "brain_query")
    feedback_schema = next(
        tool["inputSchema"] for tool in tools if tool["name"] == "brain_retrieval_feedback"
    )
    assert cast(dict[str, object], query_schema)["required"] == ["question"]
    feedback_properties = cast(
        dict[str, object], cast(dict[str, object], feedback_schema)["properties"]
    )
    assert set(feedback_properties) == {
        "retrieval_id",
        "outcome",
        "result_ids",
    }
    query_result = cast(dict[str, object], responses[2]["result"])
    assert cast(dict[str, object], query_result["structuredContent"])["scope"] == "work"
    assert retriever.requests == [RetrievalRequest(question="synthetic work topic")]
    assert feedback.requests == [
        RetrievalFeedbackRequest(
            retrieval_id="retrieval.synthetic-001",
            outcome=FeedbackOutcome.CITED,
            result_ids=(),
        )
    ]
    unknown_tool = cast(dict[str, object], responses[4]["result"])
    assert unknown_tool["isError"] is True
    assert unknown_tool["content"] == [{"type": "text", "text": "unknown tool"}]
    unknown_method = cast(dict[str, object], responses[5]["error"])
    assert unknown_method == {"code": -32601, "message": "method not found"}


def test_stdio_mcp_rejects_malformed_and_oversized_messages_and_stops_on_eof() -> None:
    initial = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
    }
    follow_up = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    responses = _mcp_responses(
        b'{"jsonrpc":"2.0","id":"synthetic-private-marker",\n',
        initial,
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
        b"x" * 1_025 + b"\n",
        follow_up,
        maximum_message_bytes=1_024,
    )

    assert [response["id"] for response in responses] == [None, 1, None, 2]
    assert cast(dict[str, object], responses[0]["error"])["code"] == -32700
    assert cast(dict[str, object], responses[2]["error"])["code"] == -32600
    assert responses[3]["id"] == 2
    assert b"synthetic-private-marker" not in repr(responses).encode("utf-8")


class _LargeResultRetriever:
    def search(self, request: RetrievalRequest) -> RetrievalBatch:
        del request
        return RetrievalBatch(
            retrieval_id="retrieval.large-001",
            hits=(
                RetrievalHit(
                    result_id="result.large-001",
                    rank=1,
                    title=RedactedText.redact("Synthetic title"),
                    excerpt=RedactedText.redact("x" * 4_096),
                    trust=TrustLabel.VERIFIED_WORK,
                ),
            ),
            truncated=False,
        )


def test_stdio_mcp_replaces_oversized_output_with_a_safe_bounded_error() -> None:
    adapter = LocalStdioMcpAdapter(retriever=_LargeResultRetriever(), feedback=_RecordingFeedback())
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
                    "params": {"name": "brain_query", "arguments": {"question": "synthetic"}},
                },
            )
        )
        + b"\n"
    )
    outgoing = io.BytesIO()

    serve_stdio_mcp(
        adapter,
        input_stream=incoming,
        output_stream=outgoing,
        maximum_message_bytes=1_024,
    )

    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert responses[1] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32603, "message": "response too large"},
    }


class _Capture:
    def __init__(self) -> None:
        self.accepted = 0

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
            duplicate=False,
        )


class _BodyReader:
    def __init__(self, body: bytes, *, failure: Exception | None = None) -> None:
        self.body = body
        self.failure = failure
        self.calls: list[tuple[int, float]] = []

    def __call__(self, maximum_bytes: int, timeout_seconds: float) -> bytes:
        self.calls.append((maximum_bytes, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        return self.body


def _share_factory(capture: _Capture) -> ShareHandlerFactory:
    def create(body_reader: BodyReader) -> ShareHttpHandler:
        return ShareHttpHandler(
            expected_bearer_token="synthetic-token",
            capture=capture,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            body_reader=body_reader,
        )

    return create


class _EmptyPageReader:
    def read(self, request: PageReadRequest) -> PageDocument | None:
        del request
        return None


class _OversizedPageReader:
    def read(self, request: PageReadRequest) -> PageDocument:
        return PageDocument(
            page_id=request.page_id,
            title=RedactedText.redact("Synthetic title"),
            markdown=RedactedText.redact("x" * 2_048),
            trust=TrustLabel.VERIFIED_WORK,
        )


def _headers(body: bytes) -> tuple[tuple[str, str], ...]:
    return (
        ("Authorization", "Bearer synthetic-token"),
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    )


def test_http_service_routes_only_authenticated_ui_and_share_handlers_with_limits() -> None:
    capture = _Capture()
    ui_handler = UiHandler(
        expected_bearer_token="synthetic-token", page_reader=_EmptyPageReader()
    )
    service = HttpService(
        ui_handler=ui_handler,
        share_handler_factory=_share_factory(capture),
        config=HttpServiceConfig(maximum_header_bytes=1_024, maximum_body_bytes=1_024),
    )
    body = json.dumps(
        {"url": "https://example.test/synthetic", "why": "Synthetic reason", "text": "Synthetic"}
    ).encode("utf-8")
    body_reader = _BodyReader(body)

    health = service.dispatch(
        method="GET",
        path="/health",
        headers=(("Authorization", "Bearer synthetic-token"),),
        body_reader=body_reader,
    )
    shared = service.dispatch(
        method="POST", path="/share", headers=_headers(body), body_reader=body_reader
    )
    unauthorized = service.dispatch(
        method="GET", path="/health", headers=(), body_reader=body_reader
    )
    invalid_method = service.dispatch(
        method="PUT", path="/share", headers=(), body_reader=body_reader
    )
    oversized_headers = service.dispatch(
        method="GET",
        path="/health",
        headers=(("Authorization", "Bearer " + "x" * 1_100),),
        body_reader=body_reader,
    )
    oversized_body = service.dispatch(
        method="POST",
        path="/share",
        headers=(
            ("Authorization", "Bearer synthetic-token"),
            ("Content-Type", "application/json"),
            ("Content-Length", "1025"),
        ),
        body_reader=body_reader,
    )
    timed_out = service.dispatch(
        method="POST",
        path="/share",
        headers=_headers(body),
        body_reader=_BodyReader(body, failure=RequestReadTimeout()),
    )
    unauthenticated_share = service.dispatch(
        method="POST",
        path="/share",
        headers=(
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ),
        body_reader=body_reader,
    )
    wrong_path = service.dispatch(
        method="POST", path="/not-share", headers=_headers(body), body_reader=body_reader
    )

    assert (health.status, json.loads(health.body)) == (200, {"status": "ok"})
    assert shared.status == 202
    assert capture.accepted == 1
    assert (unauthorized.status, unauthorized.body) == (401, b"unauthorized")
    assert (invalid_method.status, invalid_method.body) == (405, b"method_not_allowed")
    assert oversized_headers.body == b"invalid_request"
    assert (oversized_body.status, oversized_body.body) == (413, b'{"code":"request_too_large"}')
    assert (timed_out.status, timed_out.body) == (408, b'{"code":"request_timeout"}')
    assert (unauthenticated_share.status, unauthenticated_share.body) == (
        401,
        b'{"code":"unauthorized"}',
    )
    assert (wrong_path.status, wrong_path.body) == (404, b"not_found")


def test_http_service_routes_browser_posts_to_the_ui_handler_before_share(tmp_path: Path) -> None:
    engine = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    browser = derive_appliance_credential("synthetic-owner-seed", purpose="browser-bootstrap")
    sessions = ApplianceBrowserSessionStore(expected_bootstrap_credential=browser)
    share_capture = _Capture()
    service = HttpService(
        ui_handler=Phase1UiHandler(
            tasks=engine.phase1,
            browser_sessions=cast(BrowserSessionStore, sessions),
            allowed_origin="http://127.0.0.1:8788",
            page_reader=_NoPageReader(),
            status_reader=lambda: {"status": "ok"},
            history_reader=lambda _limit: {"runs": [], "status": "ok", "truncated": False},
        ),
        share_handler_factory=_share_factory(share_capture),
        config=HttpServiceConfig(maximum_header_bytes=1_024, maximum_body_bytes=1_024),
    )
    login_body = canonical_json_bytes({"credential": browser})
    login = service.dispatch(
        method="POST",
        path="/auth/login",
        headers=(
            ("Content-Length", str(len(login_body))),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
        ),
        body_reader=_BodyReader(login_body),
    )
    cookie = dict(login.headers)["Set-Cookie"].split(";", 1)[0]
    csrf_token = cast(dict[str, object], json.loads(login.body))["csrf_token"]
    assert isinstance(csrf_token, str)

    skipped_reader = _BodyReader(
        canonical_json_bytes({"delivery_id": "bad.browser.route", "name": "Blocked"})
    )
    blocked = service.dispatch(
        method="POST",
        path="/api/spaces",
        headers=(
            ("Authorization", "Bearer synthetic-token"),
            ("Content-Length", "52"),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
        ),
        body_reader=skipped_reader,
    )
    created = service.dispatch(
        method="POST",
        path="/api/spaces",
        headers=(
            ("Cookie", cookie),
            ("Content-Length", "53"),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
            ("X-CSRF-Token", csrf_token),
        ),
        body_reader=_BodyReader(
            canonical_json_bytes({"delivery_id": "good.browser.route", "name": "Browser"})
        ),
    )

    assert login.status == 200
    assert blocked.status == 401
    assert skipped_reader.calls == []
    assert created.status == 200
    assert share_capture.accepted == 0


def test_http_service_rejects_unknown_and_cross_capability_browser_posts_before_body_read(
    tmp_path: Path,
) -> None:
    engine = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    browser = derive_appliance_credential("synthetic-owner-seed", purpose="browser-bootstrap")
    sessions = ApplianceBrowserSessionStore(expected_bootstrap_credential=browser)
    share_capture = _Capture()
    service = HttpService(
        ui_handler=Phase1UiHandler(
            tasks=engine.phase1,
            browser_sessions=cast(BrowserSessionStore, sessions),
            allowed_origin="http://127.0.0.1:8788",
            page_reader=_NoPageReader(),
            status_reader=lambda: {"status": "ok"},
            history_reader=lambda _limit: {"runs": [], "status": "ok", "truncated": False},
        ),
        share_handler_factory=_share_factory(share_capture),
        config=HttpServiceConfig(maximum_header_bytes=1_024, maximum_body_bytes=1_024),
    )
    login_body = canonical_json_bytes({"credential": browser})
    login = service.dispatch(
        method="POST",
        path="/auth/login",
        headers=(
            ("Content-Length", str(len(login_body))),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
        ),
        body_reader=_BodyReader(login_body),
    )
    cookie = dict(login.headers)["Set-Cookie"].split(";", 1)[0]
    csrf_token = cast(dict[str, object], json.loads(login.body))["csrf_token"]
    assert isinstance(csrf_token, str)

    unknown_reader = _BodyReader(canonical_json_bytes({"delivery_id": "bad.unknown"}))
    malformed_reader = _BodyReader(canonical_json_bytes({"delivery_id": "bad.malformed"}))
    known_dynamic_reader = _BodyReader(b"{}")
    share_reader = _BodyReader(
        canonical_json_bytes({"url": "https://example.test/shared", "why": "browser only"})
    )

    unknown = service.dispatch(
        method="POST",
        path="/api/not-a-route",
        headers=(
            ("Cookie", cookie),
            ("Content-Length", "29"),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
            ("X-CSRF-Token", csrf_token),
        ),
        body_reader=unknown_reader,
    )
    malformed = service.dispatch(
        method="POST",
        path="/api/captures/capture_synthetic/route/extra",
        headers=(
            ("Cookie", cookie),
            ("Content-Length", "31"),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
            ("X-CSRF-Token", csrf_token),
        ),
        body_reader=malformed_reader,
    )
    known_dynamic = service.dispatch(
        method="POST",
        path="/api/proposals/proposal_synthetic/decision",
        headers=(
            ("Cookie", cookie),
            ("Content-Length", "2"),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
            ("X-CSRF-Token", csrf_token),
        ),
        body_reader=known_dynamic_reader,
    )
    wrong_capability = service.dispatch(
        method="POST",
        path="/share",
        headers=(
            ("Cookie", cookie),
            ("Content-Length", str(len(share_reader.body))),
            ("Content-Type", "application/json"),
            ("Origin", "http://127.0.0.1:8788"),
            ("X-CSRF-Token", csrf_token),
        ),
        body_reader=share_reader,
    )

    assert unknown.status == 404
    assert malformed.status == 404
    assert known_dynamic.status == 409
    assert wrong_capability.status == 401
    assert unknown_reader.calls == []
    assert malformed_reader.calls == []
    assert known_dynamic_reader.calls != []
    assert share_reader.calls == []
    assert share_capture.accepted == 0
    assert engine.phase1.inbox.list() == ()


def test_http_service_rejects_oversized_response_during_direct_dispatch() -> None:
    service = HttpService(
        ui_handler=UiHandler(
            expected_bearer_token="synthetic-token",
            page_reader=_OversizedPageReader(),
        ),
        share_handler_factory=_share_factory(_Capture()),
        config=HttpServiceConfig(maximum_response_bytes=1_024),
    )

    response = service.dispatch(
        method="GET",
        path="/pages/synthetic-page",
        headers=(("Authorization", "Bearer synthetic-token"),),
        body_reader=_BodyReader(b""),
    )

    assert (response.status, response.body) == (503, b"service_unavailable")


def test_bounded_body_reader_enforces_the_injected_time_and_size_limits() -> None:
    timeout_calls: list[float] = []
    read_calls: list[int] = []
    def read_body(size: int) -> bytes:
        read_calls.append(size)
        return b"body"

    reader = BoundedBodyReader(
        content_length=4,
        maximum_body_bytes=8,
        maximum_timeout_seconds=0.25,
        read=read_body,
        set_timeout=lambda value: timeout_calls.append(value),
    )
    oversized = BoundedBodyReader(
        content_length=9,
        maximum_body_bytes=8,
        maximum_timeout_seconds=0.25,
        read=lambda size: (_ for _ in ()).throw(AssertionError(size)),
        set_timeout=lambda value: (_ for _ in ()).throw(AssertionError(value)),
    )
    timed_out = BoundedBodyReader(
        content_length=4,
        maximum_body_bytes=8,
        maximum_timeout_seconds=0.25,
        read=lambda size: (_ for _ in ()).throw(TimeoutError(size)),
        set_timeout=lambda value: None,
    )

    assert reader(100, 5.0) == b"body"
    assert (timeout_calls, read_calls) == ([0.25], [4])
    assert oversized(100, 5.0) == b""
    with pytest.raises(RequestReadTimeout):
        timed_out(100, 5.0)


class _FakeHttpServer:
    def __init__(self) -> None:
        self.events: list[str] = []

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        assert poll_interval == 0.5
        self.events.append("serve")

    def shutdown(self) -> None:
        self.events.append("shutdown")

    def server_close(self) -> None:
        self.events.append("close")


def test_http_server_lifecycle_is_injectable_and_closes_without_a_listener() -> None:
    capture = _Capture()
    service = HttpService(
        ui_handler=UiHandler(
            expected_bearer_token="synthetic-token", page_reader=_EmptyPageReader()
        ),
        share_handler_factory=_share_factory(capture),
    )
    fake_server = _FakeHttpServer()
    observed: list[tuple[tuple[str, int], type[BaseHTTPRequestHandler]]] = []

    def server_factory(
        address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]
    ) -> _FakeHttpServer:
        observed.append((address, handler_class))
        return fake_server

    server = create_http_server(service, server_factory=server_factory)
    server.serve_forever()

    assert observed[0][0] == ("127.0.0.1", 8788)
    assert fake_server.events == ["serve", "close"]

    shutdown_server = _FakeHttpServer()
    shutdown = create_http_server(
        service,
        server_factory=lambda address, handler_class: shutdown_server,
    )
    shutdown.shutdown()
    assert shutdown_server.events == ["shutdown", "close"]
