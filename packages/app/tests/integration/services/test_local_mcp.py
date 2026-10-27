from __future__ import annotations

import io
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from collections.abc import Buffer, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    CaptureReceipt,
    CaptureTask,
    DecisionOutcome,
    ProposalDraft,
    PublicJobCaptureSink,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.v5 import V5_SIDECAR_PATHS
from open_brain_engine.storage.operational import FileLease

import open_brain.services.local_mcp as local_mcp_module
from open_brain.profile import compile_single_user_local
from open_brain.services.launcher_policy import LauncherPolicyError
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import (
    MAX_BRIEF_ACK_CALLS,
    MAX_BRIEF_READ_CALLS,
    MAX_FOLLOWUP_READ_CALLS,
    MAX_FOLLOWUP_RESPONSE_BYTES,
    MAX_FOLLOWUP_WRITE_CALLS,
    MAX_MESSAGE_BYTES,
    MAX_ORGANIZATION_READ_CALLS,
    MAX_ORGANIZATION_RESPONSE_BYTES,
    MAX_ORGANIZATION_WRITE_CALLS,
    MAX_REVIEW_DECISION_CALLS,
    MAX_REVIEW_PROPOSAL_CALLS,
    MAX_REVIEW_READ_CALLS,
    MAX_REVIEW_RESPONSE_BYTES,
    MCP_REGISTERED_TOOLS,
)
from open_brain.services.local_mcp import LocalMcpAdapter as _LocalMcpAdapter
from open_brain.services.local_operations import (
    current_brain_identity,
    mcp_capture_sink,
    search_brain,
)
from open_brain.services.mcp_protocol import (
    McpCallError,
    encoded_tool_response_size,
    serve_stdio_mcp,
)
from open_brain.services.review_publication import ReviewPublicationError
from open_brain.services.session_authority import TrustedSessionAuthority
from open_brain.services.session_consent import DurableProviderConsentStore
from open_brain.services.space_inbox import SpaceInboxError, SpaceInboxService
from open_brain.services.t03_adapters import T03AppAdapter

ROOT = Path(__file__).resolve().parents[5]
REVIEW_CAPTURE = "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
REVIEW_PROPOSAL = "proposal_8d87546c-3008-42ee-8632-0f2401904b35"
REVIEW_DIGEST = "d" * 64
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
}
PROGRAM = """
import socket, sys
from open_brain.services.local_entrypoints import run_cli

def forbidden(*args, **kwargs):
    raise AssertionError('network use')
for name in ('connect', 'connect_ex', 'bind', 'listen'):
    setattr(socket.socket, name, forbidden)
result = run_cli()
assert not any(
    name.startswith(
        (
            'open_brain.integrations',
            'open_brain.services.appliance_',
            'open_brain.services.phase1_',
            'open_brain.services.secure_node',
            'open_brain_connectors',
        )
    )
    for name in sys.modules
)
raise SystemExit(result)
"""


def _owner_authority() -> EffectiveAuthority:
    return EffectiveAuthority("mcp-test-owner", "mcp-test-session", frozenset(), None, owner=True)


class LocalMcpAdapter(_LocalMcpAdapter):
    """Test constructor that makes legacy owner-only fixtures explicit."""

    def __init__(self, **capabilities: Any) -> None:
        authority = cast(
            EffectiveAuthority,
            capabilities.pop("authority", _owner_authority()),
        )
        super().__init__(authority=authority, **capabilities)


def _call(name: str, arguments: dict[str, object], identifier: int = 2) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _wire(adapter: LocalMcpAdapter, *messages: dict[str, object]) -> list[Any]:
    incoming = b"".join(json.dumps(item).encode() + b"\n" for item in messages)
    outgoing = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(incoming),
        output_stream=outgoing,
        maximum_message_bytes=MAX_MESSAGE_BYTES,
    )
    return [json.loads(line) for line in outgoing.getvalue().splitlines()]


def _adapter(tasks: Any, capture: bool = True, search: bool = True) -> LocalMcpAdapter:
    return LocalMcpAdapter(
        capture=mcp_capture_sink(tasks) if capture else None,
        search=(
            lambda query, limit: search_brain(
                tasks.retrieval, tasks.reconciliation, query, limit=limit
            )
        )
        if search
        else None,
    )


@pytest.fixture
def tasks(tmp_path: Path) -> Any:
    return open_local_engine(compile_single_user_local(tmp_path / "brain"))


@pytest.mark.parametrize(("capture", "search"), [(True, False), (False, True), (True, True)])
def test_capabilities_are_independently_listed_and_enforced(
    tasks: Any,
    capture: bool,
    search: bool,
) -> None:
    adapter = _adapter(tasks, capture, search)
    responses = _wire(
        adapter,
        INITIALIZE,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        _call("brain_capture", {"text": "w6 capability token"}, 3),
        _call("brain_search", {"query": "w6 capability token"}, 4),
        _call("brain_fetch", {"result_id": "unused"}, 5),
    )
    assert responses[0]["result"]["capabilities"] == {"tools": {}}
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == (
        {"brain_catalog"}
        | ({"brain_capture"} if capture else set())
        | ({"brain_search"} if search else set())
    )
    assert responses[2]["result"].get("isError", False) is not capture
    assert responses[3]["result"].get("isError", False) is not search
    assert responses[4]["result"]["content"][0]["text"] == "unknown tool"
    if capture and search:
        result = responses[3]["result"]["structuredContent"]["results"][0]
        assert result["trust"] == "unverified"
        assert result["source_origin"] == "unknown"


def test_capture_tool_text_names_owner_only_retraction(tasks: Any) -> None:
    adapters = (_adapter(tasks), _destination_adapter(tasks, _destination_policy(tasks)))
    descriptions = {
        tool["name"]: tool["description"]
        for adapter in adapters
        for tool in adapter.list_tools()
    }
    for name in ("brain_capture", "brain_capture_submit"):
        text = descriptions[name]
        assert "cannot selectively delete" not in text
        assert "owner-only" in text and "open-brain retract" in text


def test_scoped_capture_registry_requires_grant_safe_operation_and_injection(
    tasks: Any,
) -> None:
    sink = mcp_capture_sink(tasks)
    granted_authority = EffectiveAuthority(
        "scoped-mcp",
        "scoped-mcp-granted",
        frozenset({"capture"}),
        None,
        allowed_capture_tiers=frozenset({PrivacyTier.PERSONAL}),
    )
    granted = LocalMcpAdapter(authority=granted_authority, capture=sink)
    assert {tool["name"] for tool in granted.list_tools()} == {
        "brain_capture",
        "brain_catalog",
    }
    assert (
        granted.call_tool("brain_capture", {"text": "scoped registry capture"})["status"]
        == "captured"
    )
    with pytest.raises(McpCallError, match="^tier_not_permitted$"):
        granted.call_tool(
            "brain_capture",
            {"text": "scoped tier override", "privacy_tier": "secret"},
        )

    denied = LocalMcpAdapter(
        authority=EffectiveAuthority(
            "scoped-mcp",
            "scoped-mcp-denied",
            frozenset(),
            None,
        ),
        capture=sink,
    )
    assert {tool["name"] for tool in denied.list_tools()} == {"brain_catalog"}
    with pytest.raises(McpCallError, match="^unknown tool$"):
        denied.call_tool("brain_capture", {"text": "denied"})

    for allowed_tiers in (
        frozenset(),
        frozenset({PrivacyTier.PUBLIC}),
        frozenset({PrivacyTier.SECRET}),
    ):
        wrong_tier = LocalMcpAdapter(
            authority=EffectiveAuthority(
                "scoped-mcp",
                "scoped-mcp-wrong-tier-" + str(len(allowed_tiers)),
                frozenset({"capture"}),
                None,
                allowed_capture_tiers=allowed_tiers,
            ),
            capture=sink,
        )
        assert {tool["name"] for tool in wrong_tier.list_tools()} == {"brain_catalog"}
        with pytest.raises(McpCallError, match="^unknown tool$"):
            wrong_tier.call_tool("brain_capture", {"text": "wrong tier"})

    unsafe_legacy_search = LocalMcpAdapter(
        authority=EffectiveAuthority(
            "scoped-mcp",
            "scoped-mcp-unsafe",
            frozenset({"search"}),
            None,
        ),
        search=lambda _query, _limit: (),
    )
    assert {tool["name"] for tool in unsafe_legacy_search.list_tools()} == {"brain_catalog"}
    with pytest.raises(McpCallError, match="^unknown tool$"):
        unsafe_legacy_search.call_tool("brain_search", {"query": "denied"})


def test_session_authority_is_revalidated_before_discovery_and_dispatch(tasks: Any) -> None:
    authority = EffectiveAuthority(
        "scoped-mcp",
        "scoped-mcp-revalidated",
        frozenset({"capture"}),
        None,
        allowed_capture_tiers=frozenset({PrivacyTier.PERSONAL}),
    )
    calls = 0
    revoked = False

    def revalidate() -> EffectiveAuthority:
        nonlocal calls
        calls += 1
        if revoked:
            raise LauncherPolicyError("stale_policy")
        return authority

    adapter = LocalMcpAdapter(
        authority=authority,
        capture=mcp_capture_sink(tasks),
        revalidate_authority=revalidate,
    )
    assert {tool["name"] for tool in adapter.list_tools()} == {
        "brain_capture",
        "brain_catalog",
    }
    assert calls == 1
    assert (
        adapter.call_tool("brain_capture", {"text": "revalidated capture"})["status"] == "captured"
    )
    assert calls == 3

    revoked = True
    with pytest.raises(LauncherPolicyError, match="stale_policy"):
        adapter.list_tools()
    with pytest.raises(LauncherPolicyError, match="stale_policy"):
        adapter.call_tool("brain_capture", {"text": "blocked"})


def test_organization_capabilities_are_explicitly_injected_with_bounded_schemas() -> None:
    listed = {"status": "listed", "items": [], "offset": 0, "next_offset": None}
    read_adapter = LocalMcpAdapter(
        inbox_list=lambda _arguments: listed,
        space_list=lambda _arguments: {
            "status": "listed",
            "spaces": [],
            "offset": 0,
            "next_offset": None,
        },
    )
    write_adapter = LocalMcpAdapter(
        space_create=lambda _arguments: {"status": "created", "space": {}},
        space_rename=lambda _arguments: {"status": "renamed", "space": {}},
        inbox_route=lambda _arguments: {
            "status": "routed",
            "capture_id": "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
            "space_id": "space_a877b476-b57b-4b77-840d-7cd38c3a12da",
        },
    )
    assert {tool["name"] for tool in read_adapter.list_tools()} == {
        "brain_catalog",
        "brain_inbox_list",
        "brain_space_list",
    }
    assert {tool["name"] for tool in write_adapter.list_tools()} == {
        "brain_catalog",
        "brain_space_create",
        "brain_space_rename",
        "brain_inbox_route",
    }
    for tool in (*read_adapter.list_tools()[:-1], *write_adapter.list_tools()[:-1]):
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "untrusted" in tool["description"]
        assert "network-backed" in tool["description"]
    inbox_schema = read_adapter.list_tools()[0]["inputSchema"]
    assert inbox_schema["properties"]["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
    }
    assert inbox_schema["properties"]["offset"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 1_000_000,
    }
    assert inbox_schema["properties"]["unassigned_only"] == {"type": "boolean"}
    with pytest.raises(McpCallError, match="^unknown tool$"):
        read_adapter.call_tool("brain_space_create", {"name": "Denied"})
    with pytest.raises(McpCallError, match="^unknown tool$"):
        write_adapter.call_tool("brain_inbox_list", {})


def test_review_capabilities_are_independently_listed_and_enforced() -> None:
    def listed(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {"status": "listed", "proposals": [], "offset": 0, "next_offset": None}

    def shown(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {"status": "shown", "proposal_id": REVIEW_PROPOSAL}

    def proposed(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {
            "status": "proposed",
            "proposal_id": REVIEW_PROPOSAL,
            "effective_idempotency_key": "retry",
        }

    def decided(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {
            "status": "approved",
            "proposal_id": REVIEW_PROPOSAL,
            "effective_idempotency_key": "retry",
        }

    readers = LocalMcpAdapter(review_list=listed, review_show=shown)
    proposer = LocalMcpAdapter(review_propose=proposed)
    decider = LocalMcpAdapter(
        review_approve=decided,
        review_reject=decided,
        review_edit_and_approve=decided,
    )
    assert {tool["name"] for tool in readers.list_tools()} == {
        "brain_catalog",
        "brain_review_list",
        "brain_review_show",
    }
    assert {tool["name"] for tool in proposer.list_tools()} == {
        "brain_catalog",
        "brain_review_propose",
    }
    assert {tool["name"] for tool in decider.list_tools()} == {
        "brain_catalog",
        "brain_review_approve",
        "brain_review_reject",
        "brain_review_edit_and_approve",
    }
    for tool in (
        *readers.list_tools()[:-1],
        *proposer.list_tools()[:-1],
        *decider.list_tools()[:-1],
    ):
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "untrusted" in tool["description"]
    with pytest.raises(McpCallError, match="^unknown tool$"):
        readers.call_tool(
            "brain_review_approve",
            {"proposal_id": REVIEW_PROPOSAL, "review_token": REVIEW_DIGEST},
        )


def test_review_quotas_validate_before_charging_and_separate_operation_groups() -> None:
    calls = 0

    def operation(_arguments: Mapping[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    adapter = LocalMcpAdapter(
        review_list=operation,
        review_propose=operation,
        review_approve=operation,
    )
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool("brain_review_list", {"limit": True})
    assert (adapter._review_read_calls, adapter._review_proposal_calls) == (0, 0)
    adapter._review_read_calls = MAX_REVIEW_READ_CALLS - 1
    adapter.call_tool("brain_review_list", {})
    with pytest.raises(McpCallError, match="^session_review_read_limit$"):
        adapter.call_tool("brain_review_list", {})
    adapter._review_proposal_calls = MAX_REVIEW_PROPOSAL_CALLS - 1
    adapter.call_tool(
        "brain_review_propose",
        {"capture_ids": [REVIEW_CAPTURE], "title": "Title", "markdown": "Body"},
    )
    with pytest.raises(McpCallError, match="^session_review_proposal_limit$"):
        adapter.call_tool(
            "brain_review_propose",
            {"capture_ids": [REVIEW_CAPTURE], "title": "Title", "markdown": "Body"},
        )
    adapter._review_decision_calls = MAX_REVIEW_DECISION_CALLS
    with pytest.raises(McpCallError, match="^session_review_decision_limit$"):
        adapter.call_tool(
            "brain_review_approve",
            {"proposal_id": REVIEW_PROPOSAL, "review_token": REVIEW_DIGEST},
        )
    assert calls == 2


def test_review_response_budget_includes_request_id_and_reserves_before_write() -> None:
    calls = 0
    result: dict[str, object] = {"status": "shown", "markdown": ('雪😀\\"\n\t\x00' * 1000)}

    def operation(_arguments: Mapping[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return result

    request_id = "request-" + '😀\\"\n' * 200
    reader = LocalMcpAdapter(review_list=operation)
    request = {**_call("brain_review_list", {}), "id": request_id}
    incoming = b"".join(json.dumps(item).encode() + b"\n" for item in (INITIALIZE, request))
    outgoing = io.BytesIO()
    serve_stdio_mcp(
        reader,
        input_stream=io.BytesIO(incoming),
        output_stream=outgoing,
        maximum_message_bytes=MAX_MESSAGE_BYTES,
    )
    response_bytes = outgoing.getvalue().splitlines()[1]
    response = json.loads(response_bytes)
    assert response["result"]["structuredContent"] == result
    assert json.loads(response["result"]["content"][0]["text"]) == result
    size = len(response_bytes)
    assert reader._review_response_bytes == size
    assert size == encoded_tool_response_size(request_id, result)

    reader._review_response_bytes = MAX_REVIEW_RESPONSE_BYTES - size
    assert reader.call_tool("brain_review_list", {}, request_id=request_id) == result
    assert reader._review_response_bytes == MAX_REVIEW_RESPONSE_BYTES
    with pytest.raises(McpCallError, match="^session_review_response_limit$"):
        reader.call_tool("brain_review_list", {}, request_id=request_id)

    writer = LocalMcpAdapter(review_propose=operation)
    with pytest.raises(McpCallError, match="^session_review_response_limit$"):
        writer.call_tool(
            "brain_review_propose",
            {"capture_ids": [REVIEW_CAPTURE], "title": "Title", "markdown": "Body"},
            request_id=request_id,
            maximum_response_bytes=1024,
        )
    assert calls == 3
    writer._review_response_bytes = MAX_REVIEW_RESPONSE_BYTES - 1
    with pytest.raises(McpCallError, match="^session_review_response_limit$"):
        writer.call_tool(
            "brain_review_propose",
            {"capture_ids": [REVIEW_CAPTURE], "title": "Title", "markdown": "Body"},
            request_id=request_id,
        )
    assert calls == 3


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("brain_inbox_list", {"limit": True}),
        ("brain_inbox_list", {"unassigned_only": None}),
        ("brain_space_list", {"offset": -1}),
        ("brain_space_list", {"limit": 101}),
        ("brain_space_create", {"name": " "}),
        ("brain_space_create", {"name": "Valid", "idempotency_key": None}),
        ("brain_space_rename", {"space_id": "space_not-a-uuid", "name": "Valid"}),
        (
            "brain_inbox_route",
            {
                "capture_id": "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
                "space_id": "space_a877b476-b57b-4b77-840d-7cd38c3a12da",
                "publish": True,
            },
        ),
    ],
)
def test_invalid_organization_arguments_do_not_consume_quota(
    tool: str, arguments: dict[str, object]
) -> None:
    calls = 0

    def operation(_arguments: Mapping[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "unused"}

    adapter = LocalMcpAdapter(
        inbox_list=operation,
        space_list=operation,
        space_create=operation,
        space_rename=operation,
        inbox_route=operation,
    )
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool(tool, arguments)
    assert calls == 0
    assert (
        adapter._organization_read_calls,
        adapter._organization_write_calls,
        adapter._organization_response_bytes,
    ) == (0, 0, 0)


def test_organization_quotas_include_duplicates_conflicts_and_share_response_bytes() -> None:
    listed = {"status": "listed", "items": [], "offset": 0, "next_offset": None}
    read_calls = 0

    def read(_arguments: Mapping[str, object]) -> dict[str, object]:
        nonlocal read_calls
        read_calls += 1
        return listed

    adapter = LocalMcpAdapter(inbox_list=read)
    adapter._organization_read_calls = MAX_ORGANIZATION_READ_CALLS - 1
    adapter.call_tool("brain_inbox_list", {})
    with pytest.raises(McpCallError, match="^session_organization_read_limit$"):
        adapter.call_tool("brain_inbox_list", {})
    assert read_calls == 1

    write_calls = 0

    def duplicate(_arguments: Mapping[str, object]) -> dict[str, object]:
        nonlocal write_calls
        write_calls += 1
        return {"status": "created", "space": {}}

    writer = LocalMcpAdapter(space_create=duplicate)
    writer._organization_write_calls = MAX_ORGANIZATION_WRITE_CALLS - 1
    writer.call_tool("brain_space_create", {"name": "Duplicate"})
    with pytest.raises(McpCallError, match="^session_organization_write_limit$"):
        writer.call_tool("brain_space_create", {"name": "Denied"})
    assert write_calls == 1

    def conflict(_arguments: Mapping[str, object]) -> dict[str, object]:
        raise SpaceInboxError("idempotency_conflict")

    conflicting = LocalMcpAdapter(space_create=conflict)
    conflicting._organization_write_calls = MAX_ORGANIZATION_WRITE_CALLS - 1
    with pytest.raises(McpCallError, match="^idempotency_conflict$"):
        conflicting.call_tool("brain_space_create", {"name": "Conflict"})
    assert conflicting._organization_write_calls == MAX_ORGANIZATION_WRITE_CALLS
    with pytest.raises(McpCallError, match="^session_organization_write_limit$"):
        conflicting.call_tool("brain_space_create", {"name": "Denied"})

    shared = LocalMcpAdapter(inbox_list=read, space_create=duplicate)
    listed_size = len(json.dumps(listed, sort_keys=True, separators=(",", ":")).encode())
    shared._organization_response_bytes = MAX_ORGANIZATION_RESPONSE_BYTES - listed_size
    shared.call_tool("brain_inbox_list", {})
    with pytest.raises(McpCallError, match="^session_organization_response_limit$"):
        shared.call_tool("brain_space_create", {"name": "Over response limit"})
    assert shared._organization_write_calls == 1


@pytest.mark.parametrize("remaining", [0, 1])
def test_organization_response_limit_cannot_commit_a_write(tasks: Any, remaining: int) -> None:
    service = SpaceInboxService(tasks.spaces)
    adapter = LocalMcpAdapter(space_create=service.space_create)
    adapter._organization_response_bytes = MAX_ORGANIZATION_RESPONSE_BYTES - remaining
    with pytest.raises(McpCallError, match="session_organization_response_limit"):
        adapter.call_tool("brain_space_create", {"name": "Must not be created"})
    assert tasks.spaces.spaces() == ()


def test_large_unicode_inbox_pages_fit_the_wire_without_losing_items(tasks: Any) -> None:
    capture_ids = set()
    for index in range(100):
        capture_ids.add(
            tasks.capture.accept(
                TextPayload("\U0001f680" * 320),
                delivery_id=f"unicode.page.{index}",
                title="\U0001f680" * 120,
                capture_why="\U0001f680" * 1000,
            ).capture_id
        )
    adapter = LocalMcpAdapter(inbox_list=SpaceInboxService(tasks.spaces).inbox_list)
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        responses = _wire(
            adapter,
            INITIALIZE,
            _call("brain_inbox_list", {"limit": 100, "offset": offset}),
        )
        assert len(json.dumps(responses[-1], ensure_ascii=True).encode()) < MAX_MESSAGE_BYTES
        result = responses[-1]["result"]["structuredContent"]
        seen.extend(item["capture_id"] for item in result["items"])
        next_offset = result["next_offset"]
        assert next_offset is None or next_offset > offset
        offset = next_offset
    assert len(seen) == len(capture_ids) and set(seen) == capture_ids


def test_organization_service_is_idempotent_path_free_and_preserves_safe_errors(
    tasks: Any,
) -> None:
    service = SpaceInboxService(tasks.inbox)
    adapter = LocalMcpAdapter(
        inbox_list=service.inbox_list,
        space_list=service.space_list,
        space_create=service.space_create,
        space_rename=service.space_rename,
        inbox_route=service.inbox_route,
    )
    capture = tasks.capture.accept(TextPayload("Synthetic inbox preview"), delivery_id="mcp.test")
    created = adapter.call_tool(
        "brain_space_create", {"name": "Cafe\u0301\r\nNotes", "idempotency_key": "same-key"}
    )
    duplicate = adapter.call_tool(
        "brain_space_create", {"name": "Cafe\u0301\r\nNotes", "idempotency_key": "same-key"}
    )
    assert duplicate == created
    space = cast(dict[str, object], created["space"])
    assert space["name"] == "Café\nNotes"
    renamed = adapter.call_tool(
        "brain_space_rename",
        {"space_id": space["space_id"], "name": "Renamed", "idempotency_key": "same-key"},
    )
    assert cast(dict[str, object], renamed["space"])["name"] == "Renamed"
    routed = adapter.call_tool(
        "brain_inbox_route",
        {
            "capture_id": capture.capture_id,
            "space_id": space["space_id"],
            "idempotency_key": "same-key",
        },
    )
    assert routed == {
        "status": "routed",
        "capture_id": capture.capture_id,
        "space_id": space["space_id"],
    }
    inbox = adapter.call_tool("brain_inbox_list", {"unassigned_only": False})
    assert cast(list[dict[str, object]], inbox["items"])[0]["preview"] == (
        "Synthetic inbox preview"
    )
    rendered = json.dumps(
        [created, renamed, routed, inbox, adapter.call_tool("brain_space_list", {})]
    )
    for protected in (str(tasks.profile.root), "source_reference", "delivery_id", "mcp.test"):
        assert protected not in rendered

    with pytest.raises(McpCallError, match="^idempotency_conflict$"):
        adapter.call_tool("brain_space_create", {"name": "Changed", "idempotency_key": "same-key"})
    with pytest.raises(McpCallError, match="^unknown_space$"):
        adapter.call_tool(
            "brain_space_rename",
            {
                "space_id": "space_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
                "name": "Missing",
            },
        )


@pytest.mark.parametrize(
    "code",
    ["idempotency_conflict", "unknown_space", "unknown_route_target", "published_capture"],
)
def test_organization_safe_errors_survive_mcp_transport(code: str) -> None:
    def fail(_arguments: Mapping[str, object]) -> dict[str, object]:
        raise SpaceInboxError(code)

    adapter = LocalMcpAdapter(space_rename=fail)
    reply = _wire(
        adapter,
        INITIALIZE,
        _call(
            "brain_space_rename",
            {
                "space_id": "space_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
                "name": "Synthetic",
            },
        ),
    )[1]
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"] == [{"type": "text", "text": code}]


def test_neither_flag_and_json_fail_before_bootstrap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "absent"
    for args in [
        ("mcp",),
        ("mcp", "--allow-capture", "--json"),
        ("--json", "mcp", "--allow-search"),
    ]:
        assert run_cli((*args, "--data-dir", str(root))) == 2
        assert capsys.readouterr().out == ""
        assert not root.exists()
    with pytest.raises(ValueError, match="no MCP capability"):
        LocalMcpAdapter()


def test_help_discloses_both_choices_without_bootstrap(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(("mcp", "--help"), environment={}) == 0
    output = capsys.readouterr().out
    for phrase in (
        "authority-scoped",
        "network-backed",
        "provider",
        "durable",
        "0.1.0",
        "owner-only",
        "500",
        "16 MiB",
        "2,000",
        "--allow-capture",
        "--allow-search",
        "--allow-review-read",
        "--allow-review-propose",
        "--allow-review-decide",
    ):
        assert phrase in output
    compact = " ".join(output.split())
    for phrase in (
        "500 review reads",
        "100 review proposals",
        "100 review decisions",
        "16 MiB encoded review output",
        "independent and off by default",
    ):
        assert phrase in compact


def test_delivery_is_exact_idempotent_across_sessions_and_namespaced(tasks: Any) -> None:
    adapter = _adapter(tasks)
    key = "synthetic-key-with-private-meaning"
    first = adapter.call_tool("brain_capture", {"text": "Café nebula", "idempotency_key": key})
    repeat = _adapter(tasks).call_tool(
        "brain_capture",
        {
            "text": "Café nebula",
            "idempotency_key": key,
        },
    )
    assert repeat == {**first, "duplicate": True}
    for different in ("other text", "Cafe\u0301 nebula"):
        with pytest.raises(McpCallError, match="^idempotency_conflict$"):
            adapter.call_tool("brain_capture", {"text": different, "idempotency_key": key})
    tasks.capture.accept(TextPayload("CLI namespace"), delivery_id=key)
    result = adapter.call_tool("brain_search", {"query": "nebula"})
    assert len(cast(list[object], result["results"])) == 1
    assert key not in json.dumps([first, repeat, result])
    connection = sqlite3.connect(tasks.profile.root / ".open-brain/state/phase1.sqlite3")
    try:
        rows = connection.execute(
            "SELECT delivery_id, actor_id, role_claim_json, submission_path FROM captures"
        ).fetchall()
        assert len(rows) == 2
        mcp_row = next(row for row in rows if row[0].startswith("delivery.mcp.key."))
        assert key not in mcp_row[0]
        assert mcp_row[1] != tasks.profile.owner_actor_id
        assert json.loads(mcp_row[2])["capabilities"] == ["capture.accept"]
        assert mcp_row[3] == "public_job"
    finally:
        connection.close()
    a = adapter.call_tool("brain_capture", {"text": "keyless"})
    b = adapter.call_tool("brain_capture", {"text": "keyless"})
    assert a["capture_id"] != b["capture_id"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("brain_capture", {}),
        ("brain_capture", {"text": ""}),
        ("brain_capture", {"text": " "}),
        ("brain_capture", {"text": "\ud800"}),
        ("brain_capture", {"text": "x" * 65537}),
        ("brain_capture", {"text": "note", "idempotency_key": None}),
        ("brain_capture", {"text": "note", "idempotency_key": "x" * 129}),
        ("brain_capture", {"text": "note", "idempotency_key": "\ud800"}),
        ("brain_capture", {"text": "note", "action": "publish"}),
        ("brain_search", {}),
        ("brain_search", {"query": "x", "limit": True}),
        ("brain_search", {"query": "x", "limit": 0}),
        ("brain_search", {"query": "x", "limit": 11}),
        ("brain_search", {"query": "x" * 501}),
        ("brain_search", {"query": "\ud800"}),
        ("brain_search", {"query": "\x00"}),
        ("brain_search", {"query": "***"}),
        ("brain_search", {"query": "x", "space_id": "unauthorized"}),
    ],
)
def test_invalid_arguments_do_not_consume_budget(tasks: Any, tool: str, arguments: Any) -> None:
    adapter = _adapter(tasks)
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool(tool, arguments)
    assert (adapter._capture_calls, adapter._capture_bytes, adapter._search_calls) == (0, 0, 0)
    assert tasks.inbox.list() == ()


class _CountingCapture:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, submission: Any) -> CaptureReceipt:
        self.calls += 1
        if submission.payload.text == "conflict":
            raise ValueError("conflicting delivery")
        return CaptureReceipt(
            capture_id="capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
            payload_family="text",
            state="inbox",
            enrichment_state="pending",
            space_id=None,
            canonical_path=None,
            duplicate=self.calls > 1,
        )


def _counting_adapter(tasks: Any) -> tuple[LocalMcpAdapter, _CountingCapture]:
    capture = _CountingCapture()
    sink = PublicJobCaptureSink(cast(CaptureTask, capture), context=mcp_capture_sink(tasks).context)
    return LocalMcpAdapter(capture=sink), capture


def test_exact_500_capture_calls_include_duplicates_and_conflicts(tasks: Any) -> None:
    adapter, capture = _counting_adapter(tasks)
    for index in range(500):
        if index % 2:
            with pytest.raises(McpCallError, match="idempotency_conflict"):
                adapter.call_tool("brain_capture", {"text": "conflict"})
        else:
            adapter.call_tool("brain_capture", {"text": "duplicate", "idempotency_key": "same"})
    with pytest.raises(McpCallError, match="^session_capture_limit$"):
        adapter.call_tool("brain_capture", {"text": "next"})
    assert capture.calls == 500


def test_exact_utf8_16_mib_budget_rejects_before_work(tasks: Any) -> None:
    adapter, capture = _counting_adapter(tasks)
    text = "\U0001f680" * 65536
    for _ in range(64):
        adapter.call_tool("brain_capture", {"text": text})
    assert adapter._capture_bytes == 16 * 1024 * 1024
    with pytest.raises(McpCallError, match="^session_capture_limit$"):
        adapter.call_tool("brain_capture", {"text": "a"})
    assert capture.calls == 64


def test_byte_budget_rejects_crossing_call_without_partial_charge(tasks: Any) -> None:
    adapter, capture = _counting_adapter(tasks)
    adapter._capture_bytes = 16 * 1024 * 1024 - 1
    with pytest.raises(McpCallError, match="session_capture_limit"):
        adapter.call_tool("brain_capture", {"text": "é"})
    assert capture.calls == 0
    assert adapter._capture_calls == 1
    adapter.call_tool("brain_capture", {"text": "a"})
    assert capture.calls == 1
    assert adapter._capture_calls == 2


def test_exact_2000_search_attempts_count_backend_failures() -> None:
    calls = 0

    def search(query: str, limit: int) -> tuple[()]:
        nonlocal calls
        calls += 1
        if calls % 2:
            raise RuntimeError("synthetic-private-error")
        return ()

    adapter = LocalMcpAdapter(search=search)
    for _ in range(1000):
        with pytest.raises(McpCallError, match="^tool call failed$"):
            adapter.call_tool("brain_search", {"query": "token"})
        adapter.call_tool("brain_search", {"query": "token"})
    with pytest.raises(McpCallError, match="^session_search_limit$"):
        adapter.call_tool("brain_search", {"query": "token"})
    assert calls == 2000


def test_transport_refuses_bad_initialize_and_recovers_from_oversize(tasks: Any) -> None:
    adapter = _adapter(tasks)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        _call("brain_capture", {"text": "must not be written"}),
        INITIALIZE,
        _call("brain_capture", {"text": "after valid init"}),
    ]
    responses = _wire(adapter, *messages)
    assert responses[0]["error"]["code"] == -32602
    assert responses[1]["error"]["message"] == "server not initialized"
    assert responses[3]["result"]["structuredContent"]["status"] == "captured"
    incoming = io.BytesIO(
        b"x" * (MAX_MESSAGE_BYTES + 1) + b"\n" + json.dumps(INITIALIZE).encode() + b"\n"
    )
    outgoing = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=incoming,
        output_stream=outgoing,
        maximum_message_bytes=MAX_MESSAGE_BYTES,
    )
    replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32600
    assert "result" in replies[1]
    assert adapter._capture_calls == 1


def test_protocol_errors_are_redacted_and_eof_and_broken_pipe_are_clean() -> None:
    def search(query: str, limit: int) -> tuple[()]:
        raise RuntimeError("synthetic-private-path-and-credential")

    adapter = LocalMcpAdapter(search=search)
    responses = _wire(adapter, INITIALIZE, _call("brain_search", {"query": "private-query"}))
    assert responses[1]["result"]["content"] == [{"type": "text", "text": "tool call failed"}]
    assert "private" not in json.dumps(responses)
    assert _wire(adapter) == []

    class BrokenOutput(io.BytesIO):
        def write(self, data: Buffer, /) -> int:
            raise BrokenPipeError

    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(json.dumps(INITIALIZE).encode() + b"\n"),
        output_stream=BrokenOutput(),
    )


def test_cli_and_mcp_share_semantic_dataset_results_and_export(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = json.loads((ROOT / "tests/fixtures/local-schema/dataset.json").read_text())
    root = tmp_path / "brain"
    for text in dataset["captures"]:
        assert run_cli(("capture", text, "--data-dir", str(root), "--json")) == 0
        capsys.readouterr()
    vault = tmp_path / "vault"
    vault.mkdir()
    for name, text in dataset["markdown"].items():
        (vault / name).write_text(text)
    assert run_cli(("import", str(vault), "--yes", "--data-dir", str(root), "--json")) == 0
    capsys.readouterr()
    tasks = open_local_engine(compile_single_user_local(root))
    adapter = _adapter(tasks)
    captured = adapter.call_tool("brain_capture", {"text": dataset["changed"]})
    mcp_result = adapter.call_tool("brain_search", {"query": "nebula", "limit": 10})
    assert run_cli(("search", "nebula", "--data-dir", str(root), "--json")) == 0
    assert json.loads(capsys.readouterr().out) == mcp_result
    hits = cast(list[dict[str, object]], mcp_result["results"])
    assert len(hits) == 5
    hit = next(item for item in hits if item["capture_id"] == captured["capture_id"])
    assert hit["trust"] == "unverified" and hit["source_origin"] == "unknown"
    assert "source_ref" not in json.dumps(mcp_result)
    export = tmp_path / "export"
    assert run_cli(("export", str(export), "--verify", "--data-dir", str(root), "--json")) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 18
    assert all((export / relative).is_file() for relative in V5_SIDECAR_PATHS)
    assert not any(".open-brain" in path.parts for path in export.rglob("*"))
    assert not any(path.suffix in {".sqlite", ".sqlite3"} for path in export.rglob("*"))
    record = json.loads(
        next((export / "sources/captures").rglob(str(captured["capture_id"]) + ".json")).read_text()
    )
    assert record["source"]["origin"] == "third_party"
    assert record["provenance"]["content_origin"] == "unknown"
    assert record["provenance"]["owner_context"] == "automation_absent"
    assert record["trust"]["label"] == "unverified"


def _start(root: Path, *flags: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", PROGRAM, "mcp", "--data-dir", str(root), *flags],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )


def _exchange(process: subprocess.Popen[str], message: dict[str, object]) -> Any:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    assert select.select([process.stdout], [], [], 15)[0], "MCP response timeout"
    return json.loads(process.stdout.readline())


@pytest.mark.parametrize(
    ("flag", "operation", "tool_name"),
    (
        ("--allow-content-read", "record.read", "brain_read"),
        ("--allow-history-read", "history.list", "brain_history_list"),
    ),
)
def test_entrypoint_admits_standalone_negotiated_read_grants(
    tasks: Any,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    operation: str,
    tool_name: str,
) -> None:
    observed: dict[str, LocalMcpAdapter] = {}

    def available(_adapter: T03AppAdapter) -> tuple[str, ...]:
        return (operation,)

    def serve(adapter: LocalMcpAdapter, **_kwargs: object) -> None:
        observed["adapter"] = adapter

    monkeypatch.setattr(T03AppAdapter, "available_operations", available)
    monkeypatch.setattr("open_brain.services.mcp_protocol.serve_stdio_mcp", serve)

    assert (
        run_cli(
            ("mcp", "--data-dir", str(tasks.profile.root), flag),
            environment={"HOME": str(tasks.profile.root.parent)},
        )
        == 0
    )
    assert {tool["name"] for tool in observed["adapter"].list_tools()} == {
        "brain_catalog",
        "brain_contract_describe",
        tool_name,
    }
    assert observed["adapter"].negotiated is not None
    assert observed["adapter"].negotiated.authority is observed["adapter"].authority


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("--allow-content-read", ["brain_contract_describe", "brain_read", "brain_catalog"]),
        (
            "--allow-history-read",
            [
                "brain_contract_describe",
                "brain_history_list",
                "brain_history_show",
                "brain_catalog",
            ],
        ),
    ],
)
def test_live_negotiated_grant_describes_only_implemented_contract(
    tasks: Any,
    flag: str,
    expected: list[str],
) -> None:
    process = _start(tasks.profile.root, flag)
    try:
        assert "result" in _exchange(process, INITIALIZE)
        tools = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [tool["name"] for tool in tools["result"]["tools"]] == expected
        described = _exchange(process, _call("brain_contract_describe", {}, 3))
        operations = described["result"]["structuredContent"]["operations"]
        assert [operation["name"] for operation in operations] == (
            ["record.read"] if flag == "--allow-content-read" else ["history.list", "history.show"]
        )
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None and process.stderr.read() == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_live_mcp_engine_retrieval_grants_and_cursor_failures(tasks: Any) -> None:
    captures = [
        tasks.capture.accept(
            TextPayload("actual MCP paged nebula"),
            delivery_id=f"mcp.retrieval.{index}",
        )
        for index in range(3)
    ]
    unicode_payload = TextPayload("é🙂é 漢字 actual MCP content\n" * 2000)
    unicode_capture = tasks.capture.accept(unicode_payload, delivery_id="mcp.retrieval.unicode")
    process = _start(
        tasks.profile.root,
        "--allow-search",
        "--allow-content-read",
    )
    other: subprocess.Popen[str] | None = None
    try:
        assert "result" in _exchange(process, INITIALIZE)
        # Cursor isolation needs two sessions, not simultaneous bootstrap writers.
        other = _start(tasks.profile.root, "--allow-search")
        listed = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert {"brain_contract_describe", "brain_search_page", "brain_read"} <= names
        first = _exchange(
            process,
            _call(
                "brain_search_page",
                {"dto_version": 1, "query": "nebula", "limit": 2},
                3,
            ),
        )["result"]["structuredContent"]
        assert [row["record_id"] for row in first["results"]] == sorted(
            capture.capture_id for capture in captures
        )[:2]
        cursor = first["next_cursor"]

        assert "result" in _exchange(other, INITIALIZE)
        cross_session = _exchange(
            other,
            _call(
                "brain_search_page",
                {"dto_version": 1, "query": "nebula", "limit": 2, "cursor": cursor},
                4,
            ),
        )
        assert cross_session["result"]["isError"] is True
        assert cross_session["result"]["content"] == [{"type": "text", "text": "cursor_invalid"}]

        tasks.capture.accept(
            TextPayload("actual MCP changed nebula"),
            delivery_id="mcp.retrieval.changed",
        )
        stale = _exchange(
            process,
            _call(
                "brain_search_page",
                {"dto_version": 1, "query": "nebula", "limit": 2, "cursor": cursor},
                5,
            ),
        )
        assert stale["result"]["content"] == [{"type": "text", "text": "cursor_stale"}]

        read_arguments: dict[str, object] = {
            "dto_version": 1,
            "record_id": unicode_capture.capture_id,
            "expected_revision_id": unicode_capture.capture_id,
            "target_bytes": 32_768,
        }
        chunks: list[str] = []
        offset = 0
        while True:
            read = _exchange(process, _call("brain_read", read_arguments, 6))["result"][
                "structuredContent"
            ]
            assert read["start_byte"] == offset
            text = read["content"]["text"]
            offset += len(text.encode("utf-8"))
            assert read["end_byte"] == offset
            chunks.append(text)
            if read["complete"]:
                break
            read_arguments = {**read_arguments, "cursor": read["next_cursor"]}
        assert "".join(chunks) == unicode_payload.text

        denied = _exchange(
            other,
            _call(
                "brain_read",
                {
                    "dto_version": 1,
                    "record_id": unicode_capture.capture_id,
                    "expected_revision_id": unicode_capture.capture_id,
                },
                7,
            ),
        )
        assert denied["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
    finally:
        for child in (process, other):
            if child is None:
                continue
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.wait(timeout=10)
            assert child.stderr is not None and child.stderr.read() == ""


def test_live_mcp_history_list_show_cursor_and_independent_grant(tasks: Any) -> None:
    space = tasks.spaces.create_space("MCP History", delivery_id="mcp.history.space")
    source = tasks.capture.accept(
        TextPayload("MCP history source"),
        delivery_id="mcp.history.source",
        space_id=space.space_id,
    )
    bodies = ["Old MCP é🙂é 漢字\n" * 2000, "Middle MCP body", "Current MCP body"]
    page_id: str | None = None
    for index, body in enumerate(bodies):
        proposal = tasks.review.propose(
            (source.capture_id,),
            (ProposalDraft(f"MCP history {index}", body),),
            delivery_id=f"mcp.history.proposal.{index}",
            target_page_id=page_id,
        )[0]
        decision = tasks.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id=f"mcp.history.decision.{index}",
            expected_review_digest=proposal.review_digest,
        )
        page_id = decision.page_id
    assert page_id is not None

    process = _start(tasks.profile.root, "--allow-history-read")
    other = _start(tasks.profile.root, "--allow-history-read")
    content_only = _start(tasks.profile.root, "--allow-content-read")
    try:
        for child in (process, other, content_only):
            assert "result" in _exchange(child, INITIALIZE)
        listed = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "brain_catalog",
            "brain_contract_describe",
            "brain_history_list",
            "brain_history_show",
        }
        first = _exchange(
            process,
            _call(
                "brain_history_list",
                {"dto_version": 1, "record_id": page_id, "limit": 2},
                3,
            ),
        )["result"]["structuredContent"]
        entries = first["entries"]
        assert len(entries) == 2
        assert entries[0]["is_current"] is True
        assert entries[1]["is_current"] is False
        cursor = first["next_cursor"]

        cross_session = _exchange(
            other,
            _call(
                "brain_history_list",
                {"dto_version": 1, "record_id": page_id, "limit": 2, "cursor": cursor},
                4,
            ),
        )
        assert cross_session["result"]["content"] == [{"type": "text", "text": "cursor_invalid"}]
        tail = _exchange(
            process,
            _call(
                "brain_history_list",
                {"dto_version": 1, "record_id": page_id, "limit": 2, "cursor": cursor},
                5,
            ),
        )["result"]["structuredContent"]
        assert tail["complete"] is True
        old_revision = tail["entries"][0]["revision_id"]

        arguments: dict[str, object] = {
            "dto_version": 1,
            "record_id": page_id,
            "expected_revision_id": old_revision,
            "target_bytes": 32_768,
        }
        chunks: list[str] = []
        while True:
            shown = _exchange(process, _call("brain_history_show", arguments, 6))["result"][
                "structuredContent"
            ]
            assert shown["record"]["revision_id"] == old_revision
            chunks.append(shown["content"]["text"])
            if shown["complete"]:
                break
            arguments = {**arguments, "cursor": shown["next_cursor"]}
        assert TextPayload(bodies[0]).text in "".join(chunks)

        denied = _exchange(
            content_only,
            _call(
                "brain_history_list",
                {"dto_version": 1, "record_id": page_id},
                7,
            ),
        )
        assert denied["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
    finally:
        for child in (process, other, content_only):
            if child.stdin is not None and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                child.wait(timeout=10)
            assert child.stderr is not None and child.stderr.read() == ""


@pytest.mark.parametrize(
    ("flag", "expected", "allowed_call"),
    [
        (
            "--allow-inbox-read",
            {"brain_catalog", "brain_inbox_list", "brain_space_list"},
            _call("brain_inbox_list", {}),
        ),
        (
            "--allow-organize",
            {
                "brain_catalog",
                "brain_contract_describe",
                "brain_space_create",
                "brain_space_rename",
                "brain_inbox_route",
                "brain_source_route",
            },
            _call("brain_space_create", {"name": "Stdio space"}),
        ),
    ],
)
def test_live_organization_flags_expose_only_their_tool_group(
    tasks: Any,
    flag: str,
    expected: set[str],
    allowed_call: dict[str, object],
) -> None:
    process = _start(tasks.profile.root, flag)
    try:
        assert "result" in _exchange(process, INITIALIZE)
        tools = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in tools["result"]["tools"]} == expected
        assert _exchange(process, allowed_call)["result"].get("isError") is not True

        denied_calls: dict[str, dict[str, object]] = {
            "brain_capture": {"text": "denied"},
            "brain_search": {"query": "denied"},
            "brain_inbox_list": {},
            "brain_space_list": {},
            "brain_space_create": {"name": "Denied"},
            "brain_space_rename": {
                "space_id": "space_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
                "name": "Denied",
            },
            "brain_inbox_route": {
                "capture_id": "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
                "space_id": "space_a877b476-b57b-4b77-840d-7cd38c3a12da",
            },
        }
        for name, arguments in denied_calls.items():
            if name in expected:
                continue
            denied = _exchange(process, _call(name, arguments))
            assert denied["result"]["isError"] is True
            assert denied["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None and process.stderr.read() == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_live_review_stdio_runs_all_operations_with_explicit_grants(
    tasks: Any,
) -> None:
    space = tasks.spaces.create_space("Review", delivery_id="review.mcp.space")
    sources = [
        tasks.capture.accept(
            TextPayload(text),
            delivery_id=f"review.mcp.source.{index}",
            space_id=space.space_id,
        ).capture_id
        for index, text in enumerate(("First MCP source", "Second MCP source"))
    ]
    process = _start(
        tasks.profile.root,
        "--allow-review-read",
        "--allow-review-propose",
        "--allow-review-decide",
    )
    try:
        assert "result" in _exchange(process, INITIALIZE)
        tools = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in tools["result"]["tools"]} == {
            "brain_catalog",
            "brain_review_list",
            "brain_review_show",
            "brain_review_propose",
            "brain_review_approve",
            "brain_review_reject",
            "brain_review_edit_and_approve",
        }
        proposed_reply = _exchange(
            process,
            _call(
                "brain_review_propose",
                {
                    "capture_ids": sources,
                    "title": "MCP combined",
                    "markdown": "MCP combined body",
                    "idempotency_key": "mcp-proposal",
                },
                3,
            ),
        )
        proposed = proposed_reply["result"]["structuredContent"]
        shown_reply = _exchange(
            process,
            _call("brain_review_show", {"proposal_id": proposed["proposal_id"]}, 4),
        )
        shown = shown_reply["result"]["structuredContent"]
        assert shown["markdown"] == "MCP combined body"
        approved_reply = _exchange(
            process,
            _call(
                "brain_review_approve",
                {
                    "proposal_id": proposed["proposal_id"],
                    "review_token": shown["review_token"],
                    "idempotency_key": "mcp-approve",
                },
                5,
            ),
        )
        approved = approved_reply["result"]["structuredContent"]
        assert approved["status"] == "approved"
        assert approved["page_id"] == proposed["page_id"]
        listed = _exchange(
            process,
            _call("brain_review_list", {"status": "approved"}, 6),
        )["result"]["structuredContent"]
        assert [row["proposal_id"] for row in listed["proposals"]] == [proposed["proposal_id"]]

        terminal_sources = [
            tasks.capture.accept(
                TextPayload(text),
                delivery_id=f"review.mcp.terminal.{index}",
                space_id=space.space_id,
            ).capture_id
            for index, text in enumerate(("Rejected MCP source", "Edited MCP source"))
        ]
        proposal_ids: list[str] = []
        tokens: list[str] = []
        for index, capture_id in enumerate(terminal_sources, start=7):
            child = _exchange(
                process,
                _call(
                    "brain_review_propose",
                    {
                        "capture_ids": [capture_id],
                        "title": f"Terminal {index}",
                        "markdown": "Terminal body",
                    },
                    index,
                ),
            )["result"]["structuredContent"]
            proposal_ids.append(child["proposal_id"])
            tokens.append(
                _exchange(
                    process,
                    _call("brain_review_show", {"proposal_id": child["proposal_id"]}, index + 10),
                )["result"]["structuredContent"]["review_token"]
            )
        rejected = _exchange(
            process,
            _call(
                "brain_review_reject",
                {"proposal_id": proposal_ids[0], "review_token": tokens[0]},
                20,
            ),
        )["result"]["structuredContent"]
        edited = _exchange(
            process,
            _call(
                "brain_review_edit_and_approve",
                {
                    "proposal_id": proposal_ids[1],
                    "review_token": tokens[1],
                    "markdown": "Explicit MCP edit",
                },
                21,
            ),
        )["result"]["structuredContent"]
        assert rejected["status"] == "rejected" and rejected["publication_id"] is None
        assert edited["status"] == "edited" and edited["publication_id"] is not None
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None and process.stderr.read() == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    ("flag", "expected"),
    (
        (
            "--allow-review-read",
            {"brain_catalog", "brain_review_list", "brain_review_show"},
        ),
        ("--allow-review-propose", {"brain_catalog", "brain_review_propose"}),
        (
            "--allow-review-decide",
            {
                "brain_catalog",
                "brain_review_approve",
                "brain_review_reject",
                "brain_review_edit_and_approve",
            },
        ),
    ),
)
def test_live_review_flags_expose_only_their_tool_group(
    tasks: Any, flag: str, expected: set[str]
) -> None:
    process = _start(tasks.profile.root, flag)
    try:
        assert "result" in _exchange(process, INITIALIZE)
        tools = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in tools["result"]["tools"]} == expected
        denied = _exchange(process, _call("brain_capture", {"text": "denied"}, 3))
        assert denied["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None and process.stderr.read() == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_live_cli_and_mcp_share_brain_and_bound_contention(
    tasks: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tasks.profile.root
    process = _start(root, "--allow-capture", "--allow-search")
    try:
        assert "result" in _exchange(process, INITIALIZE)
        # Hold the actual cross-process mutation lease after MCP startup.
        lease = FileLease(root / ".open-brain", owner_identity_id="w6-test")
        blocked_arguments: dict[str, object] = {
            "text": "blocked-token",
            "idempotency_key": "blocked-token",
        }
        with lease.acquire_shared_writer():
            started = time.monotonic()
            queued = _exchange(process, _call("brain_capture", blocked_arguments))
            assert queued["result"]["structuredContent"]["status"] == "queued"
            cli_queued = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    PROGRAM,
                    "capture",
                    "blocked-cli",
                    "--data-dir",
                    str(root),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert cli_queued.returncode == 0, cli_queued.stderr
            assert json.loads(cli_queued.stdout)["status"] == "queued"
            assert time.monotonic() - started < 15
        assert tasks.inbox.list() == ()
        mcp = _exchange(process, _call("brain_capture", blocked_arguments))
        assert mcp["result"]["structuredContent"]["status"] == "captured"
        assert run_cli(("search", "blocked-token", "--data-dir", str(root), "--json")) == 0
        assert json.loads(capsys.readouterr().out)["results"][0]["source_origin"] == "unknown"
        assert run_cli(("search", "blocked-cli", "--data-dir", str(root), "--json")) == 0
        assert (
            json.loads(capsys.readouterr().out)["results"][0]["source_origin"] == "owner_authored"
        )
        # An external SQLite writer exercises the real five-second busy timeout.
        connection = sqlite3.connect(
            root / ".open-brain/state/phase1.sqlite3", isolation_level=None
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            busy = _exchange(process, _call("brain_capture", {"text": "sqlite-blocked-token"}))
            assert busy["result"]["content"][0]["text"] == "database_busy"
            assert 4 <= time.monotonic() - started < 12
            connection.execute("ROLLBACK")
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()
        assert (
            _exchange(
                process,
                _call(
                    "brain_search_page",
                    {"dto_version": 1, "query": "sqlite-blocked-token"},
                ),
            )["result"]["structuredContent"]["results"]
            == []
        )
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stderr is not None and process.stderr.read() == ""
        assert not any((root / ".open-brain/run").iterdir())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_session_limits_are_public_protocol_errors_without_engine_work(tasks: Any) -> None:
    adapter = _adapter(tasks)
    adapter._capture_calls = 500
    adapter._search_calls = 2000
    replies = _wire(
        adapter,
        INITIALIZE,
        _call("brain_capture", {"text": "not accepted"}),
        _call("brain_search", {"query": "not accepted"}, 3),
    )
    assert [reply["result"]["content"][0]["text"] for reply in replies[1:]] == [
        "session_capture_limit",
        "session_search_limit",
    ]
    assert all(reply["result"]["isError"] for reply in replies[1:])
    assert tasks.inbox.list() == ()


def test_mcp_projected_results_and_conflicts_hide_protected_values(tasks: Any) -> None:
    path = "/synthetic-private/" + "note.txt"
    credential = "api" + "_key=" + "A" * 32
    digest = "ab" * 32
    key = "synthetic-client-private-key"
    text = "w6 residue canary " + path + " " + credential + " " + digest
    adapter = _adapter(tasks)
    replies = _wire(
        adapter,
        INITIALIZE,
        _call("brain_capture", {"text": text, "idempotency_key": key}),
        _call("brain_capture", {"text": "different", "idempotency_key": key}, 3),
        _call("brain_search", {"query": "w6 residue canary"}, 4),
    )
    rendered = json.dumps(replies)
    for private in (path, credential, digest, key, "urn:open-brain:mcp:", "source_reference"):
        assert private not in rendered
    assert replies[2]["result"]["content"][0]["text"] == "idempotency_conflict"
    assert len(replies[3]["result"]["structuredContent"]["results"]) == 1


@pytest.mark.parametrize(
    "payload",
    [b"[" * 2000 + b"]" * 2000, b"9" * 5000, b"\xff"],
    ids=["nested-array", "overlong-number", "invalid-utf8"],
)
def test_transport_parser_failures_are_bounded_and_recover(payload: bytes) -> None:
    adapter = LocalMcpAdapter(search=lambda _query, _limit: ())
    output = io.BytesIO()
    serve_stdio_mcp(
        adapter,
        input_stream=io.BytesIO(payload + b"\n" + json.dumps(INITIALIZE).encode() + b"\n"),
        output_stream=output,
    )
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["error"]["code"] in {-32700, -32600}
    assert "result" in replies[1]


@pytest.mark.parametrize(
    "raw_call",
    (
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"brain_search","arguments":{"query":"first","query":"second"}}}',
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"brain_search","arguments":{"query":"synthetic","limit":NaN}}}',
    ),
    ids=("duplicate-nested-query", "nonfinite-limit"),
)
def test_raw_transport_rejects_ambiguous_json_before_tool_callback(raw_call: bytes) -> None:
    calls = 0

    def search(_query: str, _limit: int) -> tuple[Any, ...]:
        nonlocal calls
        calls += 1
        return ()

    output = io.BytesIO()
    serve_stdio_mcp(
        LocalMcpAdapter(search=search),
        input_stream=io.BytesIO(json.dumps(INITIALIZE).encode() + b"\n" + raw_call + b"\n"),
        output_stream=output,
    )
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert "result" in replies[0]
    assert replies[1] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "parse error"},
    }
    assert calls == 0


def test_client_request_metadata_is_accepted_without_becoming_tool_input(tasks: Any) -> None:
    metadata = {
        "claudecode/toolUseId": "synthetic-tool-id",
        "progressToken": 7,
        "synthetic/owner_authority": {"allow_capture": True},
    }
    initialize = {
        **INITIALIZE,
        "params": {**cast(dict[str, object], INITIALIZE["params"]), "_meta": metadata},
    }
    capture = _call("brain_capture", {"text": "Synthetic metadata compatibility"}, 3)
    capture["params"] = {**cast(dict[str, object], capture["params"]), "_meta": metadata}
    responses = _wire(
        _adapter(tasks),
        initialize,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": metadata}},
        capture,
        _call("brain_search", {"query": "metadata compatibility"}, 4),
    )
    assert all("result" in response for response in responses)
    assert responses[2]["result"]["structuredContent"]["status"] == "captured"
    assert len(responses[3]["result"]["structuredContent"]["results"]) == 1
    assert "synthetic-tool-id" not in json.dumps(responses)
    denied = _wire(_adapter(tasks, capture=False), initialize, capture)
    assert denied[1]["result"]["isError"] is True
    assert denied[1]["result"]["content"][0]["text"] == "unknown tool"


@pytest.mark.parametrize("metadata", [None, [], "invalid"])
def test_malformed_request_metadata_is_rejected(tasks: Any, metadata: object) -> None:
    call = _call("brain_capture", {"text": "Must not be captured"})
    call["params"] = {**cast(dict[str, object], call["params"]), "_meta": metadata}
    responses = _wire(_adapter(tasks), INITIALIZE, call)
    assert responses[1]["error"]["code"] == -32602
    assert (
        _adapter(tasks).call_tool("brain_search", {"query": "Must not be captured"})["results"]
        == []
    )


def test_capture_privacy_tier_is_refused_through_the_non_owner_sink(tasks: Any) -> None:
    adapter = _adapter(tasks, capture=True, search=False)
    for tier in ("work", "unknown"):
        with pytest.raises(McpCallError, match="^tier_not_permitted$"):
            adapter.call_tool(
                "brain_capture", {"text": "synthetic tiered capture", "privacy_tier": tier}
            )
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool("brain_capture", {"text": "synthetic", "privacy_tier": "synthetic-tier"})
    plain = adapter.call_tool("brain_capture", {"text": "synthetic plain mcp capture"})
    assert plain["status"] == "captured"


def test_capture_privacy_tier_refusal_reaches_the_wire_with_its_code(tasks: Any) -> None:
    adapter = _adapter(tasks, capture=True, search=False)
    capture_tool = next(tool for tool in adapter.list_tools() if tool["name"] == "brain_capture")
    assert set(capture_tool["inputSchema"]["properties"]) == {"text", "idempotency_key"}
    responses = _wire(
        adapter,
        INITIALIZE,
        _call(
            "brain_capture",
            {
                "text": "synthetic wire tiered capture",
                "idempotency_key": "k1",
                "privacy_tier": "work",
            },
        ),
        _call(
            "brain_capture", {"text": "synthetic wire tiered capture", "idempotency_key": "k1"}, 3
        ),
    )
    assert responses[1]["result"] == {
        "content": [{"type": "text", "text": "tier_not_permitted"}],
        "isError": True,
    }
    assert responses[2]["result"]["structuredContent"]["status"] == "captured"
    assert responses[2]["result"]["structuredContent"]["duplicate"] is False


def _destination_policy(
    tasks: Any,
    *,
    allowed_capture_tiers: list[str] | None = None,
    capabilities: list[str] | None = None,
    issuer_epoch: int | None = None,
    brain_id: str | None = None,
) -> str:
    from open_brain_engine.core.access_contracts import derive_brain_id

    root = Path(tasks.profile.root)
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
    finally:
        connection.close()
    assert row is not None
    document = {
        "policy_version": "launcher-policy.v1",
        "principal_id": "synthetic-destination-principal",
        "session_id": "synthetic-destination-session",
        "capabilities": ["capture-submit"] if capabilities is None else capabilities,
        "space_ids": None,
        "allowed_read_tiers": ["public", "work"],
        "allowed_capture_tiers": (
            ["public", "work", "personal", "secret", "unknown"]
            if allowed_capture_tiers is None
            else allowed_capture_tiers
        ),
        "egress_mode": "owner_local",
        "provider_id": None,
        "consent_id": None,
        "authorization_generation": 0,
        "brain_id": brain_id if brain_id is not None else cast(str, row[0]),
        "issuer_epoch": issuer_epoch if issuer_epoch is not None else cast(int, row[1]),
    }
    assert document["brain_id"] == derive_brain_id(cast(str, tasks.profile.tenant_id)) or brain_id
    return json.dumps(document)


def _session_policy_file(
    tasks: Any,
    path: Path,
    *,
    read_tiers: list[str],
    capabilities: list[str] | None = None,
    capture_tiers: list[str] | None = None,
    external: bool = False,
    generation: int = 0,
) -> Path:
    document = cast(dict[str, object], json.loads(_destination_policy(tasks)))
    document.update(
        {
            "principal_id": "synthetic-session-principal",
            "session_id": "synthetic-scoped-session",
            "capabilities": (["content-read", "search"] if capabilities is None else capabilities),
            "allowed_read_tiers": read_tiers,
            "allowed_capture_tiers": [] if capture_tiers is None else capture_tiers,
            "egress_mode": "external_provider" if external else "owner_local",
            "provider_id": "synthetic-provider" if external else None,
            "consent_id": ("consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" if external else None),
            "authorization_generation": generation,
        }
    )
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _capture_row_count(root: Path) -> int:
    connection = sqlite3.connect(root / ".open-brain/state/phase1.sqlite3")
    try:
        return cast(int, connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
    finally:
        connection.close()


def test_live_session_policy_scopes_tiers_and_intersects_selected_operations(
    tasks: Any, tmp_path: Path
) -> None:
    captures = {
        tier: tasks.capture.accept(
            TextPayload(f"session-policy-canary {tier.value}"),
            delivery_id=f"session.policy.{tier.value}",
            privacy_tier=tier,
        )
        for tier in (PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL)
    }
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    general = _session_policy_file(
        tasks,
        authority_root / "general.json",
        read_tiers=["public", "work"],
    )
    process = _start(
        tasks.profile.root,
        "--allow-search",
        "--session-policy",
        str(general),
    )
    try:
        assert "result" in _exchange(process, INITIALIZE)
        listed = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "brain_search_page" in names
        assert "brain_read" not in names
        searched = _exchange(
            process,
            _call(
                "brain_search_page",
                {"dto_version": 1, "query": "session-policy-canary", "limit": 10},
                3,
            ),
        )["result"]["structuredContent"]
        assert {row["record_id"] for row in searched["results"]} == {
            captures[PrivacyTier.PUBLIC].capture_id,
            captures[PrivacyTier.WORK].capture_id,
        }
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=10)

    personal = _session_policy_file(
        tasks,
        authority_root / "personal.json",
        read_tiers=["public", "work", "personal"],
    )
    process = _start(
        tasks.profile.root,
        "--allow-search",
        "--session-policy",
        str(personal),
    )
    try:
        assert "result" in _exchange(process, INITIALIZE)
        searched = _exchange(
            process,
            _call(
                "brain_search_page",
                {"dto_version": 1, "query": "session-policy-canary", "limit": 10},
                2,
            ),
        )["result"]["structuredContent"]
        assert {row["record_id"] for row in searched["results"]} == {
            capture.capture_id for capture in captures.values()
        }
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=10)


def test_session_policy_capture_submit_requires_policy_capability(
    tasks: Any, tmp_path: Path
) -> None:
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    policy = _session_policy_file(
        tasks,
        authority_root / "capture-denied.json",
        read_tiers=["public", "work"],
        capabilities=[],
        capture_tiers=["public"],
    )
    before = _capture_row_count(Path(tasks.profile.root))
    process = _start(
        tasks.profile.root,
        "--allow-capture-submit",
        "--session-policy",
        str(policy),
    )
    try:
        assert "result" in _exchange(process, INITIALIZE)
        listed = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert names == {"brain_catalog"}
        denied = _exchange(
            process,
            _call(
                "brain_capture_submit",
                {
                    "text": "synthetic denied session capture",
                    "idempotency_key": "session.capture.denied",
                    "privacy_tier": "public",
                },
                3,
            ),
        )
        assert denied["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
        assert _capture_row_count(Path(tasks.profile.root)) == before
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=10)

    allowed_policy = _session_policy_file(
        tasks,
        authority_root / "capture-allowed.json",
        read_tiers=["public", "work"],
        capabilities=["capture-submit"],
        capture_tiers=["public"],
    )
    process = _start(
        tasks.profile.root,
        "--allow-capture-submit",
        "--session-policy",
        str(allowed_policy),
    )
    try:
        assert "result" in _exchange(process, INITIALIZE)
        listed = _exchange(process, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        assert {tool["name"] for tool in listed["result"]["tools"]} == {
            "brain_catalog",
            "brain_capture_submit",
        }
        captured = _exchange(
            process,
            _call(
                "brain_capture_submit",
                {
                    "text": "synthetic allowed session capture",
                    "idempotency_key": "session.capture.allowed",
                    "privacy_tier": "public",
                },
                5,
            ),
        )
        assert captured["result"]["structuredContent"]["status"] == "captured"
        assert _capture_row_count(Path(tasks.profile.root)) == before + 1
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=10)


def test_external_session_terminates_and_cannot_restart_after_consent_revocation(
    tasks: Any, tmp_path: Path
) -> None:
    authority_root = tmp_path / "authority"
    authority_root.mkdir(mode=0o700)
    consent_path = authority_root / "provider-consent.json"
    brain_id, issuer_epoch = current_brain_identity(tasks)
    consent = DurableProviderConsentStore(
        consent_path,
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )
    transition = consent.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK}),
        operation_id="grant-1",
        decided_at="2026-09-23T12:00:00Z",
        consent_id_factory=lambda: "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert transition.state.authorization_generation == 1
    policy = _session_policy_file(
        tasks,
        authority_root / "external.json",
        read_tiers=["public", "work"],
        external=True,
        generation=1,
    )
    flags = (
        "--allow-search",
        "--session-policy",
        str(policy),
        "--consent-state",
        str(consent_path),
    )
    process = _start(tasks.profile.root, *flags)
    try:
        assert "result" in _exchange(process, INITIALIZE)
        assert "result" in _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        consent.revoke(
            consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            operation_id="revoke-1",
            decided_at="2026-09-23T12:01:00Z",
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}) + "\n")
        process.stdin.flush()
        assert process.wait(timeout=15) == 78
        assert process.stderr is not None
        assert process.stderr.read() == "stale_policy\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    restarted = _start(tasks.profile.root, *flags)
    assert restarted.wait(timeout=15) == 78
    assert restarted.stderr is not None
    assert restarted.stderr.read() == "stale_policy\n"


@pytest.mark.parametrize("external", [False, True])
def test_entrypoint_external_composition_omits_unsafe_local_callbacks(
    tasks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: bool
) -> None:
    authority_root = tmp_path / "composition-authority"
    authority_root.mkdir(mode=0o700)
    brain_id, issuer_epoch = current_brain_identity(tasks)
    consent_path = authority_root / "consent.json"
    consent = DurableProviderConsentStore(
        consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch
    )
    consent.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
        operation_id="composition.grant",
        decided_at="2026-10-03T00:00:00Z",
        consent_id_factory=lambda: "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    policy = _session_policy_file(
        tasks,
        authority_root / "policy.json",
        read_tiers=["public"],
        capabilities=["search", "content-read", "history-read"],
        external=external,
        generation=1 if external else 0,
    )
    observed: list[LocalMcpAdapter] = []

    def serve(adapter: LocalMcpAdapter, **_kwargs: object) -> None:
        observed.append(adapter)

    monkeypatch.setattr("open_brain.services.mcp_protocol.serve_stdio_mcp", serve)
    assert (
        run_cli(
            (
                "mcp",
                "--data-dir",
                str(tasks.profile.root),
                "--session-policy",
                str(policy),
                *(("--consent-state", str(consent_path)) if external else ()),
                "--allow-search",
                "--allow-content-read",
                "--allow-history-read",
                "--allow-capture",
                "--allow-inbox-read",
                "--allow-organize",
                "--allow-review-read",
                "--allow-review-propose",
                "--allow-review-decide",
                "--allow-workspace-read",
                "--allow-graph-refresh",
                "--allow-brief-read",
                "--allow-brief-ack",
            )
        )
        == 0
    )
    assert len(observed) == 1 and observed[0].negotiated is not None
    for name in (
        "capture",
        "search",
        "inbox_list",
        "space_list",
        "space_create",
        "space_rename",
        "inbox_route",
        "review_list",
        "review_show",
        "review_propose",
        "review_approve",
        "review_reject",
        "review_edit_and_approve",
        "workspace_status",
        "graph_suggestions",
        "graph_projection",
        "graph_refresh",
        "brief",
        "brief_ack",
    ):
        assert (getattr(observed[0], name) is None) is external, name


def test_real_external_stdio_keeps_local_admin_callbacks_closed_with_all_flags(
    tasks: Any, tmp_path: Path
) -> None:
    from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason

    from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

    public = tasks.capture.submit(
        replace(
            _public_submission(tasks),
            payload=TextPayload("Synthetic scoped public canary"),
            delivery_id="synthetic.scoped.public",
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PUBLIC,
                reason=PrivacyReason.POLICY_PUBLIC,
                policy_version="synthetic-policy-v1",
                authority=Authority(cloud=False, external_egress=True),
            ),
        )
    )
    private = tasks.capture.accept(
        TextPayload("Synthetic scoped protected canary"),
        delivery_id="synthetic.scoped.secret",
        privacy_tier=PrivacyTier.SECRET,
    )
    authority_root = tmp_path / "stdio-authority"
    authority_root.mkdir(mode=0o700)
    brain_id, issuer_epoch = current_brain_identity(tasks)
    consent_path = authority_root / "consent.json"
    DurableProviderConsentStore(consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch).grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
        operation_id="stdio.grant",
        decided_at="2026-10-03T00:00:00Z",
        consent_id_factory=lambda: "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    policy = _session_policy_file(
        tasks,
        authority_root / "policy.json",
        read_tiers=["public"],
        capabilities=["search", "content-read", "history-read"],
        external=True,
        generation=1,
    )
    count = _capture_row_count(tasks.profile.root)
    process = _start(
        tasks.profile.root,
        "--session-policy",
        str(policy),
        "--consent-state",
        str(consent_path),
        "--allow-search",
        "--allow-content-read",
        "--allow-history-read",
        "--allow-capture",
        "--allow-capture-submit",
        "--allow-inbox-read",
        "--allow-organize",
        "--allow-review-read",
        "--allow-review-propose",
        "--allow-review-decide",
        "--allow-workspace-read",
        "--allow-graph-refresh",
    )
    try:
        assert "result" in _exchange(process, INITIALIZE)
        listing = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in listing["result"]["tools"]} == {
            "brain_catalog",
            "brain_contract_describe",
            "brain_search_page",
            "brain_read",
            "brain_history_list",
            "brain_history_show",
        }
        result = _exchange(
            process,
            _call(
                "brain_search_page",
                {
                    "dto_version": 1,
                    "query": "Synthetic scoped",
                    "limit": 10,
                },
                3,
            ),
        )["result"]["structuredContent"]
        assert [row["record_id"] for row in result["results"]] == [public.capture_id]
        for identifier, name in enumerate(
            (
                "brain_capture",
                "brain_capture_submit",
                "brain_search",
                "brain_source_route",
                "brain_inbox_list",
                "brain_space_list",
                "brain_space_create",
                "brain_space_rename",
                "brain_inbox_route",
                "brain_review_list",
                "brain_review_show",
                "brain_review_propose",
                "brain_review_approve",
                "brain_review_reject",
                "brain_review_edit_and_approve",
                "brain_workspace_status",
                "brain_graph_suggestions",
                "brain_graph_projection",
                "brain_graph_refresh",
                "brain_export",
                "brain_source_withdraw",
                "brain_sharing_approve",
            ),
            4,
        ):
            refused = _exchange(process, _call(name, {}, identifier))
            assert refused["result"]["isError"] is True, name
            assert "structuredContent" not in refused["result"]
            assert refused["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
            assert "Synthetic scoped protected canary" not in json.dumps(refused)
            assert private.capture_id not in json.dumps(refused)
        assert _capture_row_count(tasks.profile.root) == count
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=10)
    assert process.returncode == 0
    assert process.stderr is not None and process.stderr.read() == ""


def test_inflight_consent_revocation_blocks_content_before_stdio_output(
    tasks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason

    from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission

    tasks.capture.submit(
        replace(
            _public_submission(tasks),
            payload=TextPayload("Synthetic inflight consent canary"),
            delivery_id="synthetic.inflight.consent",
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PUBLIC,
                reason=PrivacyReason.POLICY_PUBLIC,
                policy_version="synthetic-public-v1",
                authority=Authority(cloud=False, external_egress=True),
            ),
        )
    )
    authority_root = tmp_path / "inflight-authority"
    authority_root.mkdir(mode=0o700)
    consent_path = authority_root / "provider-consent.json"
    brain_id, issuer_epoch = current_brain_identity(tasks)
    consent = DurableProviderConsentStore(
        consent_path, brain_id=brain_id, issuer_epoch=issuer_epoch
    )
    consent.grant(
        provider_id="synthetic-provider",
        allowed_tiers=frozenset({PrivacyTier.PUBLIC}),
        operation_id="grant-inflight",
        decided_at="2026-09-23T12:00:00Z",
        consent_id_factory=lambda: "consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    policy = _session_policy_file(
        tasks,
        authority_root / "external.json",
        read_tiers=["public"],
        external=True,
        generation=1,
    )
    trusted = TrustedSessionAuthority(tasks, policy_path=policy, consent_state_path=consent_path)
    authority = trusted.load()
    adapter = LocalMcpAdapter(
        authority=authority,
        negotiated=T03AppAdapter(tasks=tasks, authority=authority),
        revalidate_authority=lambda: trusted.revalidate(authority),
    )
    invoke = T03AppAdapter.invoke

    def revoke_after_read(self: T03AppAdapter, *args: Any, **kwargs: Any) -> dict[str, object]:
        result = invoke(self, *args, **kwargs)
        assert "Synthetic inflight consent canary" in json.dumps(result)
        consent.revoke(
            consent_id="consent_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            operation_id="revoke-inflight",
            decided_at="2026-09-23T12:01:00Z",
        )
        return result

    monkeypatch.setattr(T03AppAdapter, "invoke", revoke_after_read)
    incoming = b"".join(
        json.dumps(message).encode() + b"\n"
        for message in (
            INITIALIZE,
            _call("brain_search_page", {"dto_version": 1, "query": "canary"}),
        )
    )
    outgoing = io.BytesIO()
    refused = False
    try:
        serve_stdio_mcp(adapter, input_stream=io.BytesIO(incoming), output_stream=outgoing)
    except LauncherPolicyError as error:
        assert str(error) == "stale_policy"
        refused = True
    assert b"Synthetic inflight consent canary" not in outgoing.getvalue()
    assert refused


def _destination_adapter(tasks: Any, policy: str) -> LocalMcpAdapter:
    from open_brain.services.local_operations import (
        destination_bound_authority,
        destination_bound_capture_submit,
    )

    authority = destination_bound_authority(tasks, policy)
    return LocalMcpAdapter(
        authority=authority,
        capture_submit=destination_bound_capture_submit(tasks, authority),
    )


def test_capture_submit_entrypoint_reuses_one_policy_authority(
    tasks: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    policy_path = tmp_path / "launcher-policy.json"
    policy_path.write_text(_destination_policy(tasks, capabilities=[]), encoding="utf-8")

    def capture_submit(_tasks: object, authority: object) -> object:
        from open_brain.services.local_operations import (
            DestinationBoundCaptureCapability,
        )

        observed["capture_authority"] = authority
        return DestinationBoundCaptureCapability(
            cast(Any, _tasks), cast(EffectiveAuthority, authority)
        )

    def serve(adapter: LocalMcpAdapter, **_arguments: object) -> None:
        observed["adapter"] = adapter

    monkeypatch.setattr(
        "open_brain.services.local_entrypoints.destination_bound_capture_submit",
        capture_submit,
    )
    monkeypatch.setattr("open_brain.services.mcp_protocol.serve_stdio_mcp", serve)

    assert (
        run_cli(
            (
                "mcp",
                "--allow-capture-submit",
                "--capture-policy",
                str(policy_path),
                "--data-dir",
                str(tasks.profile.root),
            ),
            environment={"HOME": str(tasks.profile.root.parent)},
        )
        == 0
    )
    adapter = cast(LocalMcpAdapter, observed["adapter"])
    assert adapter.authority is observed["capture_authority"]
    assert "capture-submit" in adapter.authority.capabilities
    assert {tool["name"] for tool in adapter.list_tools()} == {
        "brain_catalog",
        "brain_capture_submit",
    }


def test_capture_submit_rejects_distinct_or_broader_injected_authority(tasks: Any) -> None:
    from open_brain.services.local_operations import (
        destination_bound_authority,
        destination_bound_capture_submit,
    )

    authority = destination_bound_authority(
        tasks, _destination_policy(tasks, allowed_capture_tiers=["public"])
    )
    equal_but_distinct = replace(authority)
    with pytest.raises(ValueError, match="invalid MCP capture-submit authority"):
        LocalMcpAdapter(
            authority=authority,
            capture_submit=destination_bound_capture_submit(tasks, equal_but_distinct),
        )

    broader = replace(
        authority,
        allowed_capture_tiers=frozenset({PrivacyTier.PUBLIC, PrivacyTier.SECRET}),
    )
    with pytest.raises(ValueError, match="invalid MCP capture-submit authority"):
        LocalMcpAdapter(
            authority=authority,
            capture_submit=destination_bound_capture_submit(tasks, broader),
        )


def test_local_mcp_rejects_missing_authority(tasks: Any) -> None:
    with pytest.raises(ValueError, match="invalid MCP authority"):
        LocalMcpAdapter(authority=cast(Any, None), capture=mcp_capture_sink(tasks))


def test_capture_submit_is_listed_only_when_the_grant_is_injected(tasks: Any) -> None:
    assert "brain_capture_submit" not in {tool["name"] for tool in _adapter(tasks).list_tools()}
    adapter = _destination_adapter(tasks, _destination_policy(tasks))
    assert {tool["name"] for tool in adapter.list_tools()} == {
        "brain_catalog",
        "brain_capture_submit",
    }
    schema = adapter.list_tools()[0]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["text"]
    assert set(schema["properties"]) == {"text", "idempotency_key", "privacy_tier"}


def test_capture_submit_binds_the_requested_tier_and_authority(tasks: Any) -> None:
    from open_brain_engine.core.access_contracts import derive_brain_id

    adapter = _destination_adapter(tasks, _destination_policy(tasks))
    responses = _wire(
        adapter,
        INITIALIZE,
        _call(
            "brain_capture_submit",
            {
                "text": "synthetic destination-bound mcp token",
                "idempotency_key": "destination-1",
                "privacy_tier": "work",
            },
        ),
        _call("brain_capture_submit", {"text": "synthetic destination unkeyed"}),
    )
    first = responses[1]["result"]["structuredContent"]
    assert first["status"] == "captured"
    assert first["requested_tier"] == "work"
    assert first["final_admitted_tier"] == "work"
    assert first["delivery_id"].startswith("delivery.mcp.destination.key.")
    assert len(cast(str, first["request_sha256"])) == 64
    assert first["destination_brain_id"] == derive_brain_id(cast(str, tasks.profile.tenant_id))
    assert isinstance(first["issuer_epoch"], int)
    second = responses[2]["result"]["structuredContent"]
    assert second["status"] == "captured"
    assert second["requested_tier"] == "unknown"
    assert second["final_admitted_tier"] == "unknown"


def test_capture_submit_replay_is_idempotent_and_out_of_set_tiers_are_refused(
    tasks: Any,
) -> None:
    adapter = _destination_adapter(
        tasks, _destination_policy(tasks, allowed_capture_tiers=["work"])
    )
    first = adapter.call_tool(
        "brain_capture_submit",
        {
            "text": "synthetic destination replay token",
            "idempotency_key": "destination-2",
            "privacy_tier": "work",
        },
    )
    repeated = adapter.call_tool(
        "brain_capture_submit",
        {
            "text": "synthetic destination replay token",
            "idempotency_key": "destination-2",
            "privacy_tier": "work",
        },
    )
    assert first["status"] == "captured"
    assert repeated == {**first, "duplicate": True}
    with pytest.raises(McpCallError, match="^tier_not_permitted$"):
        adapter.call_tool(
            "brain_capture_submit",
            {"text": "synthetic destination denied tier", "privacy_tier": "secret"},
        )
    denied = _wire(
        adapter,
        INITIALIZE,
        _call(
            "brain_capture_submit",
            {"text": "synthetic destination wire denied tier", "privacy_tier": "secret"},
        ),
    )
    assert denied[1]["result"] == {
        "content": [{"type": "text", "text": "tier_not_permitted"}],
        "isError": True,
    }
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool("brain_capture_submit", {"text": "synthetic", "privacy_tier": 7})


def test_capture_submit_refuses_stale_epoch_and_wrong_brain_policies(tasks: Any) -> None:
    from open_brain.services.launcher_policy import LauncherPolicyError
    from open_brain.services.local_operations import destination_bound_authority

    with pytest.raises(LauncherPolicyError, match="issuer_mismatch"):
        destination_bound_authority(tasks, _destination_policy(tasks, issuer_epoch=99))
    with pytest.raises(LauncherPolicyError, match="destination_mismatch"):
        destination_bound_authority(tasks, _destination_policy(tasks, brain_id="brn_" + "q" * 26))


FOLLOWUP_ITEM = "followup_3e6e8e2c-e638-47c6-8195-4bd6f306d67b"
FOLLOWUP_EVIDENCE = {
    "capture_id": REVIEW_CAPTURE,
    "revision_id": REVIEW_CAPTURE,
    "quote_start": 0,
    "quote_end": 5,
    "quote_sha256": "b" * 64,
    "role": "origin",
}
FOLLOWUP_DECIDE = {
    "operation_id": "op",
    "item_id": FOLLOWUP_ITEM,
    "expected_version": 1,
    "action": "confirm",
}


def _followup_ok(_arguments: Mapping[str, object]) -> dict[str, object]:
    return {"outcome": "ok", "data": {"item": {"item_id": FOLLOWUP_ITEM}}, "code": None}


def test_followup_capabilities_are_independently_listed_and_enforced() -> None:
    readers = LocalMcpAdapter(followup_list=_followup_ok, followup_show=_followup_ok)
    proposer = LocalMcpAdapter(followup_propose=_followup_ok, followup_add_evidence=_followup_ok)
    decider = LocalMcpAdapter(followup_decide=_followup_ok)
    assert {tool["name"] for tool in readers.list_tools()} == {
        "brain_catalog",
        "brain_followup_list",
        "brain_followup_show",
    }
    assert {tool["name"] for tool in proposer.list_tools()} == {
        "brain_catalog",
        "brain_followup_propose",
        "brain_followup_add_evidence",
    }
    assert {tool["name"] for tool in decider.list_tools()} == {
        "brain_catalog",
        "brain_followup_decide",
    }
    for tool in (*readers.list_tools(), *proposer.list_tools(), *decider.list_tools()):
        if tool["name"] == "brain_catalog":
            continue
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "untrusted" in tool["description"]
    propose_arguments = {
        "operation_id": "op",
        "kind": "commitment",
        "summary": "Send the deck",
        "owner": "self",
        "evidence": [FOLLOWUP_EVIDENCE],
    }
    with pytest.raises(McpCallError, match="^unknown tool$"):
        readers.call_tool("brain_followup_propose", propose_arguments)
    with pytest.raises(McpCallError, match="^unknown tool$"):
        readers.call_tool("brain_followup_decide", FOLLOWUP_DECIDE)
    with pytest.raises(McpCallError, match="^unknown tool$"):
        proposer.call_tool("brain_followup_decide", FOLLOWUP_DECIDE)
    with pytest.raises(McpCallError, match="^unknown tool$"):
        proposer.call_tool("brain_followup_list", {})
    with pytest.raises(McpCallError, match="^unknown tool$"):
        decider.call_tool("brain_followup_propose", propose_arguments)
    assert readers.call_tool("brain_followup_list", {})["outcome"] == "ok"
    assert proposer.call_tool("brain_followup_propose", propose_arguments)["outcome"] == "ok"
    assert decider.call_tool("brain_followup_decide", FOLLOWUP_DECIDE)["outcome"] == "ok"


def test_followup_tools_validate_shapes_pass_refusals_and_enforce_quotas() -> None:
    def refused(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {"outcome": "refused", "data": {"current_version": 4}, "code": "version_conflict"}

    adapter = LocalMcpAdapter(followup_list=_followup_ok, followup_decide=refused)
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool("brain_followup_list", {"limit": "ten"})
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool("brain_followup_list", {"unexpected": True})
    assert (adapter._followup_read_calls, adapter._followup_write_calls) == (0, 0)
    result = adapter.call_tool("brain_followup_decide", FOLLOWUP_DECIDE)
    assert result == {
        "outcome": "refused",
        "data": {"current_version": 4},
        "code": "version_conflict",
    }
    assert adapter._followup_write_calls == 1
    adapter._followup_read_calls = MAX_FOLLOWUP_READ_CALLS - 1
    adapter.call_tool("brain_followup_list", {})
    with pytest.raises(McpCallError, match="^session_followup_read_limit$"):
        adapter.call_tool("brain_followup_list", {})
    adapter._followup_write_calls = MAX_FOLLOWUP_WRITE_CALLS
    with pytest.raises(McpCallError, match="^session_followup_write_limit$"):
        adapter.call_tool("brain_followup_decide", FOLLOWUP_DECIDE)
    adapter._followup_write_calls = 0
    adapter._followup_response_bytes = MAX_FOLLOWUP_RESPONSE_BYTES
    with pytest.raises(McpCallError, match="^session_followup_response_limit$"):
        adapter.call_tool("brain_followup_decide", FOLLOWUP_DECIDE)


def test_followup_tool_limits_match_the_engine_contracts() -> None:
    adapter = LocalMcpAdapter(
        followup_list=_followup_ok, followup_propose=_followup_ok, followup_decide=_followup_ok
    )
    tools = cast(
        dict[str, Any],
        {tool["name"]: tool["inputSchema"]["properties"] for tool in adapter.list_tools()},
    )
    propose = tools["brain_followup_propose"]
    decide = tools["brain_followup_decide"]
    corrections = decide["corrections"]["properties"]
    assert tools["brain_followup_list"]["project"]["maxLength"] == 128
    assert propose["project"]["maxLength"] == propose["owner_name"]["maxLength"] == 128
    assert propose["summary"]["maxLength"] == 280
    assert corrections["project"]["maxLength"] == corrections["owner_name"]["maxLength"] == 128
    assert decide["ticket_key"]["maxLength"] == 200
    assert decide["ticket_url"]["maxLength"] == 2048
    assert decide["expected_version"]["minimum"] == 1

    url = "https://example.test/"
    link = {**FOLLOWUP_DECIDE, "action": "link", "ticket_key": "k" * 200}
    assert adapter.call_tool(
        "brain_followup_decide", {**link, "ticket_url": url + "u" * (2048 - len(url))}
    )["outcome"] == "ok"
    for arguments in (
        {**link, "ticket_url": url + "u" * (2049 - len(url))},
        {**link, "ticket_key": "k" * 201},
    ):
        with pytest.raises(McpCallError, match="^invalid tool arguments$"):
            adapter.call_tool("brain_followup_decide", arguments)
    assert adapter.call_tool("brain_followup_list", {"project": "p" * 128})["outcome"] == "ok"
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool("brain_followup_list", {"project": "p" * 129})
    base = {
        "operation_id": "op",
        "kind": "commitment",
        "summary": "Send the deck",
        "owner": "other",
        "evidence": [FOLLOWUP_EVIDENCE],
    }
    assert adapter.call_tool(
        "brain_followup_propose", {**base, "owner_name": "n" * 128, "project": "p" * 128}
    )["outcome"] == "ok"
    for proposal in (
        {**base, "owner_name": "n" * 129},
        {**base, "owner_name": "n", "project": "p" * 129},
    ):
        with pytest.raises(McpCallError, match="^invalid tool arguments$"):
            adapter.call_tool("brain_followup_propose", proposal)


def test_followup_tools_are_registered_with_their_grants() -> None:
    registered = {cast(str, tool["name"]): tool for tool in MCP_REGISTERED_TOOLS}
    assert registered["brain_followup_list"]["required_grants"] == ["followup-read"]
    assert registered["brain_followup_show"]["required_grants"] == ["followup-read"]
    assert registered["brain_followup_propose"]["required_grants"] == ["followup-propose"]
    assert registered["brain_followup_add_evidence"]["required_grants"] == ["followup-propose"]
    assert registered["brain_followup_decide"]["required_grants"] == ["followup-decide"]


@pytest.mark.parametrize(
    ("flags", "tools"),
    (
        (("--allow-followup-read",), {"brain_followup_list", "brain_followup_show"}),
        (
            ("--allow-followup-propose",),
            {"brain_followup_propose", "brain_followup_add_evidence"},
        ),
        (("--allow-followup-decide",), {"brain_followup_decide"}),
    ),
)
def test_live_followup_stdio_lists_only_granted_tools(
    tasks: Any, flags: tuple[str, ...], tools: set[str]
) -> None:
    process = _start(tasks.profile.root, *flags)
    try:
        assert "result" in _exchange(process, INITIALIZE)
        listed = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in listed["result"]["tools"]} == {"brain_catalog", *tools}
        denied_name = next(
            name
            for name in (
                "brain_followup_decide",
                "brain_followup_propose",
                "brain_followup_list",
            )
            if name not in tools
        )
        denied = _exchange(process, _call(denied_name, {}, 3))
        assert denied["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
        if "brain_followup_list" in tools:
            listed_items = _exchange(process, _call("brain_followup_list", {}, 4))
            payload = listed_items["result"]["structuredContent"]
            assert payload == {
                "outcome": "ok",
                "data": {"items": [], "next_cursor": None, "truncated": False},
                "code": None,
            }
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_mcp_without_any_grant_still_refuses_and_followup_flags_count_as_grants(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    assert run_cli(("mcp", "--data-dir", str(root))) == 2
    capsys.readouterr()
    assert run_cli(("mcp", "--help", "--allow-followup-read"), environment={}) == 0
    output = capsys.readouterr().out
    for phrase in ("--allow-followup-read", "--allow-followup-propose", "--allow-followup-decide"):
        assert phrase in output


def _followup_fixture_items(tasks: Any) -> tuple[str, str]:
    """One Work and one Personal item proposed by the owner; returns their item IDs."""
    from hashlib import sha256

    from open_brain_engine.engine.followup_contracts import (
        EvidenceRef,
        EvidenceRole,
        FollowupKind,
        OwnerKind,
        ProposeRequest,
    )

    from open_brain.services.t03_adapters import owner_authority

    owner = owner_authority(tasks, session_id="owner-fixture")
    ids: list[str] = []
    for tier, text, operation in (
        (PrivacyTier.WORK, "Synthetic work promise: send the deck", "op-work"),
        (PrivacyTier.PERSONAL, "Synthetic personal promise: call the dentist", "op-personal"),
    ):
        capture_id = tasks.capture.accept(
            TextPayload(text), delivery_id=operation, privacy_tier=tier
        ).capture_id
        start = text.index("promise")
        end = start + len("promise")
        ref = EvidenceRef(
            capture_id,
            capture_id,
            start,
            end,
            sha256(text[start:end].encode("utf-8")).hexdigest(),
            EvidenceRole.ORIGIN,
        )
        item = tasks.followups.propose(
            ProposeRequest(
                operation_id=operation,
                kind=FollowupKind.COMMITMENT,
                summary=text,
                project=None,
                owner=OwnerKind.SELF,
                owner_name=None,
                due=None,
                due_evidence_index=None,
                evidence=(ref,),
            ),
            authority=owner,
        )
        ids.append(item.item_id)
    return ids[0], ids[1]


def test_unscoped_followup_mcp_is_work_only_while_owner_cli_sees_all(
    tasks: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    work_item, personal_item = _followup_fixture_items(tasks)
    root = tasks.profile.root

    assert run_cli(("followup", "list", "--json", "--data-dir", str(root))) == 0
    owner_view = json.loads(capsys.readouterr().out)
    assert {row["item_id"] for row in owner_view["data"]["items"]} == {work_item, personal_item}

    # A review grant beside the follow-up grant makes the session owner-authority; the
    # follow-up callbacks must still be clamped to Work.
    for flags in (("--allow-followup-read",), ("--allow-followup-read", "--allow-review-read")):
        process = _start(root, *flags)
        try:
            assert "result" in _exchange(process, INITIALIZE)
            listed = _exchange(process, _call("brain_followup_list", {}, 2))
            payload = listed["result"]["structuredContent"]
            assert payload["outcome"] == "ok"
            assert [row["item_id"] for row in payload["data"]["items"]] == [work_item]
            shown = _exchange(
                process, _call("brain_followup_show", {"item_id": personal_item}, 3)
            )
            assert shown["result"]["structuredContent"] == {
                "outcome": "refused",
                "data": None,
                "code": "not_found",
            }
            visible = _exchange(process, _call("brain_followup_show", {"item_id": work_item}, 4))
            assert visible["result"]["structuredContent"]["outcome"] == "ok"
            assert visible["result"]["structuredContent"]["data"]["decisions"]
            assert "principal_id" not in json.dumps(visible)
            assert str(tasks.profile.owner_actor_id) not in json.dumps(visible)
            assert process.stdin is not None
            process.stdin.close()
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_scoped_followup_tools_require_the_session_grant() -> None:
    scoped = EffectiveAuthority(
        "mcp-test-agent", "mcp-test-session", frozenset({"followup-read"}), None
    )
    adapter = LocalMcpAdapter(
        authority=scoped,
        followup_list=_followup_ok,
        followup_show=_followup_ok,
        followup_decide=_followup_ok,
    )
    assert {tool["name"] for tool in adapter.list_tools()} == {
        "brain_catalog",
        "brain_followup_list",
        "brain_followup_show",
    }
    with pytest.raises(McpCallError, match="^unknown tool$"):
        adapter.call_tool("brain_followup_decide", FOLLOWUP_DECIDE)


def _brief_ok(_arguments: Mapping[str, object]) -> dict[str, object]:
    return {
        "outcome": "ok",
        "data": {"items": [], "nothing_needs_attention": True, "model": "none", "saved": True},
        "code": None,
    }


BRIEF_ACK = {"family_ref": "family:consumer:synthetic:space_3e6e8e2c-e638-47c6-8195-4bd6f3000001"}


def test_brief_capabilities_are_independently_listed_and_enforced() -> None:
    reader = LocalMcpAdapter(brief=_brief_ok)
    acker = LocalMcpAdapter(brief_ack=_brief_ok)
    both = LocalMcpAdapter(brief=_brief_ok, brief_ack=_brief_ok)
    assert {tool["name"] for tool in reader.list_tools()} == {"brain_catalog", "brain_brief"}
    assert {tool["name"] for tool in acker.list_tools()} == {"brain_catalog", "brain_brief_ack"}
    tools = {tool["name"]: tool for tool in both.list_tools()}
    assert set(tools) == {"brain_catalog", "brain_brief", "brain_brief_ack"}
    assert "brain_brief_last" not in tools
    for name, tool in tools.items():
        if name == "brain_catalog":
            continue
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "untrusted" in tool["description"]
        assert "no model" in tool["description"] or "state changes" in tool["description"]
    brief_schema: Any = tools["brain_brief"]["inputSchema"]
    assert set(brief_schema["properties"]) == {"space_ids", "stale_days"}
    assert brief_schema["required"] == []
    assert brief_schema["properties"]["stale_days"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 365,
    }
    assert brief_schema["properties"]["space_ids"]["maxItems"] == 32
    assert brief_schema["properties"]["space_ids"]["items"]["pattern"].startswith("^space_")
    ack_schema: Any = tools["brain_brief_ack"]["inputSchema"]
    assert ack_schema["required"] == ["family_ref"]
    assert ack_schema["properties"]["family_ref"]["pattern"].startswith("^family:")
    with pytest.raises(McpCallError, match="^unknown tool$"):
        reader.call_tool("brain_brief_ack", BRIEF_ACK)
    with pytest.raises(McpCallError, match="^unknown tool$"):
        acker.call_tool("brain_brief", {})
    with pytest.raises(McpCallError, match="^unknown tool$"):
        both.call_tool("brain_brief_last", {})
    assert reader.call_tool("brain_brief", {})["outcome"] == "ok"
    assert acker.call_tool("brain_brief_ack", BRIEF_ACK)["outcome"] == "ok"


def test_brief_tools_validate_shapes_pass_refusals_and_enforce_quotas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {"outcome": "refused", "data": None, "code": "version_conflict"}

    adapter = LocalMcpAdapter(brief=_brief_ok, brief_ack=refused)
    arguments: Mapping[str, object]
    for arguments in (
        {"stale_days": 0},
        {"stale_days": 366},
        {"stale_days": "7"},
        {"space_ids": []},
        {"space_ids": ["not-a-space"]},
        {"unexpected": True},
    ):
        with pytest.raises(McpCallError, match="^invalid tool arguments$"):
            adapter.call_tool("brain_brief", arguments)
    for arguments in (
        {},
        {"family_ref": "followup:x"},
        {"family_ref": "family:"},
        {**BRIEF_ACK, "x": 1},
    ):
        with pytest.raises(McpCallError, match="^invalid tool arguments$"):
            adapter.call_tool("brain_brief_ack", arguments)
    assert (adapter._brief_read_calls, adapter._brief_ack_calls) == (0, 0)
    assert adapter.call_tool("brain_brief_ack", BRIEF_ACK) == {
        "outcome": "refused",
        "data": None,
        "code": "version_conflict",
    }
    assert adapter._brief_ack_calls == 1
    adapter._brief_read_calls = MAX_BRIEF_READ_CALLS - 1
    adapter.call_tool(
        "brain_brief", {"space_ids": [BRIEF_ACK["family_ref"].rsplit(":", 1)[1]], "stale_days": 30}
    )
    with pytest.raises(McpCallError, match="^session_brief_read_limit$"):
        adapter.call_tool("brain_brief", {})
    adapter._brief_ack_calls = MAX_BRIEF_ACK_CALLS
    with pytest.raises(McpCallError, match="^session_brief_ack_limit$"):
        adapter.call_tool("brain_brief_ack", BRIEF_ACK)
    # One encoded brief response is about 330 bytes, so 512 admits one call and refuses the next.
    monkeypatch.setattr(local_mcp_module, "MAX_BRIEF_RESPONSE_BYTES", 512)
    adapter._brief_read_calls = 0
    adapter._brief_response_bytes = 0
    adapter.call_tool("brain_brief", {})
    with pytest.raises(McpCallError, match="^session_brief_response_limit$"):
        adapter.call_tool("brain_brief", {})


def test_brief_tools_are_registered_with_their_grants() -> None:
    registered = {cast(str, tool["name"]): tool for tool in MCP_REGISTERED_TOOLS}
    assert registered["brain_brief"]["required_grants"] == ["brief-read"]
    assert registered["brain_brief_ack"]["required_grants"] == ["brief-ack"]
    assert "brain_brief_last" not in registered


def test_scoped_brief_tools_require_the_session_grant() -> None:
    scoped = EffectiveAuthority(
        "mcp-test-agent", "mcp-test-session", frozenset({"brief-read"}), None
    )
    adapter = LocalMcpAdapter(authority=scoped, brief=_brief_ok, brief_ack=_brief_ok)
    assert {tool["name"] for tool in adapter.list_tools()} == {"brain_catalog", "brain_brief"}
    with pytest.raises(McpCallError, match="^unknown tool$"):
        adapter.call_tool("brain_brief_ack", BRIEF_ACK)
    assert adapter.call_tool("brain_brief", {})["outcome"] == "ok"


@pytest.mark.parametrize(
    ("flags", "tools"),
    (
        (("--allow-brief-read",), {"brain_brief"}),
        (("--allow-brief-ack",), {"brain_brief_ack"}),
        (("--allow-brief-read", "--allow-brief-ack"), {"brain_brief", "brain_brief_ack"}),
    ),
)
def test_live_brief_stdio_lists_only_granted_tools(
    tasks: Any, flags: tuple[str, ...], tools: set[str]
) -> None:
    process = _start(tasks.profile.root, *flags)
    try:
        assert "result" in _exchange(process, INITIALIZE)
        listed = _exchange(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert {tool["name"] for tool in listed["result"]["tools"]} == {"brain_catalog", *tools}
        denied_name = next(
            name
            for name in ("brain_brief_ack", "brain_brief", "brain_brief_last")
            if name not in tools
        )
        denied = _exchange(process, _call(denied_name, {}, 3))
        assert denied["result"]["content"] == [{"type": "text", "text": "unknown tool"}]
        if "brain_brief" in tools:
            reply = _exchange(process, _call("brain_brief", {}, 4))
            payload = reply["result"]["structuredContent"]
            assert payload["outcome"] == "ok" and payload["code"] is None
            data = payload["data"]
            assert data["items"] == [] and data["nothing_needs_attention"] is True
            assert data["model"] == "none" and data["saved"] is True and data["since"] is None
            assert data["coverage"]["followups"] == "not_granted"
            assert data["coverage"]["freshness"] == "read"
            assert data["scope"]["tiers"] == ["work"]
            again = _exchange(process, _call("brain_brief", {"stale_days": 30}, 5))
            assert again["result"]["structuredContent"]["data"]["since"] == data["generated_at"]
        if "brain_brief_ack" in tools:
            acked = _exchange(
                process, _call("brain_brief_ack", {"family_ref": "family:consumer:none:x"}, 6)
            )
            # The flags are independent, but an ack needs brief-read too: ack-only is refused
            # before any read, and read+ack reaches the family lookup.
            assert acked["result"]["structuredContent"] == {
                "outcome": "refused",
                "data": None,
                "code": "not_found" if "brain_brief" in tools else "unsupported_capability",
            }
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_brief_flags_count_as_grants_and_are_documented_in_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    assert run_cli(("mcp", "--data-dir", str(root))) == 2
    capsys.readouterr()
    assert run_cli(("mcp", "--help", "--allow-brief-read"), environment={}) == 0
    output = " ".join(capsys.readouterr().out.split())  # argparse wraps the description
    for phrase in (
        "--allow-brief-read",
        "--allow-brief-ack",
        "200 brief reads",
        "brief grants are independent",
    ):
        assert phrase in output, phrase


def test_brief_followup_items_are_work_only_and_owner_grants_stay_clamped(tasks: Any) -> None:
    work_item, personal_item = _followup_fixture_items(tasks)
    # The second session clamps to the same Work-only scope and grants as the first, so it
    # shares the first session's visibility key and sees the item as still open.
    for flags, expect_followups, expect_changed in (
        (("--allow-brief-read", "--allow-followup-read"), "read", "new"),
        (
            ("--allow-brief-read", "--allow-followup-read", "--allow-review-read"),
            "read",
            "still_open",
        ),
        (("--allow-brief-read", "--allow-review-read"), "not_granted", None),
    ):
        process = _start(tasks.profile.root, *flags)
        try:
            assert "result" in _exchange(process, INITIALIZE)
            reply = _exchange(process, _call("brain_brief", {}, 2))
            payload = reply["result"]["structuredContent"]
            assert payload["outcome"] == "ok", (flags, payload)
            data = payload["data"]
            assert data["coverage"]["followups"] == expect_followups, flags
            assert data["coverage"]["freshness"] == "read", flags
            refs = [item["ref"] for item in data["items"]]
            if expect_followups == "read":
                assert refs == ["followup:" + work_item], flags
                assert data["items"][0]["kind"] == "followup_open"
                assert data["items"][0]["evidence"]
                assert data["items"][0]["changed"] == expect_changed, flags
            else:
                assert refs == []
            encoded = json.dumps(payload)
            assert personal_item not in encoded and "withheld" not in encoded
            assert "Synthetic" not in encoded, "a brief never carries summaries or quotes"
            assert str(tasks.profile.owner_actor_id) not in encoded
            assert data["scope"]["tiers"] == ["work"]
            assert process.stdin is not None
            process.stdin.close()
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize(
    "code", ["review_target_stale", "replacement_contains_redaction", "duplicate_page"]
)
def test_review_safe_errors_survive_mcp_transport(code: str) -> None:
    def fail(_arguments: Mapping[str, object]) -> dict[str, object]:
        raise ReviewPublicationError(code)

    adapter = LocalMcpAdapter(review_approve=fail)
    reply = _wire(
        adapter,
        INITIALIZE,
        _call(
            "brain_review_approve",
            {"proposal_id": REVIEW_PROPOSAL, "review_token": REVIEW_DIGEST},
        ),
    )[1]
    assert reply["result"]["isError"] is True
    assert reply["result"]["content"] == [{"type": "text", "text": code}]


def test_only_publishing_decisions_accept_allow_duplicate() -> None:
    def operation(_arguments: Mapping[str, object]) -> dict[str, object]:
        return {"status": "ok"}

    adapter = LocalMcpAdapter(
        review_approve=operation, review_reject=operation, review_edit_and_approve=operation
    )
    properties = {
        tool["name"]: cast(dict[str, Any], tool["inputSchema"])["properties"]
        for tool in adapter.list_tools()
    }
    assert properties["brain_review_approve"]["allow_duplicate"] == {"type": "boolean"}
    assert properties["brain_review_edit_and_approve"]["allow_duplicate"] == {"type": "boolean"}
    assert "allow_duplicate" not in properties["brain_review_reject"]
