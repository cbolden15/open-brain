"""Explicit composition for bounded work-only MCP and HTTP services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO

from open_brain.capture.http import BodyReader, CaptureAcceptor, ShareHttpHandler
from open_brain.integrations.mcp import EngineMcpAdapter, LocalStdioMcpAdapter
from open_brain.integrations.ports import PageReader, RetrievalFeedback, WorkRetriever
from open_brain.integrations.ui import UiBindConfig, UiHandler
from open_brain.services.http_server import (
    HttpRouteMode,
    HttpServerFactory,
    HttpService,
    HttpServiceConfig,
    ManagedHttpServer,
    ServiceResponse,
    ShareHandlerFactory,
    create_http_server,
)
from open_brain.services.mcp_stdio import serve_stdio_mcp


@dataclass(frozen=True, slots=True)
class ProductionServiceDependencies:
    """Concrete capabilities required to compose local bounded services."""

    retriever: WorkRetriever | None = None
    feedback: RetrievalFeedback | None = None
    page_reader: PageReader | None = None
    capture: CaptureAcceptor | None = None
    expected_bearer_token: str | None = field(default=None, repr=False)
    clock: Callable[[], datetime] | None = None
    bind: UiBindConfig = field(default_factory=UiBindConfig)
    http_route_mode: HttpRouteMode = HttpRouteMode.COMBINED


@dataclass(frozen=True, slots=True)
class StdioMcpLifecycle:
    """A startable stdio MCP service that does not own process streams."""

    adapter: LocalStdioMcpAdapter | EngineMcpAdapter

    def serve(self, *, input_stream: BinaryIO, output_stream: BinaryIO) -> None:
        serve_stdio_mcp(
            self.adapter,
            input_stream=input_stream,
            output_stream=output_stream,
        )


@dataclass(frozen=True, slots=True)
class HttpLifecycle:
    """A startable HTTP service that creates no listener until ``start``."""

    service: HttpService

    def dispatch(
        self,
        *,
        method: str,
        path: str,
        headers: tuple[tuple[str, str], ...],
        body_reader: BodyReader,
    ) -> ServiceResponse:
        return self.service.dispatch(
            method=method,
            path=path,
            headers=headers,
            body_reader=body_reader,
        )

    def start(self, *, server_factory: HttpServerFactory | None = None) -> ManagedHttpServer:
        return create_http_server(self.service, server_factory=server_factory)


@dataclass(frozen=True, slots=True)
class ProductionServices:
    """Unstarted lifecycle objects for the bounded production-facing protocols."""

    mcp: StdioMcpLifecycle
    http: HttpLifecycle


def compose_production_services(
    dependencies: ProductionServiceDependencies,
) -> ProductionServices:
    """Compose local services from supplied work-only dependencies without I/O."""
    if not _valid_dependencies(dependencies):
        raise ValueError("invalid production service dependencies")
    assert dependencies.retriever is not None
    assert dependencies.feedback is not None
    assert dependencies.page_reader is not None
    assert dependencies.capture is not None
    assert dependencies.expected_bearer_token is not None
    assert dependencies.clock is not None
    try:
        mcp = LocalStdioMcpAdapter(
            retriever=dependencies.retriever,
            feedback=dependencies.feedback,
        )
        ui = UiHandler(
            expected_bearer_token=dependencies.expected_bearer_token,
            page_reader=dependencies.page_reader,
        )
        share_handler_factory = _ShareHandlerFactory(
            expected_bearer_token=dependencies.expected_bearer_token,
            capture=dependencies.capture,
            clock=dependencies.clock,
        )
        http = HttpService(
            ui_handler=ui,
            share_handler_factory=share_handler_factory,
            config=HttpServiceConfig(
                bind=dependencies.bind,
                route_mode=dependencies.http_route_mode,
            ),
        )
    except Exception as error:
        raise ValueError("invalid production service dependencies") from error
    return ProductionServices(mcp=StdioMcpLifecycle(mcp), http=HttpLifecycle(http))


def _valid_dependencies(value: object) -> bool:
    if not isinstance(value, ProductionServiceDependencies):
        return False
    return (
        value.retriever is not None
        and callable(getattr(value.retriever, "search", None))
        and value.feedback is not None
        and callable(getattr(value.feedback, "record", None))
        and value.page_reader is not None
        and callable(getattr(value.page_reader, "read", None))
        and value.capture is not None
        and callable(getattr(value.capture, "accept", None))
        and isinstance(value.expected_bearer_token, str)
        and bool(value.expected_bearer_token)
        and callable(value.clock)
        and isinstance(value.bind, UiBindConfig)
        and isinstance(value.http_route_mode, HttpRouteMode)
    )


@dataclass(frozen=True, slots=True)
class _ShareHandlerFactory(ShareHandlerFactory):
    """Create request-scoped share handlers without exposing the bearer token."""

    expected_bearer_token: str = field(repr=False)
    capture: CaptureAcceptor
    clock: Callable[[], datetime]

    def __call__(self, body_reader: BodyReader) -> ShareHttpHandler:
        return ShareHttpHandler(
            expected_bearer_token=self.expected_bearer_token,
            capture=self.capture,
            clock=self.clock,
            body_reader=body_reader,
        )
