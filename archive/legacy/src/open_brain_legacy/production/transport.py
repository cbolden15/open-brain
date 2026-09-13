from __future__ import annotations

import http.client
import ipaddress
import math
import socket
import ssl
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from open_brain_legacy.capture.egress import PinnedRequest, ResponseStream, TransportResponse

from .errors import ProductionRuntimeError, RuntimeFailureCode


class _PinnedConnection(Protocol):
    def request(self, request: PinnedRequest) -> TransportResponse: ...


class PinnedConnectionFactory(Protocol):
    """Creates a connection using the pinned address and original TLS hostname."""

    def open(self, request: PinnedRequest) -> _PinnedConnection: ...


class SystemResolver:
    """Explicit opt-in resolver for use with ``OutboundFetcher``."""

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled

    def resolve(self, hostname: str) -> tuple[str, ...]:
        if not self._enabled:
            raise ProductionRuntimeError(RuntimeFailureCode.DISABLED)
        try:
            answers = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except OSError as error:
            raise ProductionRuntimeError(RuntimeFailureCode.EXECUTION_FAILED) from error
        addresses: list[str] = []
        for family, _, _, _, sockaddr in answers:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            address = sockaddr[0]
            if isinstance(address, str) and address not in addresses:
                addresses.append(address)
        return tuple(addresses)


class DnsPinnedHttpTransport:
    """Concrete HTTP transport that connects only to ``PinnedRequest.pinned_address``.

    Redirect, DNS-answer, cookie-domain, and media validation remain owned by
    :class:`open_brain_legacy.capture.egress.OutboundFetcher`. This adapter deliberately
    performs no DNS lookup and never follows redirects itself.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        connection_factory: PinnedConnectionFactory | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("invalid transport configuration")
        if ssl_context is not None and not isinstance(ssl_context, ssl.SSLContext):
            raise ValueError("invalid transport configuration")
        self._enabled = enabled
        self._factory = connection_factory or _StdlibPinnedConnectionFactory(
            ssl.create_default_context() if ssl_context is None else ssl_context
        )

    def request(self, request: PinnedRequest) -> TransportResponse:
        if not self._enabled:
            raise ProductionRuntimeError(RuntimeFailureCode.DISABLED)
        _validate_request(request)
        try:
            return self._factory.open(request).request(request)
        except ProductionRuntimeError:
            raise
        except Exception as error:
            raise ProductionRuntimeError(RuntimeFailureCode.EXECUTION_FAILED) from error


@dataclass(frozen=True, slots=True)
class _StdlibPinnedConnectionFactory:
    context: ssl.SSLContext

    def open(self, request: PinnedRequest) -> _PinnedConnection:
        return _StdlibPinnedConnection(request, self.context)


class _StdlibPinnedConnection:
    def __init__(self, request: PinnedRequest, context: ssl.SSLContext) -> None:
        self._request = request
        self._context = context

    def request(self, request: PinnedRequest) -> TransportResponse:
        if request != self._request:
            raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
        connection: http.client.HTTPConnection
        if request.scheme == "https":
            connection = _PinnedHTTPSConnection(request, self._context)
        else:
            connection = _PinnedHTTPConnection(request)
        try:
            connection.request("GET", request.target, headers=dict(request.headers))
            response = connection.getresponse()
        except Exception:
            connection.close()
            raise
        return TransportResponse(
            status=response.status,
            headers=dict(response.getheaders()),
            stream=_HttpResponseStream(response, connection),
        )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, request: PinnedRequest) -> None:
        super().__init__(request.hostname, request.port, timeout=request.timeout_seconds)
        self._pinned_address = request.pinned_address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, request: PinnedRequest, context: ssl.SSLContext) -> None:
        super().__init__(
            request.hostname,
            request.port,
            timeout=request.timeout_seconds,
            context=context,
        )
        self._pinned_address = request.pinned_address
        self._ssl_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._pinned_address, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


class _HttpResponseStream(ResponseStream):
    def __init__(
        self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection
    ) -> None:
        self._response = response
        self._connection = connection

    def read(self, size: int) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        with suppress(Exception):
            self._response.close()
        with suppress(Exception):
            self._connection.close()


def _validate_request(request: PinnedRequest) -> None:
    if not isinstance(request, PinnedRequest):
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
    if request.scheme not in {"http", "https"} or not request.hostname:
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
    if (
        not isinstance(request.port, int)
        or isinstance(request.port, bool)
        or not 1 <= request.port <= 65535
    ):
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
    try:
        ipaddress.ip_address(request.pinned_address)
    except ValueError as error:
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT) from error
    if (
        not request.target.startswith("/")
        or "\r" in request.target
        or "\n" in request.target
        or not isinstance(request.timeout_seconds, (int, float))
        or isinstance(request.timeout_seconds, bool)
        or not math.isfinite(request.timeout_seconds)
        or request.timeout_seconds <= 0
    ):
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
    host_header: str | None = None
    if not isinstance(request.headers, Mapping):
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
    for name, value in request.headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or "\r" in value
            or "\n" in value
        ):
            raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
        if name.lower() == "host":
            host_header = value
    if host_header != request.hostname:
        raise ProductionRuntimeError(RuntimeFailureCode.INVALID_INPUT)
