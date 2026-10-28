from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import TextPayload, open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain.services.compact_search import CompactSearchAppAdapter
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import LocalMcpAdapter
from open_brain.services.mcp_protocol import (
    COMPACT_SEARCH_TEXT_PREFIX,
    McpCallError,
    encoded_tool_response_size,
    serve_stdio_mcp,
)
from open_brain.services.t03_adapters import T03AppAdapter, owner_authority


@pytest.fixture
def tasks(tmp_path: Path) -> Any:
    return open_local_engine(compile_single_user_local(tmp_path / "brain"))


def _adapter(tasks: Any) -> LocalMcpAdapter:
    authority = owner_authority(tasks, session_id="synthetic-compact-mcp")
    negotiated = T03AppAdapter(tasks, authority)
    return LocalMcpAdapter(
        authority=authority,
        negotiated=negotiated,
        compact_search=CompactSearchAppAdapter(
            tasks.retrieval.compact_search_page,
            authority,
            negotiated.budget,
        ),
    )


def _members(page: dict[str, Any]) -> list[str]:
    return [member["record_id"] for row in page["results"] for member in row["members"]]


def test_actual_mcp_budget_reflows_successor_from_final_member(tasks: Any) -> None:
    title = 'synthetic compact "quoted" 🙂 漢字 '
    expected = [
        tasks.capture.accept(
            TextPayload(title + "\nUntrusted body text that must not appear in compact results."),
            delivery_id=f"synthetic.compact.transport.{index}",
        ).capture_id
        for index in range(30)
    ]
    adapter = _adapter(tasks)
    arguments: dict[str, Any] = {
        "dto_version": 1,
        "query": "synthetic compact",
        "view": "compact",
        "limit": 20,
        "target_bytes": 8192,
    }
    request_id = '"🙂' * 20
    maximum = 2600
    first = adapter.call_tool(
        "brain_search", arguments, request_id=request_id, maximum_response_bytes=maximum
    )
    assert 0 < len(_members(first)) < 20
    assert len(portable_canonical_json_bytes(first)) <= 8192
    assert encoded_tool_response_size(request_id, first) <= maximum
    first_cursor = first["next_cursor"]
    assert first_cursor is not None
    found = _members(first)
    pages: list[dict[str, Any]] = [first]
    while not pages[-1]["complete"]:
        page = adapter.call_tool(
            "brain_search_page",
            {**arguments, "cursor": pages[-1]["next_cursor"]},
            request_id=request_id,
            maximum_response_bytes=maximum,
        )
        assert encoded_tool_response_size(request_id, page) <= maximum
        found.extend(_members(page))
        pages.append(page)
    assert sorted(found) == sorted(expected)
    assert len(found) == len(set(found))
    assert all(page["total_matches"] == 30 and page["total_groups"] == 1 for page in pages)
    assert len({row["group_id"] for page in pages for row in page["results"]}) == 1
    offsets = [row["member_offset"] for page in pages for row in page["results"]]
    assert offsets == [
        sum(len(_members(page)) for page in pages[:index]) for index in range(len(pages))
    ]
    replay = adapter.call_tool(
        "brain_search",
        {**arguments, "cursor": first_cursor},
        request_id=request_id,
        maximum_response_bytes=maximum,
    )
    assert _members(replay) == _members(pages[1])
    encoded = json.dumps(pages)
    assert "Untrusted body text" not in encoded
    assert "excerpt" not in encoded
    assert "source_id" not in encoded
    assert adapter.negotiated is not None
    assert adapter.negotiated.budget.content_bytes == sum(
        encoded_tool_response_size(request_id, page) for page in [*pages, replay]
    )


def test_tiny_transport_refuses_without_dropping_first_member(tasks: Any) -> None:
    tasks.capture.accept(TextPayload("synthetic compact one"), delivery_id="synthetic.compact.tiny")
    adapter = _adapter(tasks)
    arguments = {"dto_version": 1, "query": "compact", "view": "compact"}
    with pytest.raises(McpCallError, match="^response_too_large$"):
        adapter.call_tool("brain_search", arguments, maximum_response_bytes=400)
    first = adapter.call_tool("brain_search_page", arguments)
    assert len(_members(first)) == 1
    assert first["complete"] is True


def test_stdio_compact_structured_and_text_content_are_identical(tasks: Any) -> None:
    tasks.capture.accept(
        TextPayload("synthetic compact wire"), delivery_id="synthetic.compact.wire"
    )
    messages = [
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
            "params": {
                "name": "brain_search",
                "arguments": {
                    "dto_version": 1,
                    "query": "synthetic compact",
                    "view": "compact",
                },
            },
        },
    ]
    outgoing = io.BytesIO()
    serve_stdio_mcp(
        _adapter(tasks),
        input_stream=io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in messages)),
        output_stream=outgoing,
        maximum_message_bytes=4096,
    )
    lines = outgoing.getvalue().splitlines()
    response = json.loads(lines[-1])["result"]
    text = response["content"][0]["text"]
    assert text.startswith(COMPACT_SEARCH_TEXT_PREFIX)
    assert response["structuredContent"] == json.loads(
        text.removeprefix(COMPACT_SEARCH_TEXT_PREFIX)
    )
    assert response["structuredContent"]["view"] == "compact"
    assert len(lines[-1]) <= 4096


@pytest.mark.parametrize("command", ["search", "search-page"])
def test_cli_compact_defaults_counts_and_continuation(
    tasks: Any,
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = [
        tasks.capture.accept(
            TextPayload("synthetic compact CLI"), delivery_id=f"synthetic.compact.cli.{index}"
        ).capture_id
        for index in range(3)
    ]
    base = (
        command,
        "synthetic compact",
        "--compact",
        "--data-dir",
        str(tasks.profile.root),
        "--json",
    )
    assert run_cli((*base, "--limit", "2")) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["mode_used"] == "lexical"
    assert first["group_by"] == "title"
    assert first["total_matches"] == 3 and first["total_groups"] == 1
    assert len(_members(first)) == 2
    assert run_cli((*base, "--limit", "2", "--cursor", first["next_cursor"])) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["complete"] is True
    assert sorted(_members(first) + _members(second)) == sorted(expected)
    assert run_cli((*base, "--count-only", "--group-by", "none", "--target-bytes", "2048")) == 0
    count = json.loads(capsys.readouterr().out)
    assert count["results"] == [] and count["count_only"] is True
    assert count["total_matches"] == 3 and count["total_groups"] == 3
    assert count["complete"] is True and count["next_cursor"] is None


@pytest.mark.parametrize(
    "command,flag",
    [
        ("search", ("--group-by", "title")),
        ("search-page", ("--count-only",)),
        ("search-page", ("--target-bytes", "2048")),
        ("search", ("--cursor", "opaque")),
    ],
)
def test_new_compact_flags_require_opt_in_before_bootstrap(
    tmp_path: Path,
    command: str,
    flag: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "does-not-exist"
    assert run_cli((command, "synthetic", *flag, "--data-dir", str(root), "--json")) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": {"code": "invalid_arguments"},
    }
    assert not root.exists()


def test_core_factory_injects_compact_only_with_existing_search_grant(
    tasks: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[LocalMcpAdapter] = []

    def serve(adapter: LocalMcpAdapter, **_kwargs: object) -> None:
        observed.append(adapter)

    monkeypatch.setattr("open_brain.services.mcp_protocol.serve_stdio_mcp", serve)
    for flag in ("--allow-search", "--allow-content-read"):
        assert run_cli(("mcp", "--data-dir", str(tasks.profile.root), flag)) == 0
    compact = observed[0].compact_search
    assert compact is not None and compact.available
    assert observed[0].negotiated is not None
    assert compact.budget is observed[0].negotiated.budget
    assert compact.authority is observed[0].authority
    assert observed[1].compact_search is None


def test_cli_human_compact_output_sanitizes_hostile_titles(
    tasks: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tasks.capture.accept(
        TextPayload("synthetic compact \x1b[31m title"), delivery_id="synthetic.compact.terminal"
    )
    assert (
        run_cli(("search", "synthetic compact", "--compact", "--data-dir", str(tasks.profile.root)))
        == 0
    )
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert '"total_matches": 1' in output
    assert '"title"' in output
    assert '"excerpt"' not in output
