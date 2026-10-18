"""Owner-local embedding policy CLI grammar and exact JSON status shape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import BrainEngine

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS
from open_brain.services.plugin_bridge import (
    _BASE_OPERATIONS,
    _COLLECTOR_OPERATIONS,
    _NEGOTIATED_OPERATIONS,
)

COMPONENT = "0123456789abcdef" * 4


def _cli(root: Path, *args: str) -> int:
    return run_cli(
        (*args, "--data-dir", str(root), "--json"),
        platform_name="darwin",
        filesystem_type_probe=lambda _path, _platform: "apfs",
    )


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast(dict[str, object], json.loads(captured.out))


def test_status_enable_disable_json_shapes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    BrainEngine.open(compile_single_user_local(root))
    assert _cli(root, "embedding", "status") == 0
    status = _output(capsys)
    assert status.pop("embedding_health") is not None
    assert status == {
        "schema": "ob.embedding-policy-status.v1",
        "enabled": False,
        "component_id": None,
        "consent_version": 0,
    }
    assert _cli(root, "embedding", "enable", "--component-id", COMPONENT) == 0
    assert _output(capsys) == {
        "schema": "ob.embedding-policy-status.v1",
        "enabled": True,
        "component_id": COMPONENT,
        "consent_version": 1,
    }
    assert _cli(root, "embedding", "disable") == 0
    assert _output(capsys) == {
        "schema": "ob.embedding-policy-status.v1",
        "enabled": False,
        "component_id": None,
        "consent_version": 2,
    }
    assert _cli(root, "embedding", "status") == 0
    status = _output(capsys)
    assert status["consent_version"] == 2
    assert status["embedding_health"]["consent_state"] == "revoked"  # type: ignore[index]


@pytest.mark.parametrize("component", ["ABC", COMPONENT.upper(), COMPONENT + "0"])
def test_enable_rejects_malformed_component_before_opening(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], component: str
) -> None:
    root = tmp_path / "brain"
    assert _cli(root, "embedding", "enable", "--component-id", component) == 2
    assert _output(capsys) == {"error": {"code": "invalid_component_id"}}
    assert not root.exists()


def test_missing_brain_is_not_created(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "brain"
    assert _cli(root, "embedding", "status") == 78
    assert _output(capsys) == {"error": {"code": "local_operation_failed"}}
    assert not root.exists()


def test_policy_is_not_exposed_to_mcp_or_plugin_surfaces() -> None:
    names = {str(tool.get("name", "")) for tool in MCP_REGISTERED_TOOLS}
    operations = {*_BASE_OPERATIONS, *_NEGOTIATED_OPERATIONS, *_COLLECTOR_OPERATIONS}
    assert not any("embedding" in name for name in names | operations)
