from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import tools.open_brain_dev.base_native as base_native
from tools.open_brain_dev.base_native import BaseNativeError


def _mcp_process(
    tools: set[str], *, catalog_tools: set[str] | None = None
) -> subprocess.CompletedProcess[str]:
    responses: list[dict[str, object]] = [
        {"result": {"capabilities": {"tools": {}}}},
        {"result": {"tools": [{"name": name} for name in sorted(tools)]}},
        {"result": {"structuredContent": {"status": "ok"}}},
        {
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "unknown tool"}],
            }
        },
    ]
    if catalog_tools is not None:
        responses.append(
            {
                "result": {
                    "structuredContent": {
                        "surfaces": {"mcp": {"authorized_tools": sorted(catalog_tools)}}
                    }
                }
            }
        )
    return subprocess.CompletedProcess(
        args=("open-brain", "mcp"),
        returncode=0,
        stdout="".join(json.dumps(response) + "\n" for response in responses),
        stderr="",
    )


def _catalog() -> dict[str, object]:
    return {
        "schema_version": 2,
        "product": {
            "name": "open-brain",
            "version": "0.1.0",
            "public_acceptance": "not_certified",
        },
        "compatibility": {
            "bridge_protocol": 1,
            "catalog_schema": 2,
            "portable_metadata": 5,
            "runtime_session": 4,
            "state_schema": 9,
            "task_contract": "t03.v1",
        },
        "packages": [
            {"name": "open-brain", "installed": True},
            {"name": "open-brain-engine", "installed": True},
            {"name": "open-brain-connectors", "installed": False},
            {"name": "open-brain-collector", "installed": False},
        ],
        "acceptance": {"trusted_certifications": []},
    }


def test_native_audit_requires_catalog_implementation() -> None:
    assert "open_brain.services.catalog" in base_native._REQUIRED_MODULES


def test_native_mcp_inventory_requires_catalog_and_exact_grant_set() -> None:
    expected = base_native._expected_mcp_tools(
        "--allow-content-read",
        "brain_read",
        {"brain_read", "brain_contract_describe"},
    )
    assert expected == {"brain_catalog", "brain_contract_describe", "brain_read"}
    assert base_native._validate_mcp_exchange(_mcp_process(expected), expected) == {"status": "ok"}

    for drifted in (expected - {"brain_catalog"}, expected | {"brain_capture"}):
        with pytest.raises(BaseNativeError, match="native MCP exchange failed"):
            base_native._validate_mcp_exchange(_mcp_process(drifted), expected)


def test_native_mcp_catalog_call_requires_exact_session_grant_projection() -> None:
    expected = {"brain_capture", "brain_catalog"}
    requests = base_native._mcp_exchange_requests(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        "brain_capture",
        {"text": "synthetic"},
        "brain_search",
        inspect_catalog=True,
    )
    assert requests[-1] == {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "brain_catalog", "arguments": {"schema_version": 2}},
    }
    assert base_native._validate_mcp_exchange(
        _mcp_process(expected, catalog_tools=expected),
        expected,
        inspect_catalog=True,
    ) == {"status": "ok"}

    with pytest.raises(BaseNativeError, match="native MCP catalog authorization failed"):
        base_native._validate_mcp_exchange(
            _mcp_process(expected, catalog_tools=expected | {"brain_search"}),
            expected,
            inspect_catalog=True,
        )


def test_grantless_catalog_probe_requires_core_coordinates_and_no_optional_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    environment = {"HOME": os.fspath(home), "PATH": os.defpath}
    observed: list[tuple[str, ...]] = []
    catalog = _catalog()

    def run(
        command: tuple[str, ...], selected_environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        assert selected_environment == environment
        return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")

    monkeypatch.setattr(base_native, "_run", run)
    base_native._smoke_catalog(tmp_path / "open-brain", environment)
    assert observed == [
        (
            os.fspath(tmp_path / "open-brain"),
            "catalog",
            "--schema-version",
            "2",
            "--json",
        )
    ]
    assert not base_native._brain_root(home).exists()

    compatibility = catalog["compatibility"]
    assert isinstance(compatibility, dict)
    compatibility["runtime_session"] = 2
    with pytest.raises(BaseNativeError, match="native catalog discovery failed"):
        base_native._smoke_catalog(tmp_path / "open-brain", environment)
    compatibility["runtime_session"] = 4
    compatibility["state_schema"] = 7
    with pytest.raises(BaseNativeError, match="native catalog discovery failed"):
        base_native._smoke_catalog(tmp_path / "open-brain", environment)
    compatibility["state_schema"] = 9

    packages = catalog["packages"]
    assert isinstance(packages, list)
    packages[2]["installed"] = True
    with pytest.raises(BaseNativeError, match="native catalog discovery failed"):
        base_native._smoke_catalog(tmp_path / "open-brain", environment)


def test_runtime_subprocess_uses_disposable_cwd_and_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    environment = {"HOME": os.fspath(home), "PATH": os.defpath}
    observed: dict[str, object] = {}

    def run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    result = base_native._run(("/synthetic/open-brain", "--version"), environment)

    assert result.stdout == "ok\n"
    assert observed["cwd"] == work.resolve()
    assert observed["env"] == {"HOME": os.fspath(home), "PATH": os.defpath}
    assert "PYTHONPATH" not in observed["env"]
