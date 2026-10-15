"""Closed CLI/plugin forwarding keeps scoped setup inputs intact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import open_brain.services.local_entrypoints as cli
import open_brain.services.plugin_bridge as bridge
from open_brain.local_data import LocalRootSelection
from open_brain.services.agent_setup import AgentSetupFailure


@pytest.mark.parametrize("apply", [False, True])
def test_cli_forwards_scoped_inputs_without_reading_launch_context_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    runtime = tmp_path / "open-brain"
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o700)
    received: dict[str, Any] = {}

    def setup(_selection: object, **values: Any) -> dict[str, object]:
        received.update(values)
        return {"status": "tested"}

    monkeypatch.setattr(cli, "apply_agent_setup" if apply else "preview_agent_setup", setup)
    context = {"surface": "standalone-cli", "overrides": []}
    args = [
        "agent",
        "setup",
        "--client",
        "codex",
        "--scope",
        "user",
        "--runtime",
        str(runtime),
        "--session-policy",
        str(tmp_path / "policy.json"),
        "--consent-state",
        str(tmp_path / "consent.json"),
        "--client-version",
        "0.162.1",
        "--launch-context",
        json.dumps(context),
        "--acknowledge-unscoped",
        "--json",
    ]
    if apply:
        args += ["--apply", "--preview-id", "setup_test"]
    parsed = cli._parser().parse_args(args)
    selection = LocalRootSelection(tmp_path, None, tmp_path / "brain", "darwin")
    assert cli._run_agent_setup(parsed, selection=selection, environment={}, json_output=True) == 0
    assert received["launch_context"] == context
    assert received["session_policy"] == str(tmp_path / "policy.json")
    assert received["consent_state"] == str(tmp_path / "consent.json")
    assert received["client_version"] == "0.162.1"
    assert received["acknowledge_unscoped"] is True
    assert not selection.brain_root.exists()


@pytest.mark.parametrize("apply", [False, True])
def test_plugin_forwards_closed_scoped_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    received: dict[str, Any] = {}

    def setup(_selection: object, **values: Any) -> dict[str, object]:
        received.update(values)
        return {"status": "tested"}

    monkeypatch.setattr(bridge, "resolve_agent_runtime", lambda _value: tmp_path / "runtime")
    monkeypatch.setattr(bridge, "apply_agent_setup" if apply else "preview_agent_setup", setup)
    values: dict[str, object] = {
        "action": "configure",
        "allow_capture": False,
        "allow_search": True,
        "allow_content_read": True,
        "client": "codex",
        "scope": "user",
        "session_policy": str(tmp_path / "policy.json"),
        "consent_state": str(tmp_path / "consent.json"),
        "client_version": "0.162.1",
        "launch_context": {"surface": "standalone-cli"},
        "acknowledge_unscoped": True,
    }
    operation = "agent.setup.apply" if apply else "agent.setup.preview"
    if apply:
        values["preview_id"] = "setup_test"
    selection = LocalRootSelection(tmp_path, None, tmp_path / "brain", "darwin")
    assert bridge._agent_setup(
        selection, operation, values, base_executable=None, environment={}
    ) == {"status": "tested"}
    for key in (
        "session_policy",
        "consent_state",
        "client_version",
        "launch_context",
        "acknowledge_unscoped",
    ):
        assert received[key] == values[key]
    with pytest.raises(bridge.PluginBridgeFailure, match="invalid_arguments"):
        bridge._agent_setup(
            selection,
            operation,
            {**values, "session_origin": "mcp_policy"},
            base_executable=None,
            environment={},
        )


def test_cli_rejects_invalid_launch_json_before_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "resolve_agent_runtime", lambda _value: tmp_path / "runtime")
    parsed = cli._parser().parse_args(
        [
            "agent",
            "setup",
            "--client",
            "codex",
            "--scope",
            "user",
            "--launch-context",
            "{invalid",
        ]
    )
    with pytest.raises(AgentSetupFailure, match="invalid_arguments"):
        cli._run_agent_setup(
            parsed,
            selection=LocalRootSelection(tmp_path, None, tmp_path / "brain", "darwin"),
            environment={},
            json_output=True,
        )
