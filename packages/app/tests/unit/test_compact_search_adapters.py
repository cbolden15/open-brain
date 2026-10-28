from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, ValidationError  # type: ignore[import-untyped]
from open_brain_engine.engine.compact_search_contracts import (
    CompactSearchPageRequest,
    CompactSearchPageResponse,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.services.compact_search import CompactSearchAppAdapter
from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.mcp_protocol import (
    COMPACT_SEARCH_TEXT_PREFIX,
    McpCallError,
    _tool_result_response,
    encoded_tool_response_size,
)
from open_brain.services.t03_adapters import (
    MAX_CONTENT_BYTES,
    MAX_CONTENT_CALLS,
    T03AppAdapter,
    T03AppError,
    T03SessionBudget,
)


def _authority(*grants: str, owner: bool = True) -> EffectiveAuthority:
    return EffectiveAuthority(
        "synthetic_actor", "synthetic_session", frozenset(grants), None, owner=owner
    )


def _empty(request: CompactSearchPageRequest) -> CompactSearchPageResponse:
    return CompactSearchPageResponse.from_wire(
        {
            "status": "ok",
            "dto_version": 1,
            "view": "compact",
            "group_by": request.group_by,
            "count_only": request.count_only,
            "mode_used": "lexical",
            "count_scope": "lexical_matches",
            "total_matches": 0,
            "total_groups": 0,
            "results": [],
            "complete": True,
            "next_cursor": None,
            "warnings": [],
        }
    )


class _Operation:
    def __init__(self) -> None:
        self.calls: list[CompactSearchPageRequest] = []
        self.maximum: int | None = None
        self.encoded_size: Callable[[Mapping[str, object]], int] | None = None

    def __call__(
        self,
        request: CompactSearchPageRequest,
        *,
        authority: EffectiveAuthority,
        maximum_response_bytes: int | None = None,
        encoded_size: Callable[[Mapping[str, object]], int] | None = None,
    ) -> CompactSearchPageResponse:
        self.calls.append(request)
        self.maximum = maximum_response_bytes
        self.encoded_size = encoded_size
        return _empty(request)


def _adapter(operation: _Operation, authority: EffectiveAuthority | None = None) -> LocalMcpAdapter:
    authority = authority or _authority()
    negotiated = T03AppAdapter(
        SimpleNamespace(retrieval=SimpleNamespace(search_page=lambda *_a, **_k: {})),
        authority,
    )
    return LocalMcpAdapter(
        authority=authority,
        negotiated=negotiated,
        search=lambda _q, _l: (),
        compact_search=CompactSearchAppAdapter(operation, authority, negotiated.budget),
    )


def test_compact_is_explicit_and_does_not_extend_empty_t03_describe() -> None:
    operation = _Operation()
    adapter = _adapter(operation)
    assert adapter.negotiated is not None
    assert adapter.call_tool("brain_contract_describe", {}) == adapter.negotiated.describe({})
    described = adapter.call_tool("brain_contract_describe", {"extensions": ["compact_search_v1"]})
    assert described["contract_version"] == "compact-search.v1"
    assert described["operations"] == [
        {"name": "search.compact.page", "dto_version": 1, "required_grants": ["search"]}
    ]
    assert described["limits"] == {
        "request_bytes": 65536,
        "default_limit": 20,
        "maximum_limit": 100,
        "default_target_bytes": 4096,
        "minimum_target_bytes": 2048,
        "maximum_target_bytes": 8192,
        "cursor_ttl_seconds": 900,
    }
    assert operation.calls == []
    without = replace(adapter, compact_search=None)
    for name in ("brain_search", "brain_search_page"):
        with pytest.raises(McpCallError, match="^unsupported_capability$"):
            without.call_tool(name, {"dto_version": 1, "query": "synthetic", "view": "compact"})
        schema = next(tool for tool in without.list_tools() if tool["name"] == name)["inputSchema"]
        assert "view" not in schema["properties"]
    with pytest.raises(McpCallError, match="^unsupported_capability$"):
        without.call_tool("brain_contract_describe", {"extensions": ["compact_search_v1"]})


@pytest.mark.parametrize(
    "arguments",
    [
        {"extensions": []},
        {"extensions": ["compact_search_v1", "other"]},
        {"extensions": ["compact_search_v1"], "other": True},
        {"extensions": "compact_search_v1"},
    ],
)
def test_extension_describe_has_closed_opt_in_grammar(arguments: dict[str, object]) -> None:
    with pytest.raises(McpCallError, match="^invalid_arguments$"):
        _adapter(_Operation()).call_tool("brain_contract_describe", arguments)


@pytest.mark.parametrize("name", ["brain_search", "brain_search_page"])
def test_tools_advertise_closed_full_and_compact_branches(name: str) -> None:
    adapter = _adapter(_Operation())
    schema = next(tool for tool in adapter.list_tools() if tool["name"] == name)["inputSchema"]
    validator = Draft202012Validator(schema)
    full = {"query": "synthetic"}
    if name == "brain_search_page":
        full["dto_version"] = cast(Any, 1)
    validator.validate(full)
    validator.validate({**full, "view": "full"})
    compact = {
        "dto_version": 1,
        "query": "synthetic",
        "view": "compact",
        "limit": 100,
        "group_by": "none",
        "count_only": True,
        "target_bytes": 2048,
    }
    validator.validate(compact)
    for invalid in (
        {**full, "group_by": "title"},
        {**full, "view": "full", "count_only": True},
        {**compact, "target_bytes": 1000},
        {**compact, "authority": {}},
    ):
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_compact_tools_share_t03_call_and_final_encoded_byte_budget() -> None:
    operation = _Operation()
    adapter = _adapter(operation)
    assert adapter.negotiated is not None
    request = {"dto_version": 1, "query": "synthetic", "view": "compact"}
    first = adapter.call_tool(
        "brain_search", request, request_id='escaped"id', maximum_response_bytes=3000
    )
    adapter.call_tool("brain_search_page", request)
    assert len(operation.calls) == 2
    assert all(call.limit == 20 and call.mode == "lexical" for call in operation.calls)
    assert adapter.negotiated.budget.content_calls == 2
    expected = encoded_tool_response_size('escaped"id', first) + encoded_tool_response_size(
        0, first
    )
    assert adapter.negotiated.budget.content_bytes == expected
    adapter.negotiated.budget.content_calls = MAX_CONTENT_CALLS
    with pytest.raises(McpCallError, match="^operation_pending$"):
        adapter.call_tool("brain_search", request)
    assert len(operation.calls) == 2
    adapter.negotiated.budget.content_calls = 0
    adapter.negotiated.budget.content_bytes = MAX_CONTENT_BYTES
    with pytest.raises(McpCallError, match="^response_too_large$"):
        adapter.call_tool("brain_search_page", request)
    assert len(operation.calls) == 2


def test_final_wrapper_budget_is_forwarded_and_checked_again() -> None:
    operation = _Operation()
    adapter = _adapter(operation)
    request = {"dto_version": 1, "query": "synthetic", "view": "compact"}
    result = adapter.call_tool(
        "brain_search", request, request_id="🙂", maximum_response_bytes=1234
    )
    assert operation.maximum == 1234
    assert operation.encoded_size is not None
    assert operation.encoded_size(result) == encoded_tool_response_size("🙂", result)
    assert adapter.negotiated is not None
    adapter.negotiated.budget.content_bytes = MAX_CONTENT_BYTES - 1500
    adapter.call_tool("brain_search_page", request, maximum_response_bytes=3000)
    assert operation.maximum == 1500
    with pytest.raises(McpCallError, match="^response_too_large$"):
        adapter.call_tool("brain_search", request, maximum_response_bytes=1)


def test_scoped_compact_does_not_enable_unsafe_legacy_callback_or_content_read() -> None:
    operation = _Operation()
    adapter = _adapter(operation, _authority("search", owner=False))
    request = {"dto_version": 1, "query": "synthetic", "view": "compact"}
    assert adapter.call_tool("brain_search", request)["view"] == "compact"
    schema = next(tool for tool in adapter.list_tools() if tool["name"] == "brain_search")[
        "inputSchema"
    ]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({"query": "synthetic"})
    with pytest.raises(McpCallError, match="^unknown tool$"):
        adapter.call_tool("brain_search", {"query": "synthetic"})
    assert "brain_read" not in {tool["name"] for tool in adapter.list_tools()}
    denied = _adapter(operation, _authority("content-read", owner=False))
    with pytest.raises(McpCallError, match="^unsupported_capability$"):
        denied.call_tool("brain_search", request)
    with pytest.raises(McpCallError, match="^unsupported_capability$"):
        denied.call_tool("brain_contract_describe", {"extensions": ["compact_search_v1"]})
    assert len(operation.calls) == 1


def test_compact_capability_requires_same_authority_and_budget() -> None:
    adapter = _adapter(_Operation())
    assert adapter.compact_search is not None
    with pytest.raises(ValueError, match="^invalid MCP compact authority$"):
        replace(adapter, compact_search=replace(adapter.compact_search, authority=_authority()))
    with pytest.raises(ValueError, match="^invalid MCP compact budget$"):
        replace(adapter, compact_search=replace(adapter.compact_search, budget=T03SessionBudget()))


@pytest.mark.parametrize(
    "extra",
    [
        {"authority": {}},
        {"limit": True},
        {"dto_version": 1.0},
        {"target_bytes": 8193},
        {"count_only": True, "cursor": "opaque"},
        {"view": "unknown"},
        {"query": "\ud800"},
    ],
)
def test_compact_request_validation_precedes_callback(extra: dict[str, object]) -> None:
    operation = _Operation()
    with pytest.raises(McpCallError, match="^invalid_arguments$"):
        _adapter(operation).call_tool(
            "brain_search",
            {
                "dto_version": 1,
                "query": "synthetic",
                "view": "compact",
                **extra,
            },
        )
    assert operation.calls == []


def test_compact_engine_errors_are_path_free() -> None:
    def fail(*_args: Any, **_kwargs: Any) -> CompactSearchPageResponse:
        raise T03Error("projection_stale")

    adapter = CompactSearchAppAdapter(fail, _authority(), T03SessionBudget())
    with pytest.raises(T03AppError, match="^projection_stale$"):
        adapter.invoke({"dto_version": 1, "query": "synthetic"})


def test_scoped_full_search_page_and_compact_share_existing_authority_and_budget() -> None:
    operation = _Operation()
    adapter = _adapter(operation, _authority("search", owner=False))
    assert adapter.negotiated is not None
    adapter.negotiated.parse_request = lambda _op, arguments: arguments

    def serialize(operation: str, response: object) -> dict[str, object]:
        return {"legacy": True}

    adapter.negotiated.serialize_response = serialize
    schema = next(tool for tool in adapter.list_tools() if tool["name"] == "brain_search_page")[
        "inputSchema"
    ]
    full = {"dto_version": 1, "query": "synthetic"}
    Draft202012Validator(schema).validate(full)
    assert adapter.call_tool("brain_search_page", full) == {"legacy": True}
    assert adapter.call_tool("brain_search_page", {**full, "view": "full"}) == {"legacy": True}
    adapter.call_tool("brain_search", {**full, "view": "compact"})
    assert adapter.negotiated.budget.content_calls == 3
    adapter.negotiated.budget.content_calls = MAX_CONTENT_CALLS
    with pytest.raises(McpCallError, match="^operation_pending$"):
        adapter.call_tool("brain_search_page", full)


def test_untrusted_wrapper_prefix_applies_only_to_compact_search_envelopes() -> None:
    legacy = {"status": "ok", "dto_version": 1, "results": [], "next_cursor": None}
    encoded = json.dumps(legacy, separators=(",", ":"), ensure_ascii=True)
    assert _tool_result_response(7, legacy) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "content": [{"type": "text", "text": encoded}],
            "structuredContent": legacy,
        },
    }
    compact = _empty(CompactSearchPageRequest(query="synthetic")).to_wire()
    wrapped = _tool_result_response(7, compact)
    result = cast(dict[str, Any], wrapped["result"])
    assert result["content"][0]["text"] == COMPACT_SEARCH_TEXT_PREFIX + json.dumps(
        compact,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert result["structuredContent"] == compact
    assert encoded_tool_response_size(7, compact) == len(
        json.dumps(
            wrapped,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    )


@pytest.mark.parametrize("command", ["search", "search-page"])
def test_cli_compact_defaults_are_twenty_and_full_defaults_remain_ten(command: str) -> None:
    from open_brain.services.local_entrypoints import (
        _parser,
        _t03_cli_request,
        _validate_compact_search_arguments,
    )

    compact = _parser().parse_args([command, "synthetic", "--compact"])
    _validate_compact_search_arguments(compact)
    operation, request = _t03_cli_request(compact)
    assert operation == "search.compact.page"
    assert request["limit"] == 20
    assert request["mode"] == "lexical"
    assert request["target_bytes"] == 4096
    assert request["group_by"] == "title"
    assert request["count_only"] is False
    full = _parser().parse_args([command, "synthetic"])
    _validate_compact_search_arguments(full)
    assert full.limit == 10
    if command == "search-page":
        operation, request = _t03_cli_request(full)
        assert operation == "search.page"
        assert set(request) == {"dto_version", "query", "filters", "mode", "limit", "cursor"}
        legacy_cursor = _parser().parse_args([command, "synthetic", "--cursor", "opaque"])
        _validate_compact_search_arguments(legacy_cursor)
        assert _t03_cli_request(legacy_cursor)[1]["cursor"] == "opaque"
