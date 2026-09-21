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
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import (
    CaptureReceipt,
    CaptureTask,
    DecisionOutcome,
    ProposalDraft,
    PublicJobCaptureSink,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.portable.v5 import V5_SIDECAR_PATHS
from open_brain_engine.storage.operational import FileLease

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import (
    MAX_MESSAGE_BYTES,
    MAX_ORGANIZATION_READ_CALLS,
    MAX_ORGANIZATION_RESPONSE_BYTES,
    MAX_ORGANIZATION_WRITE_CALLS,
    MAX_REVIEW_DECISION_CALLS,
    MAX_REVIEW_PROPOSAL_CALLS,
    MAX_REVIEW_READ_CALLS,
    MAX_REVIEW_RESPONSE_BYTES,
    LocalMcpAdapter,
)
from open_brain.services.local_operations import mcp_capture_sink, search_brain
from open_brain.services.mcp_protocol import (
    McpCallError,
    encoded_tool_response_size,
    serve_stdio_mcp,
)
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
        "whole-Brain",
        "network-backed",
        "provider",
        "durable",
        "0.1.0",
        "delete",
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
    assert json.loads(capsys.readouterr().out)["schema_version"] == 5
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
        assert denied["result"]["content"] == [{"type": "text", "text": "unsupported_capability"}]
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
        assert denied["result"]["content"] == [{"type": "text", "text": "unsupported_capability"}]
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
        with lease.acquire_shared_writer():
            started = time.monotonic()
            busy = _exchange(process, _call("brain_capture", {"text": "blocked-token"}))
            assert busy["result"]["content"][0]["text"] == "database_busy"
            assert run_cli(("capture", "blocked-cli", "--data-dir", str(root), "--json")) == 75
            assert json.loads(capsys.readouterr().out)["error"]["code"] == "database_busy"
            assert time.monotonic() - started < 10
        assert tasks.inbox.list() == ()
        cli = subprocess.Popen(
            [
                sys.executable,
                "-c",
                PROGRAM,
                "capture",
                "cli-parallel-nebula",
                "--data-dir",
                str(root),
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        mcp = _exchange(process, _call("brain_capture", {"text": "mcp-parallel-nebula"}))
        output, error = cli.communicate(timeout=15)
        assert cli.returncode in (0, 75), error
        assert mcp["result"].get("isError") is not True or (
            mcp["result"]["content"][0]["text"] == "database_busy"
        )
        if cli.returncode == 75:
            assert json.loads(output)["error"]["code"] == "database_busy"
        if mcp["result"].get("isError"):
            mcp = _exchange(process, _call("brain_capture", {"text": "mcp-parallel-nebula"}))
        assert mcp["result"]["structuredContent"]["status"] == "captured"
        assert run_cli(("search", "mcp-parallel-nebula", "--data-dir", str(root), "--json")) == 0
        assert json.loads(capsys.readouterr().out)["results"][0]["source_origin"] == "unknown"
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
            _exchange(process, _call("brain_search", {"query": "sqlite-blocked-token"}))["result"][
                "structuredContent"
            ]["results"]
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
    with pytest.raises(McpCallError, match="^capture privacy tier requires owner authority$"):
        adapter.call_tool(
            "brain_capture", {"text": "synthetic tiered capture", "privacy_tier": "work"}
        )
    with pytest.raises(McpCallError, match="^invalid tool arguments$"):
        adapter.call_tool("brain_capture", {"text": "synthetic", "privacy_tier": "synthetic-tier"})
    plain = adapter.call_tool("brain_capture", {"text": "synthetic plain mcp capture"})
    assert plain["status"] == "captured"
