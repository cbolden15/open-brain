# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Bounded stdio JSON-RPC transport for the work-only MCP adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import BinaryIO

from open_brain_legacy._compat.open_brain.integrations.mcp import (
    EngineMcpAdapter,
    LocalStdioMcpAdapter,
    McpCallError,
)
from open_brain_legacy._compat.open_brain.integrations.ports import IntegrationScope

MCP_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset(
    {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
)
DEFAULT_MAXIMUM_MESSAGE_BYTES = 65_536
_MINIMUM_MESSAGE_BYTES = 1_024
_MAXIMUM_MESSAGE_BYTES = 1_048_576


def serve_stdio_mcp(
    adapter: LocalStdioMcpAdapter | EngineMcpAdapter,
    *,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    maximum_message_bytes: int = DEFAULT_MAXIMUM_MESSAGE_BYTES,
) -> None:
    """Serve line-delimited MCP JSON-RPC until EOF without writing to stderr."""
    if (
        not isinstance(adapter, LocalStdioMcpAdapter | EngineMcpAdapter)
        or adapter.transport != "stdio"
        or adapter.scope is not IntegrationScope.WORK
    ):
        raise ValueError("invalid MCP adapter")
    if (
        type(maximum_message_bytes) is not int
        or not _MINIMUM_MESSAGE_BYTES <= maximum_message_bytes <= _MAXIMUM_MESSAGE_BYTES
    ):
        raise ValueError("invalid maximum message bytes")

    initialized = False
    while True:
        line = input_stream.readline(maximum_message_bytes + 1)
        if not line:
            return
        if len(line) > maximum_message_bytes:
            if not line.endswith(b"\n"):
                _discard_line(input_stream)
            if not _write_response(
                output_stream,
                _error_response(None, -32600, "invalid request"),
                maximum_message_bytes,
            ):
                return
            continue
        response, initialized = _handle_message(line, adapter, initialized)
        if response is not None and not _write_response(
            output_stream, response, maximum_message_bytes
        ):
            return


def _discard_line(input_stream: BinaryIO) -> None:
    while True:
        remainder = input_stream.readline(DEFAULT_MAXIMUM_MESSAGE_BYTES)
        if not remainder or remainder.endswith(b"\n"):
            return


def _handle_message(
    line: bytes,
    adapter: LocalStdioMcpAdapter | EngineMcpAdapter,
    initialized: bool,
) -> tuple[dict[str, object] | None, bool]:
    try:
        decoded = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_response(None, -32700, "parse error"), initialized
    if not isinstance(decoded, dict):
        return _error_response(None, -32600, "invalid request"), initialized
    request: dict[str, object] = decoded
    request_id = request.get("id")
    has_response = "id" in request
    if not _is_request_id(request_id, has_response):
        return _error_response(None, -32600, "invalid request"), initialized
    method = request.get("method")
    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return (
            _error_response(request_id if has_response else None, -32600, "invalid request"),
            initialized,
        )
    params = request.get("params", {})
    if not isinstance(params, dict):
        return (
            _error_response(request_id if has_response else None, -32602, "invalid params"),
            initialized,
        )

    if method.startswith("notifications/"):
        return None, initialized
    if not has_response:
        return None, initialized
    if method == "initialize":
        return _initialize_response(request_id, params), True
    if not initialized:
        return _error_response(request_id, -32600, "server not initialized"), initialized
    if method == "tools/list":
        if params:
            return _error_response(request_id, -32602, "invalid params"), initialized
        return _result_response(request_id, {"tools": list(adapter.list_tools())}), initialized
    if method == "tools/call":
        return _call_tool_response(request_id, params, adapter), initialized
    return _error_response(request_id, -32601, "method not found"), initialized


def _is_request_id(value: object, has_response: bool) -> bool:
    return not has_response or (isinstance(value, (str, int)) and not isinstance(value, bool))


def _initialize_response(
    request_id: object,
    params: Mapping[str, object],
) -> dict[str, object]:
    if set(params) != {"protocolVersion", "capabilities", "clientInfo"}:
        return _error_response(request_id, -32602, "invalid params")
    protocol_version = params.get("protocolVersion")
    if (
        not isinstance(protocol_version, str)
        or protocol_version not in SUPPORTED_MCP_PROTOCOL_VERSIONS
        or not isinstance(params.get("capabilities"), dict)
        or not isinstance(params.get("clientInfo"), dict)
    ):
        return _error_response(request_id, -32602, "invalid params")
    return _result_response(
        request_id,
        {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "open-brain", "version": "0.1.0"},
        },
    )


def _call_tool_response(
    request_id: object,
    params: Mapping[str, object],
    adapter: LocalStdioMcpAdapter | EngineMcpAdapter,
) -> dict[str, object]:
    if set(params) != {"name", "arguments"}:
        return _error_response(request_id, -32602, "invalid params")
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return _error_response(request_id, -32602, "invalid params")
    try:
        result = adapter.call_tool(name, arguments)
    except McpCallError as exc:
        message = str(exc)
        if message not in {"unknown tool", "invalid tool arguments"}:
            message = "tool call failed"
        return _result_response(
            request_id,
            {"content": [{"type": "text", "text": message}], "isError": True},
        )
    content = json.dumps(result, separators=(",", ":"), ensure_ascii=True)
    return _result_response(
        request_id,
        {
            "content": [{"type": "text", "text": content}],
            "structuredContent": result,
        },
    )


def _result_response(request_id: object, result: Mapping[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error_response(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _write_response(
    output_stream: BinaryIO,
    response: Mapping[str, object],
    maximum_message_bytes: int,
) -> bool:
    encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > maximum_message_bytes:
        encoded = json.dumps(
            _error_response(None, -32603, "response too large"),
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    try:
        output_stream.write(encoded + b"\n")
        output_stream.flush()
    except (OSError, ValueError):
        return False
    return True
