from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    CaptureAction,
    ManagedAccessMode,
    ManagedProvider,
    TextPayload,
    open_local_engine,
)

from open_brain.profile import open_existing_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import (
    MAX_GRAPH_INPUT_BYTES,
    MAX_GRAPH_MODEL_ATTEMPTS,
    MAX_GRAPH_REFRESH_CALLS,
    MAX_WORKSPACE_READ_CALLS,
    MAX_WORKSPACE_RESPONSE_BYTES,
    LocalMcpAdapter,
)
from open_brain.services.local_operations import (
    graph_suggestions,
    refresh_graph,
    workspace_status,
)
from open_brain.services.mcp_protocol import McpCallError


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def test_owner_cli_workspace_flow_uses_path_free_shared_read_projection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    assert (
        run_cli(
            ("init", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    capsys.readouterr()
    tasks = open_local_engine(open_existing_single_user_local(root))
    space_id = tasks.inbox.create_space("Notes", delivery_id="managed.cli.space").space_id
    first = tasks.capture.accept(
        TextPayload("First managed CLI note"),
        delivery_id="managed.cli.first",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
    )

    assert (
        run_cli(
            ("workspace", "setup", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    setup = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert setup["status"] == "setup"
    workspace = tmp_path / "Open Brain Vault"
    assert next(workspace.rglob("*.md")).is_file()

    second = tasks.capture.accept(
        TextPayload("Second managed CLI note"),
        delivery_id="managed.cli.second",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
    )
    assert (
        run_cli(
            ("workspace", "refresh", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "refreshed"
    assert len(tuple(workspace.rglob("*.md"))) == 2

    assert (
        run_cli(
            ("workspace", "status", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    status = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert status["active_notes"] == 2
    assert status["connected"] is True
    rendered = json.dumps(status)
    assert str(root) not in rendered
    assert str(workspace) not in rendered
    assert first.capture_id not in rendered
    assert second.capture_id not in rendered

    assert (
        run_cli(
            ("graph", "suggestions", "--data-dir", str(root), "--json"),
            filesystem_type_probe=_filesystem,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["suggestions"] == []


def test_mcp_workspace_capabilities_are_opt_in_bounded_and_non_owner() -> None:
    refresh_caps: list[tuple[int, int]] = []

    def refresh(attempts: int, input_bytes: int) -> tuple[dict[str, object], int, int]:
        refresh_caps.append((attempts, input_bytes))
        return {"status": "refreshed"}, 2, 1024

    adapter = LocalMcpAdapter(
        workspace_status=lambda: {"status": "ok"},
        graph_suggestions=lambda: {"status": "ok", "suggestions": []},
        graph_refresh=refresh,
    )
    assert {tool["name"] for tool in adapter.list_tools()} == {
        "brain_workspace_status",
        "brain_graph_suggestions",
        "brain_graph_refresh",
    }
    assert adapter.call_tool("brain_workspace_status", {}) == {"status": "ok"}
    assert adapter.call_tool("brain_graph_suggestions", {})["suggestions"] == []
    assert adapter.call_tool("brain_graph_refresh", {}) == {"status": "refreshed"}
    assert refresh_caps == [(MAX_GRAPH_MODEL_ATTEMPTS, MAX_GRAPH_INPUT_BYTES)]
    assert adapter._workspace_read_calls == 2
    assert adapter._graph_refresh_calls == 1
    assert adapter._graph_model_attempts == 2
    assert adapter._graph_input_bytes == 1024
    for forbidden in ("brain_graph_accept", "brain_workspace_resolve", "brain_consent_grant"):
        with pytest.raises(McpCallError, match="unknown tool"):
            adapter.call_tool(forbidden, {})

    with pytest.raises(McpCallError, match="invalid tool arguments"):
        adapter.call_tool("brain_workspace_status", {"path": "/private"})
    assert adapter._workspace_read_calls == 2

    adapter._workspace_read_calls = MAX_WORKSPACE_READ_CALLS
    with pytest.raises(McpCallError, match="session_workspace_read_limit"):
        adapter.call_tool("brain_workspace_status", {})
    adapter._workspace_read_calls = 0
    adapter._workspace_response_bytes = MAX_WORKSPACE_RESPONSE_BYTES
    with pytest.raises(McpCallError, match="session_workspace_read_limit"):
        adapter.call_tool("brain_workspace_status", {})
    adapter._graph_refresh_calls = MAX_GRAPH_REFRESH_CALLS
    with pytest.raises(McpCallError, match="session_graph_refresh_limit"):
        adapter.call_tool("brain_graph_refresh", {})


def test_shared_workspace_reads_match_mcp_projection(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    assert run_cli(("init", "--data-dir", str(root)), filesystem_type_probe=_filesystem) == 0
    tasks = open_local_engine(open_existing_single_user_local(root))
    adapter = LocalMcpAdapter(
        workspace_status=lambda: workspace_status(tasks),
        graph_suggestions=lambda: graph_suggestions(tasks),
    )

    assert adapter.call_tool("brain_workspace_status", {}) == workspace_status(tasks)
    assert adapter.call_tool("brain_graph_suggestions", {}) == graph_suggestions(tasks)


def test_deterministic_fake_provider_runs_through_shared_refresh_contract(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    assert run_cli(("init", "--data-dir", str(root)), filesystem_type_probe=_filesystem) == 0
    tasks = open_local_engine(open_existing_single_user_local(root))
    space_id = tasks.inbox.create_space("Notes", delivery_id="managed.fake.space").space_id
    for index, body in enumerate(
        ("Solar generation peaks at midday.", "Battery storage works after sunset.")
    ):
        tasks.capture.accept(
            TextPayload(body),
            delivery_id=f"managed.fake.capture.{index}",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=space_id,
        )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    setup = tasks.managed_workspace.setup(str(workspace), operation_id="managed.fake.setup")
    tasks.managed_policy.grant_consent(
        setup.workspace_id,
        ManagedProvider.OPENAI_API,
        ManagedAccessMode.API_KEY,
        operation_id="managed.fake.consent",
    )

    def fake_provider(
        prompt: str, max_output_bytes: int, timeout_seconds: int
    ) -> dict[str, object]:
        assert "Solar generation" in prompt
        assert "Battery storage" in prompt
        assert max_output_bytes == 16 * 1024
        assert timeout_seconds == 60
        sources = cast(list[dict[str, object]], json.loads(prompt)["selected_sources"])
        source = next(
            index
            for index, selected in enumerate(sources, start=1)
            if "Solar generation" in cast(str, selected["body"])
        )
        target = next(
            index
            for index, selected in enumerate(sources, start=1)
            if "Battery storage" in cast(str, selected["body"])
        )
        return {
            "source": source,
            "source_quote": "peaks at midday",
            "target": target,
            "target_quote": "after sunset",
        }

    adapter = LocalMcpAdapter(
        graph_refresh=lambda attempts, input_bytes: refresh_graph(
            tasks,
            provider=ManagedProvider.OPENAI_API,
            access_mode=ManagedAccessMode.API_KEY,
            adapter_identity="openai_api:deterministic-fake-v1",
            model="deterministic-fake-v1",
            request_id="request_00000000-0000-4000-8000-000000000301",
            invoke=fake_provider,
            remaining_attempts=attempts,
            remaining_input_bytes=input_bytes,
        )
    )

    result = adapter.call_tool("brain_graph_refresh", {})

    assert result["status"] == "refreshed"
    assert len(cast(list[object], graph_suggestions(tasks)["suggestions"])) == 1
    assert adapter._graph_refresh_calls == 1
    assert adapter._graph_model_attempts == 1
    assert 0 < adapter._graph_input_bytes <= MAX_GRAPH_INPUT_BYTES
