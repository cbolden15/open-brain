"""Bounded stdlib HTTP transport composed from safe existing handlers."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol, runtime_checkable

from open_brain.capture.http import (
    BODY_LIMIT_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    BodyReader,
    HttpRequest,
    RequestReadTimeout,
    ShareHttpHandler,
)
from open_brain.integrations.phase1_ui import Phase1UiHandler, Phase1UiRequest
from open_brain.integrations.ui import UiBindConfig, UiHandler, UiRequest

DEFAULT_MAXIMUM_HEADER_BYTES = 8_192
DEFAULT_MAXIMUM_RESPONSE_BYTES = 131_072
_MAXIMUM_HEADER_COUNT = 64


class HttpRouteMode(StrEnum):
    """Closed HTTP capability sets for read-only UI and queue-only share services."""

    UI_ONLY = "ui-only"
    SHARE_ONLY = "share-only"
    COMBINED = "combined"


@dataclass(frozen=True, slots=True)
class HttpServiceConfig:
    """Closed transport bounds for the UI and share HTTP handlers."""

    bind: UiBindConfig = field(default_factory=UiBindConfig)
    maximum_header_bytes: int = DEFAULT_MAXIMUM_HEADER_BYTES
    maximum_body_bytes: int = BODY_LIMIT_BYTES
    maximum_response_bytes: int = DEFAULT_MAXIMUM_RESPONSE_BYTES
    request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    route_mode: HttpRouteMode = HttpRouteMode.COMBINED

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bind, UiBindConfig)
            or type(self.maximum_header_bytes) is not int
            or not 1_024 <= self.maximum_header_bytes <= 65_536
            or type(self.maximum_body_bytes) is not int
            or not 1 <= self.maximum_body_bytes <= BODY_LIMIT_BYTES
            or type(self.maximum_response_bytes) is not int
            or not 1_024 <= self.maximum_response_bytes <= 262_144
            or type(self.request_timeout_seconds) is not float
            or not 0.1 <= self.request_timeout_seconds <= REQUEST_TIMEOUT_SECONDS
            or not isinstance(self.route_mode, HttpRouteMode)
        ):
            raise ValueError("invalid HTTP service configuration")


@dataclass(frozen=True, slots=True)
class ServiceResponse:
    """Transport-neutral HTTP response copied from an existing safe handler."""

    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]


class BoundedBodyReader:
    """Read one declared HTTP body through injected stream and timeout operations."""

    def __init__(
        self,
        *,
        content_length: int | None,
        maximum_body_bytes: int,
        maximum_timeout_seconds: float,
        read: Callable[[int], bytes],
        set_timeout: Callable[[float], object],
    ) -> None:
        self._content_length = content_length
        self._maximum_body_bytes = maximum_body_bytes
        self._maximum_timeout_seconds = maximum_timeout_seconds
        self._read = read
        self._set_timeout = set_timeout

    def __call__(self, maximum_bytes: int, timeout_seconds: float) -> bytes:
        content_length = self._content_length
        if content_length is None or content_length > min(maximum_bytes, self._maximum_body_bytes):
            return b""
        self._set_timeout(min(timeout_seconds, self._maximum_timeout_seconds))
        try:
            return self._read(content_length)
        except TimeoutError as exc:
            raise RequestReadTimeout() from exc


class ShareHandlerFactory(Protocol):
    """Construct the existing share handler with a request-scoped body reader."""

    def __call__(self, body_reader: BodyReader) -> ShareHttpHandler: ...


class OperationGate(Protocol):
    """Return a context manager that holds the daemon mutation gate for one request."""

    def __call__(self) -> AbstractContextManager[object]: ...


class HttpService:
    """Route closed browser UI and share capability sets to injected safe handlers."""

    def __init__(
        self,
        *,
        ui_handler: UiHandler | Phase1UiHandler,
        share_handler_factory: ShareHandlerFactory,
        config: HttpServiceConfig | None = None,
        operation_gate: OperationGate | None = None,
    ) -> None:
        if (
            not callable(getattr(ui_handler, "handle", None))
            or not callable(share_handler_factory)
            or (operation_gate is not None and not callable(operation_gate))
        ):
            raise ValueError("invalid HTTP service dependencies")
        self._ui_handler = ui_handler
        self._share_handler_factory = share_handler_factory
        self.config = config or HttpServiceConfig()
        self._operation_gate: OperationGate = (
            (lambda: nullcontext()) if operation_gate is None else operation_gate
        )

    def dispatch(
        self,
        *,
        method: str,
        path: str,
        headers: tuple[tuple[str, str], ...],
        body_reader: BodyReader,
    ) -> ServiceResponse:
        """Dispatch one bounded parsed request without exposing handler internals."""
        if (
            not isinstance(method, str)
            or not isinstance(path, str)
            or not callable(body_reader)
            or not _valid_headers(headers, self.config.maximum_header_bytes)
        ):
            return _text_response(400, "invalid_request")
        try:
            with self._operation_gate():
                return self._dispatch(
                    method=method,
                    path=path,
                    headers=headers,
                    body_reader=body_reader,
                )
        except RuntimeError:
            return _text_response(503, "service_unavailable")

    def _dispatch(
        self,
        *,
        method: str,
        path: str,
        headers: tuple[tuple[str, str], ...],
        body_reader: BodyReader,
    ) -> ServiceResponse:
        if method == "GET" and self.config.route_mode is not HttpRouteMode.SHARE_ONLY:
            if isinstance(self._ui_handler, Phase1UiHandler):
                return _copy_response(
                    self._ui_handler.handle(Phase1UiRequest(method, path, headers)),
                    maximum_body_bytes=self.config.maximum_response_bytes,
                )
            return _copy_response(
                self._ui_handler.handle(UiRequest(method, path, headers)),
                maximum_body_bytes=self.config.maximum_response_bytes,
            )
        if method == "GET":
            return _text_response(405, "method_not_allowed", allow="POST")
        if method != "POST":
            allow = (
                "GET"
                if self.config.route_mode is HttpRouteMode.UI_ONLY
                else "POST"
                if self.config.route_mode is HttpRouteMode.SHARE_ONLY
                else "GET, POST"
            )
            return _text_response(405, "method_not_allowed", allow=allow)
        if _is_browser_post_path(path):
            if self.config.route_mode is HttpRouteMode.SHARE_ONLY:
                return _text_response(404, "not_found")
            if isinstance(self._ui_handler, Phase1UiHandler):
                preflight = self._ui_handler.preflight(Phase1UiRequest(method, path, headers))
                if preflight is not None:
                    return _copy_response(
                        preflight,
                        maximum_body_bytes=self.config.maximum_response_bytes,
                    )
                return _copy_response(
                    self._ui_handler.handle(
                        Phase1UiRequest(
                            method,
                            path,
                            headers,
                            _read_ui_body(
                                body_reader,
                                headers=headers,
                                maximum_body_bytes=self.config.maximum_body_bytes,
                                timeout_seconds=self.config.request_timeout_seconds,
                            ),
                        )
                    ),
                    maximum_body_bytes=self.config.maximum_response_bytes,
                )
            return _text_response(405, "method_not_allowed", allow="GET")
        if not _is_share_post_path(path):
            return _text_response(404, "not_found")
        if self.config.route_mode is HttpRouteMode.UI_ONLY:
            return _text_response(405, "method_not_allowed", allow="GET")
        content_length = _content_length(headers)
        if content_length is not None and content_length > self.config.maximum_body_bytes:
            return _json_response(413, b'{"code":"request_too_large"}')
        try:
            share_handler = self._share_handler_factory(body_reader)
        except Exception:
            return _text_response(503, "service_unavailable")
        if not isinstance(share_handler, ShareHttpHandler):
            return _text_response(503, "service_unavailable")
        return _copy_response(
            share_handler.handle(HttpRequest(method, path, headers)),
            maximum_body_bytes=self.config.maximum_response_bytes,
        )


def _is_browser_post_path(path: str) -> bool:
    return path in {"/auth/login", "/auth/logout"} or path.startswith("/api/")


def _is_share_post_path(path: str) -> bool:
    return path in {"/share", "/captures"}


def _read_ui_body(
    body_reader: BodyReader,
    *,
    headers: tuple[tuple[str, str], ...],
    maximum_body_bytes: int,
    timeout_seconds: float,
) -> bytes:
    content_length = _content_length(headers)
    try:
        body = body_reader(maximum_body_bytes, timeout_seconds)
    except RequestReadTimeout:
        return b""
    if not isinstance(body, bytes):
        return b""
    if len(body) > maximum_body_bytes:
        return body[: maximum_body_bytes + 1]
    if content_length is not None and len(body) != content_length:
        return b""
    return body


def _valid_headers(headers: object, maximum_bytes: int) -> bool:
    if not isinstance(headers, tuple) or len(headers) > _MAXIMUM_HEADER_COUNT:
        return False
    total = 0
    for header in headers:
        if (
            not isinstance(header, tuple)
            or len(header) != 2
            or not isinstance(header[0], str)
            or not isinstance(header[1], str)
            or not header[0].isascii()
            or not header[1].isascii()
            or "\r" in header[0]
            or "\n" in header[0]
            or "\r" in header[1]
            or "\n" in header[1]
        ):
            return False
        total += len(header[0]) + len(header[1]) + 4
        if total > maximum_bytes:
            return False
    return True


def _content_length(headers: tuple[tuple[str, str], ...]) -> int | None:
    values = [value for name, value in headers if name.lower() == "content-length"]
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        return None
    return int(values[0])


def _copy_response(response: object, *, maximum_body_bytes: int) -> ServiceResponse:
    status = getattr(response, "status", None)
    body = getattr(response, "body", None)
    headers = getattr(response, "headers", None)
    if (
        type(status) is not int
        or not isinstance(body, bytes)
        or len(body) > maximum_body_bytes
        or not isinstance(headers, tuple)
        or not _valid_headers(headers, DEFAULT_MAXIMUM_RESPONSE_BYTES)
    ):
        return _text_response(503, "service_unavailable")
    return ServiceResponse(status=status, body=body, headers=headers)


def _text_response(status: int, text: str, *, allow: str | None = None) -> ServiceResponse:
    headers = [("Content-Type", "text/plain; charset=utf-8")]
    if allow is not None:
        headers.append(("Allow", allow))
    return ServiceResponse(status=status, body=text.encode("utf-8"), headers=tuple(headers))


def _json_response(status: int, body: bytes) -> ServiceResponse:
    return ServiceResponse(
        status=status,
        body=body,
        headers=(("Content-Type", "application/json"),),
    )


@runtime_checkable
class HttpServerProtocol(Protocol):
    """The small stdlib server lifecycle surface used by this composition root."""

    def serve_forever(self, poll_interval: float = 0.5) -> None: ...

    def shutdown(self) -> None: ...

    def server_close(self) -> None: ...


class HttpServerFactory(Protocol):
    """Inject a server constructor to test lifecycle behavior without sockets."""

    def __call__(
        self,
        address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
    ) -> HttpServerProtocol: ...


@dataclass(slots=True)
class ManagedHttpServer:
    """Own one already-composed stdlib server and close it exactly once."""

    _server: HttpServerProtocol
    _closed: bool = False
    _close_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        try:
            self._server.serve_forever(poll_interval)
        finally:
            self._close()

    def shutdown(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._close()

    def close(self) -> None:
        self._close()

    def _close(self) -> None:
        with self._close_lock:
            if not self._closed:
                self._server.server_close()
                self._closed = True


def create_http_server(
    service: HttpService,
    *,
    server_factory: HttpServerFactory | None = None,
) -> ManagedHttpServer:
    """Compose, but do not start, a loopback-only stdlib HTTP server."""
    if not isinstance(service, HttpService):
        raise ValueError("invalid HTTP service")
    handler_class = _request_handler(service)
    factory = _default_server_factory if server_factory is None else server_factory
    server = factory((service.config.bind.host, service.config.bind.port), handler_class)
    if not isinstance(server, HttpServerProtocol):
        raise ValueError("invalid HTTP server")
    return ManagedHttpServer(server)


def _default_server_factory(
    address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]
) -> HttpServerProtocol:
    server = ThreadingHTTPServer(address, handler_class)
    server.daemon_threads = True
    return server


def _request_handler(service: HttpService) -> type[BaseHTTPRequestHandler]:
    config = service.config

    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "open-brain"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(config.request_timeout_seconds)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            del message, explain
            self._send(_text_response(code, "invalid_request"))

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def _dispatch(self) -> None:
            headers = tuple((name, value) for name, value in self.headers.items())
            response = service.dispatch(
                method=self.command,
                path=self.path,
                headers=headers,
                body_reader=self._body_reader(headers),
            )
            self._send(response)

        def _body_reader(self, headers: tuple[tuple[str, str], ...]) -> BodyReader:
            return BoundedBodyReader(
                content_length=_content_length(headers),
                maximum_body_bytes=config.maximum_body_bytes,
                maximum_timeout_seconds=config.request_timeout_seconds,
                read=self.rfile.read,
                set_timeout=self.connection.settimeout,
            )

        def _send(self, response: ServiceResponse) -> None:
            if len(response.body) > config.maximum_response_bytes:
                response = _text_response(503, "service_unavailable")
            self.send_response(response.status)
            for name, value in response.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.body)
            self.close_connection = True

    return RequestHandler
