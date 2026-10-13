from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from open_brain.services.harness_bridge import run_harness
from open_brain.services.harness_mcp import HarnessMcpAdapter
from open_brain.services.mcp_protocol import McpCallError, serve_stdio_mcp
from packages.app.tests.integration.services.test_harness_bridge import event, setup


def structured(response: dict[str, object]) -> dict[str, Any]:
    value = response["structuredContent"]
    assert isinstance(value, dict)
    return value


def test_mcp_search_source_and_current_revocation(tmp_path: Path) -> None:
    config, project, policy, configuration = setup(tmp_path)
    assert run_harness("enqueue", config, "codex", event(project))["status"] == "queued"
    drained = run_harness("drain", config, "codex")
    assert cast(list[dict[str, object]], drained["results"])[0]["status"] == "delivered", drained
    counts = cast(dict[str, int], run_harness("status", config, "codex")["counts"])
    assert counts["delivered"] == 1
    adapter = HarnessMcpAdapter(config, "claude-code", project)
    found = adapter.call_tool("brain_search_page", {"query": "editor"})
    assert found["isError"] is False
    results = structured(found)["results"]
    assert len(results) == 1
    capture_id = results[0]["capture_id"]
    assert json.loads(results[0]["citation"])["capture_id"] == capture_id
    source = adapter.call_tool("brain_read_source", {"capture_id": capture_id})
    assert source["isError"] is False
    assert structured(source)["results"][0]["source_context"] == (
        "My preferred editor is Vim."
    )
    policy["providers"]["claude-code"] = []
    Path(configuration["policy"]).write_text(json.dumps(policy))
    denied = adapter.call_tool("brain_read_source", {"capture_id": capture_id})
    assert denied["isError"] is True
    assert "Vim" not in json.dumps(denied)
    assert "results" not in structured(denied)


def test_mcp_initialization_never_opens_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("tool discovery must not open the Brain")

    monkeypatch.setattr("open_brain.services.harness_mcp.run_harness", forbidden)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "synthetic", "version": "1"},
        }},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    output = io.BytesIO()
    serve_stdio_mcp(
        HarnessMcpAdapter(tmp_path / "absent.json", "codex", tmp_path),
        input_stream=io.BytesIO(b"".join(json.dumps(row).encode() + b"\n" for row in messages)),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0]["result"]["capabilities"] == {"tools": {}}
    assert {tool["name"] for tool in rows[1]["result"]["tools"]} == {
        "brain_search_page", "brain_read_source",
    }


@pytest.mark.parametrize("extra", ["provider", "local_only", "project_path", "cwd"])
def test_mcp_arguments_cannot_widen_host_authority(tmp_path: Path, extra: str) -> None:
    adapter = HarnessMcpAdapter(tmp_path / "absent.json", "pi", tmp_path)
    with pytest.raises(McpCallError):
        adapter.call_tool("brain_search_page", {"query": "editor", extra: "local"})


def test_mcp_deadline_and_response_budget_have_visible_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain.services.harness_cli import HarnessDeadlineExceeded

    def expired(*args: object, **kwargs: object) -> None:
        raise HarnessDeadlineExceeded

    monkeypatch.setattr("open_brain.services.harness_mcp.run_harness", expired)
    adapter = HarnessMcpAdapter(tmp_path / "absent.json", "codex", tmp_path)
    response = adapter.call_tool("brain_search_page", {"query": "editor"})
    assert response["isError"] is True
    assert structured(response)["error_code"] == "bridge_timeout"
    monkeypatch.setattr("open_brain.services.harness_mcp.run_harness", lambda *a, **kw: {
        "status": "ok", "results": [{"text": "x" * 4096}],
    })
    response = adapter.call_tool("brain_search_page", {"query": "editor"},
                                 maximum_response_bytes=1024)
    assert response["isError"] is True
    assert structured(response)["error_code"] == "response_budget_exceeded"
