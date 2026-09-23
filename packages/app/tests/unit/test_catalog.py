from __future__ import annotations

import importlib.metadata
import io
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

import open_brain.services.local_entrypoints as entrypoints
import open_brain.services.plugin_bridge as bridge
from open_brain.local_data import select_local_root
from open_brain.services.catalog import (
    CatalogRequestError,
    build_catalog,
    cli_registrations,
)
from open_brain.services.local_entrypoints import _parser, run_cli
from open_brain.services.local_mcp import MCP_REGISTERED_TOOLS, LocalMcpAdapter
from open_brain.services.mcp_protocol import McpCallError


def _owner_authority() -> EffectiveAuthority:
    return EffectiveAuthority("catalog-owner", "catalog-session", frozenset(), None, owner=True)


def _catalog(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "cli_commands": cli_registrations(_parser()),
        "mcp_registered": MCP_REGISTERED_TOOLS,
        "mcp_authorized": None,
        "mcp_discovery_context": "test",
        "bridge_base": bridge._BASE_OPERATIONS,
        "bridge_negotiated": bridge._NEGOTIATED_OPERATIONS,
        "bridge_optional": bridge._COLLECTOR_OPERATIONS,
        "bridge_available": None,
        "base_executable": Path("/missing/synthetic/open-brain"),
        "platform_name": "darwin",
        "machine": "arm64",
    }
    arguments.update(overrides)
    return build_catalog({"schema_version": 2}, **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "catalog_request",
    (
        {},
        {"schema_version": 1},
        {"schema_version": 2.0},
        {"schema_version": True},
        {"schema_version": 2, "extra": False},
    ),
)
def test_catalog_request_is_exact_schema_two(catalog_request: dict[str, object]) -> None:
    with pytest.raises(CatalogRequestError, match="unsupported catalog request"):
        build_catalog(
            catalog_request,
            cli_commands=(),
            mcp_registered=(),
            mcp_authorized=None,
            mcp_discovery_context="test",
            bridge_base=(),
            bridge_negotiated=(),
            bridge_optional=(),
            bridge_available=None,
            entry_points=(),
            base_executable=Path("/missing/synthetic/open-brain"),
        )


def test_catalog_is_deterministic_json_safe_and_evidence_paths_exist() -> None:
    first = _catalog()
    second = _catalog()
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    assert first["schema_version"] == 2
    assert cast(dict[str, object], first["compatibility"])["portable_metadata"] == 5
    assert cast(dict[str, object], first["acceptance"])["trusted_certifications"] == []
    root = Path(__file__).resolve().parents[4]
    evidence = cast(list[dict[str, str]], cast(dict[str, object], first["acceptance"])["evidence"])
    assert all((root / item["path"]).is_file() for item in evidence)
    assert all(item["scope"].startswith("historical_") for item in evidence)


def test_cli_catalog_uses_parser_registration_before_root_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Brain selection/bootstrap is forbidden")

    monkeypatch.setattr(entrypoints, "select_local_root", forbidden)
    assert run_cli(("catalog", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    commands = payload["surfaces"]["cli"]["commands"]
    assert commands == list(cli_registrations(_parser()))
    assert {tuple(item["command"]) for item in commands} >= {
        ("catalog",),
        ("obsidian-plugin", "install"),
        ("workspace", "recover"),
        ("graph", "refresh-structural"),
        ("history", "show"),
        ("journal", "discard"),
        ("journal", "drain"),
        ("journal", "retry"),
        ("journal", "status"),
    }
    obsidian = next(item for item in commands if item["command"] == ["obsidian-plugin", "install"])
    assert obsidian["positional_choices"] == {"action": "install"}
    assert len(commands) == 57
    assert run_cli(("catalog", "--json", "--schema-version", "1")) == 2
    assert run_cli(("catalog", "--json", "--unknown")) == 2


def test_owner_privacy_repair_is_installed_but_excluded_from_every_catalog_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import open_brain.services.t03_adapters as t03

    parsed = _parser().parse_args(("privacy", "repair", "--request-file", "-", "--json"))
    assert parsed.command == "privacy"
    assert parsed.privacy_action == "repair"
    commands = cli_registrations(_parser())
    assert len(commands) == 57
    assert ("privacy", "repair") not in {
        tuple(cast(list[str], command["command"])) for command in commands
    }

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Brain selection/bootstrap is forbidden")

    monkeypatch.setattr(entrypoints, "select_local_root", forbidden)
    assert run_cli(("catalog", "--json")) == 0
    direct = json.loads(capsys.readouterr().out)
    adapter = LocalMcpAdapter(authority=_owner_authority(), search=lambda _query, _limit: ())
    mcp = adapter.call_tool("brain_catalog", {"schema_version": 2})

    selection = select_local_root(
        data_dir=str(tmp_path / "brain"),
        environment={"HOME": str(tmp_path)},
        platform_name="darwin",
    )
    request = {
        "arguments": {"schema_version": 2},
        "operation": "catalog.describe",
        "protocol": bridge.OPEN_BRAIN_CLIENT_PROTOCOL,
        "protocol_version": bridge.OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
        "request_id": f"plugin_{uuid.uuid4()}",
    }
    output = io.BytesIO()
    assert (
        bridge.serve_plugin_stdio(
            selection,
            authority=_owner_authority(),
            input_stream=io.BytesIO(json.dumps(request).encode()),
            output_stream=output,
            base_executable=tmp_path / "missing/open-brain",
            environment={"HOME": str(tmp_path)},
        )
        == 0
    )
    plugin = cast(dict[str, object], json.loads(output.getvalue())["result"])
    for catalog in (direct, mcp, plugin):
        surfaces = cast(dict[str, object], catalog["surfaces"])
        cli = cast(dict[str, object], surfaces["cli"])
        serialized_commands = cast(list[dict[str, object]], cli["commands"])
        assert ["privacy", "repair"] not in [row["command"] for row in serialized_commands]

    assert all("repair" not in cast(str, tool["name"]) for tool in MCP_REGISTERED_TOOLS)
    assert all(
        "repair" not in operation
        for operation in (
            *bridge._BASE_OPERATIONS,
            *bridge._NEGOTIATED_OPERATIONS,
            *bridge._COLLECTOR_OPERATIONS,
            *bridge._available_operations({}, _owner_authority(), capture_available=False),
            *t03._NEGOTIATED,
            *t03._OWNER_ONLY,
        )
    )


def test_mcp_catalog_uses_actual_authorized_session_registry() -> None:
    adapter = LocalMcpAdapter(authority=_owner_authority(), search=lambda _query, _limit: ())
    names = [tool["name"] for tool in adapter.list_tools()]
    assert names == ["brain_search", "brain_catalog"]
    payload = adapter.call_tool("brain_catalog", {"schema_version": 2})
    mcp = cast(dict[str, object], cast(dict[str, object], payload["surfaces"])["mcp"])
    assert mcp["authorized_tools"] == sorted(names)
    assert mcp["discovery_context"] == "authorized_session_registry"
    with pytest.raises(McpCallError, match="invalid tool arguments"):
        adapter.call_tool("brain_catalog", {"schema_version": 1})
    with pytest.raises(McpCallError, match="unknown tool"):
        adapter.call_tool("semantic_recall", {})


def test_registered_mcp_catalog_matches_all_injected_tools() -> None:
    def operations(_arguments: object) -> dict[str, object]:
        return {"status": "ok"}

    def empty() -> dict[str, object]:
        return {"status": "ok"}

    adapter = LocalMcpAdapter(
        authority=_owner_authority(),
        search=lambda _query, _limit: (),
        workspace_status=empty,
        graph_suggestions=empty,
        graph_projection=empty,
        graph_refresh=lambda _attempts, _bytes: ({}, 0, 0),
        inbox_list=operations,
        space_list=operations,
        space_create=operations,
        space_rename=operations,
        inbox_route=operations,
        review_list=operations,
        review_show=operations,
        review_propose=operations,
        review_approve=operations,
        review_reject=operations,
        review_edit_and_approve=operations,
    )
    # Capture and negotiated tools need concrete engine adapters; account for their names here.
    actual = (
        {tool["name"] for tool in adapter.list_tools()}
        | {"brain_capture"}
        | {"brain_capture_submit"}
    )
    registered = {cast(str, tool["name"]) for tool in MCP_REGISTERED_TOOLS}
    contract = next(
        tool for tool in MCP_REGISTERED_TOOLS if tool["name"] == "brain_contract_describe"
    )
    assert contract["required_grants_any_of"] == [
        "content-read",
        "history-read",
        "organize",
        "search",
    ]
    assert "required_grants" not in contract
    negotiated = {
        "brain_contract_describe",
        "brain_search_page",
        "brain_read",
        "brain_history_list",
        "brain_history_show",
        "brain_source_route",
    }
    assert actual == registered - negotiated


def test_missing_and_mismatched_distribution_metadata_fail_closed() -> None:
    missing = _catalog(
        distribution_lookup=lambda name: (_ for _ in ()).throw(
            importlib.metadata.PackageNotFoundError(name)
        ),
        entry_points=(),
    )
    assert all(not item["installed"] for item in cast(list[dict[str, object]], missing["packages"]))

    fake = SimpleNamespace(version="9.9.9", entry_points=(), requires=())
    mismatched = _catalog(distribution_lookup=lambda _name: fake, entry_points=())
    for package in cast(list[dict[str, object]], mismatched["packages"]):
        assert package["installed"] is True
        assert package["version_compatible"] is False
        assert package["dependency_compatible"] is False
        assert not any(cast(dict[str, bool], package["entry_points"]).values())
    extension = cast(dict[str, object], mismatched["optional_connector_extensions"])
    assert extension["registered"] is False

    declared_app = SimpleNamespace(
        version="0.1.0",
        entry_points=(),
        requires=("open-brain-engine==0.1.0",),
    )
    wrong_engine = SimpleNamespace(version="9.9.9", entry_points=(), requires=())

    def dependency_mismatch(name: str) -> Any:
        return declared_app if name == "open-brain" else wrong_engine

    dependency_payload = _catalog(
        distribution_lookup=dependency_mismatch,
        entry_points=(),
    )
    app = cast(list[dict[str, object]], dependency_payload["packages"])[0]
    assert app["declared_dependencies_match"] is True
    assert app["dependency_compatible"] is False

    malformed = SimpleNamespace(version="0.1.4.private", entry_points=(), requires=())
    malformed_payload = _catalog(
        distribution_lookup=lambda _name: malformed,
        entry_points=(),
    )
    engine = cast(list[dict[str, object]], malformed_payload["packages"])[1]
    assert engine["dependency_compatible"] is False


@pytest.mark.parametrize(
    "unsafe_version",
    (
        "/synthetic/private/package-root",
        "0.1.0\ncredential=SYNTHETIC_TOKEN",
        "0.1.0+private-local-label",
        "x" * 300_000,
        "0.1." + "9" * 5_000,
    ),
)
def test_untrusted_versions_are_not_projected_or_unbounded(unsafe_version: str) -> None:
    fake = SimpleNamespace(version=unsafe_version, entry_points=(), requires=())
    payload = _catalog(distribution_lookup=lambda _name: fake, entry_points=())
    serialized = json.dumps(payload, sort_keys=True)
    assert unsafe_version not in serialized
    assert len(serialized) < 100_000
    for package in cast(list[dict[str, object]], payload["packages"]):
        assert package["observed_version"] is None
        assert package["version_metadata_status"] == "invalid"
        assert package["version_compatible"] is False
        assert package["dependency_compatible"] is False


def test_public_cli_and_mcp_do_not_disclose_untrusted_version_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unsafe = "/synthetic/private/package-root\ncredential=SYNTHETIC_TOKEN"
    fake = SimpleNamespace(version=unsafe, entry_points=(), requires=())
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: fake)
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda: ())

    assert run_cli(("catalog", "--json")) == 0
    output = capsys.readouterr()
    assert unsafe not in output.out
    assert "SYNTHETIC_TOKEN" not in output.out + output.err
    assert len(output.out) < 100_000

    adapter = LocalMcpAdapter(authority=_owner_authority(), search=lambda _query, _limit: ())
    payload = adapter.call_tool("brain_catalog", {"schema_version": 2})
    serialized = json.dumps(payload)
    assert unsafe not in serialized
    assert "SYNTHETIC_TOKEN" not in serialized
    with pytest.raises(McpCallError, match="response_too_large"):
        adapter.call_tool(
            "brain_catalog",
            {"schema_version": 2},
            maximum_response_bytes=1_024,
        )


class _BrokenMetadata:
    def __init__(self, failing_property: str) -> None:
        self.failing_property = failing_property

    @property
    def version(self) -> str:
        if self.failing_property == "version":
            raise OSError("/synthetic/private/package-root/METADATA")
        return "0.1.0"

    @property
    def entry_points(self) -> tuple[object, ...]:
        if self.failing_property == "entry_points":
            raise OSError("/synthetic/private/package-root/entry_points.txt")
        return ()

    @property
    def requires(self) -> tuple[str, ...]:
        if self.failing_property == "requires":
            raise UnicodeError("synthetic corrupt metadata")
        return ()


@pytest.mark.parametrize("failing_property", ("version", "entry_points", "requires"))
def test_unreadable_distribution_properties_are_code_only_observations(
    failing_property: str,
) -> None:
    payload = _catalog(
        distribution_lookup=lambda _name: cast(
            importlib.metadata.Distribution, _BrokenMetadata(failing_property)
        ),
        entry_points=(),
    )
    serialized = json.dumps(payload)
    assert "/synthetic/private" not in serialized
    assert "corrupt metadata" not in serialized
    for package in cast(list[dict[str, object]], payload["packages"]):
        assert package["installed"] is True
        assert package["metadata_status"] == "incomplete"
        assert package["version_compatible"] is (failing_property != "version")


def test_unreadable_lookup_and_global_entry_points_fail_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreadable(_name: str) -> importlib.metadata.Distribution:
        raise OSError("/synthetic/private/package-root/METADATA")

    monkeypatch.setattr(importlib.metadata, "distribution", unreadable)
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda: (_ for _ in ()).throw(OSError("/synthetic/private/entry_points.txt")),
    )
    assert run_cli(("catalog", "--json")) == 0
    captured = capsys.readouterr()
    assert "/synthetic/private" not in captured.out + captured.err
    payload = json.loads(captured.out)
    assert all(
        package["installed"] == "unknown" and package["metadata_status"] == "unreadable"
        for package in payload["packages"]
    )
    assert payload["optional_connector_extensions"]["metadata_status"] == "unreadable"


def test_malformed_metadata_and_wrong_entry_point_target_fail_closed() -> None:
    malformed = SimpleNamespace(version="0.1.0", entry_points="not-a-sequence", requires={})
    payload = _catalog(distribution_lookup=lambda _name: malformed, entry_points=())
    for package in cast(list[dict[str, object]], payload["packages"]):
        assert package["entry_point_metadata_status"] == "invalid"
        assert package["requirements_metadata_status"] == "invalid"
        assert not any(cast(dict[str, bool], package["entry_points"]).values())

    wrong_target = importlib.metadata.EntryPoint(
        name="open-brain",
        value="synthetic_wrong.module:run",
        group="console_scripts",
    )
    correct_version_wrong_target = SimpleNamespace(
        version="0.1.0",
        entry_points=(wrong_target,),
        requires=("open-brain-engine==0.1.0",),
    )
    payload = _catalog(
        distribution_lookup=lambda _name: correct_version_wrong_target,
        entry_points=(),
    )
    app = cast(list[dict[str, object]], payload["packages"])[0]
    assert app["version_compatible"] is True
    assert app["entry_points"] == {"open-brain": False}


def test_unexpected_metadata_programming_error_remains_distinct() -> None:
    def programming_error(_name: str) -> importlib.metadata.Distribution:
        raise RuntimeError("synthetic programming failure")

    with pytest.raises(RuntimeError, match="synthetic programming failure"):
        _catalog(distribution_lookup=programming_error, entry_points=())


def test_plugin_assets_absent_and_optional_packages_are_not_imported() -> None:
    before = {
        name
        for name in sys.modules
        if name.startswith(("open_brain_connectors", "open_brain_collector"))
    }
    payload = _catalog(base_executable=Path("/missing/synthetic/open-brain"))
    after = {
        name
        for name in sys.modules
        if name.startswith(("open_brain_connectors", "open_brain_collector"))
    }
    assert after == before
    obsidian = cast(dict[str, object], cast(dict[str, object], payload["surfaces"])["obsidian"])
    assert obsidian == {
        "acceptance": "source_only",
        "authorization": "unassessed",
        "enabled": "unassessed",
        "implementation": "implemented",
        "packaged_assets": "unavailable",
        "scope": "core",
        "readiness": "unavailable",
        "version": None,
    }


def test_bridge_catalog_bypasses_brain_and_credential_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = select_local_root(
        data_dir=str(tmp_path / "brain"),
        environment={"HOME": str(tmp_path)},
        platform_name="darwin",
    )

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("credential discovery is forbidden")

    monkeypatch.setattr(bridge, "discover_credential_store", forbidden)
    request = {
        "arguments": {"schema_version": 2},
        "operation": "catalog.describe",
        "protocol": bridge.OPEN_BRAIN_CLIENT_PROTOCOL,
        "protocol_version": bridge.OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
        "request_id": f"plugin_{uuid.uuid4()}",
    }
    output = io.BytesIO()
    assert (
        bridge.serve_plugin_stdio(
            selection,
            authority=_owner_authority(),
            input_stream=io.BytesIO(json.dumps(request).encode()),
            output_stream=output,
            base_executable=tmp_path / "missing/open-brain",
            environment={"HOME": str(tmp_path)},
        )
        == 0
    )
    response = json.loads(output.getvalue())
    assert response["ok"] is True
    assert response["result"]["schema_version"] == 2
    assert not selection.brain_root.exists()

    request["arguments"] = {"schema_version": 1}
    output = io.BytesIO()
    bridge.serve_plugin_stdio(
        selection,
        authority=_owner_authority(),
        input_stream=io.BytesIO(json.dumps(request).encode()),
        output_stream=output,
        base_executable=tmp_path / "missing/open-brain",
        environment={"HOME": str(tmp_path)},
    )
    assert json.loads(output.getvalue())["error"]["code"] == "invalid_arguments"


def test_bridge_registry_catalog_is_exact_and_deferred_operation_is_refused(tmp_path: Path) -> None:
    payload = _catalog(
        bridge_available=bridge._available_operations(
            {}, _owner_authority(), capture_available=False
        )
    )
    surface = cast(dict[str, object], cast(dict[str, object], payload["surfaces"])["plugin_bridge"])
    assert surface["base_operations"] == sorted(bridge._BASE_OPERATIONS)
    assert surface["negotiated_operations"] == sorted(bridge._NEGOTIATED_OPERATIONS)
    assert surface["optional_collector_operations"] == sorted(bridge._COLLECTOR_OPERATIONS)
    assert not set(bridge._COLLECTOR_OPERATIONS) & set(
        cast(list[str], surface["available_in_context"])
    )
    with pytest.raises(bridge.PluginBridgeFailure, match="unknown_operation"):
        bridge.dispatch_plugin_request(
            cast(Any, SimpleNamespace(tasks=SimpleNamespace())),
            "semantic.recall",
            {},
            request_id=f"plugin_{uuid.uuid4()}",
            base_executable=tmp_path / "missing/open-brain",
            runtime=bridge.PluginRuntimeState(None, _owner_authority()),
        )
