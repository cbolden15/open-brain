"""Retained work-scoped MCP entry point over the shared bounded transport."""

from __future__ import annotations

from typing import BinaryIO

from open_brain.integrations.mcp import EngineMcpAdapter, LocalStdioMcpAdapter
from open_brain.integrations.ports import IntegrationScope
from open_brain.services.mcp_protocol import (
    DEFAULT_MAXIMUM_MESSAGE_BYTES as DEFAULT_MAXIMUM_MESSAGE_BYTES,
)
from open_brain.services.mcp_protocol import (
    MCP_PROTOCOL_VERSION as MCP_PROTOCOL_VERSION,
)
from open_brain.services.mcp_protocol import (
    SUPPORTED_MCP_PROTOCOL_VERSIONS as SUPPORTED_MCP_PROTOCOL_VERSIONS,
)
from open_brain.services.mcp_protocol import serve_stdio_mcp as _serve


def serve_stdio_mcp(
    adapter: LocalStdioMcpAdapter | EngineMcpAdapter,
    *,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    maximum_message_bytes: int = DEFAULT_MAXIMUM_MESSAGE_BYTES,
) -> None:
    """Keep the retained adapter's explicit work-scope admission check."""
    if (
        not isinstance(adapter, LocalStdioMcpAdapter | EngineMcpAdapter)
        or adapter.transport != "stdio"
        or adapter.scope is not IntegrationScope.WORK
    ):
        raise ValueError("invalid MCP adapter")
    _serve(
        adapter,
        input_stream=input_stream,
        output_stream=output_stream,
        maximum_message_bytes=maximum_message_bytes,
    )
