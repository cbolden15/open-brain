"""Owner-local egress policy CLI grammar, idempotence and exact JSON status shape."""

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


def _cli(root: Path, *args: str, json_output: bool = True) -> int:
    return run_cli(
        (*args, "--data-dir", str(root), *(("--json",) if json_output else ())),
        platform_name="darwin",
        filesystem_type_probe=lambda _path, _platform: "apfs",
    )


def _output(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast(dict[str, object], json.loads(captured.out))


def _status(version: int, tiers: list[str], operation_id: str | None) -> dict[str, object]:
    return {
        "schema": "ob.owner-egress-policy-status.v1",
        "version": version,
        "tiers": tiers,
        "operation_id": operation_id,
    }


def _without_time(payload: dict[str, object]) -> dict[str, object]:
    recorded = payload.pop("recorded_at")
    assert recorded is None or str(recorded).endswith("Z")
    return payload


def test_show_set_and_none_json_shapes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    BrainEngine.open(compile_single_user_local(root))
    assert _cli(root, "egress-policy", "show") == 0
    assert _without_time(_output(capsys)) == _status(0, [], None)
    arguments = ("egress-policy", "set", "--tier", "work", "--tier", "public")
    assert _cli(root, *arguments, "--operation-id", "op-1") == 0
    first = _output(capsys)
    assert _without_time(dict(first)) == _status(1, ["public", "work"], "op-1")
    # Repeating the operation is idempotent and does not append history.
    assert _cli(root, *arguments, "--operation-id", "op-1") == 0
    assert _output(capsys) == first
    assert _cli(root, "egress-policy", "set", "--none", "--operation-id", "op-2") == 0
    assert _without_time(_output(capsys)) == _status(2, [], "op-2")
    assert _cli(root, "egress-policy", "show") == 0
    assert _without_time(_output(capsys)) == _status(2, [], "op-2")


def test_conflicting_operation_reuse_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    BrainEngine.open(compile_single_user_local(root))
    assert _cli(root, "egress-policy", "set", "--tier", "work", "--operation-id", "op") == 0
    capsys.readouterr()
    assert _cli(root, "egress-policy", "set", "--tier", "personal", "--operation-id", "op") == 78
    assert _output(capsys) == {"error": {"code": "operation_conflict"}}


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (("set", "--tier", "secret", "--operation-id", "op"), "invalid_tier"),
        (("set", "--tier", "unknown", "--operation-id", "op"), "invalid_tier"),
        (("set", "--operation-id", "op"), "invalid_arguments"),
        (("set", "--none", "--tier", "work", "--operation-id", "op"), "invalid_arguments"),
        (("set", "--tier", "work", "--operation-id", ""), "invalid_operation_id"),
    ],
)
def test_invalid_arguments_refuse_before_opening(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arguments: tuple[str, ...], code: str
) -> None:
    root = tmp_path / "brain"
    assert _cli(root, "egress-policy", *arguments) == 2
    assert _output(capsys) == {"error": {"code": code}}
    assert not root.exists()


def test_human_output_names_the_shared_tiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "brain"
    BrainEngine.open(compile_single_user_local(root))
    assert _cli(root, "egress-policy", "show", json_output=False) == 0
    assert capsys.readouterr().out == "Owner egress policy version 0: no tier is shared.\n"
    assert (
        _cli(
            root,
            "egress-policy",
            "set",
            "--tier",
            "work",
            "--operation-id",
            "op",
            json_output=False,
        )
        == 0
    )
    assert capsys.readouterr().out == "Owner egress policy version 1: shares work.\n"


def test_missing_brain_is_not_created(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "brain"
    assert _cli(root, "egress-policy", "show") == 78
    assert _output(capsys) == {"error": {"code": "local_operation_failed"}}
    assert not root.exists()


def test_policy_is_not_exposed_to_mcp_or_plugin_surfaces() -> None:
    names = {str(tool.get("name", "")) for tool in MCP_REGISTERED_TOOLS}
    operations = {*_BASE_OPERATIONS, *_NEGOTIATED_OPERATIONS, *_COLLECTOR_OPERATIONS}
    assert not any("egress" in name for name in names | operations)
