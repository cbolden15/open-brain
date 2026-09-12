from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import open_brain_engine.core.ids as engine_ids_module
import pytest
from open_brain_engine.core.ids import canonical_json_bytes

import open_brain.services.phase1_entrypoints as entrypoints_module
from open_brain.config import AppConfig, NamedSecretRef, RetainedRoots, SecretRef
from open_brain.integrations.ui import UiBindConfig
from open_brain.services.http_server import HttpRouteMode
from open_brain.services.phase1_application import SingleUserLocalApplication
from open_brain.services.phase1_entrypoints import (
    reserved_appliance_entrypoints,
    run_cli,
    run_http,
    run_mcp,
)
from open_brain.services.runtime import (
    RESERVED_APPLIANCE_APPLICATION_MODULE,
    RESERVED_APPLIANCE_ENTRYPOINT_MODULE,
    ServiceConfigurationError,
    compose_http_from_config,
    compose_mcp_from_config,
    load_private_http_bind_config,
)

PACKAGE_IMPORT_PATHS = [
    str(Path(inspect.getfile(entrypoints_module)).resolve().parents[2]),
    str(Path(inspect.getfile(engine_ids_module)).resolve().parents[2]),
]
APP_PACKAGE_ROOT = Path(inspect.getfile(entrypoints_module)).resolve().parents[1]


def _installed_package_program(statement: str) -> str:
    return f"import sys; sys.path[:0] = {PACKAGE_IMPORT_PATHS!r}; {statement}"


def test_app_package_cli_uses_the_local_default_entrypoint() -> None:
    module_source = (APP_PACKAGE_ROOT / "__main__.py").read_text(encoding="utf-8")

    assert "from open_brain.services.local_entrypoints import run_cli" in module_source


def test_appliance_entrypoint_names_match_the_installed_scripts() -> None:
    assert RESERVED_APPLIANCE_APPLICATION_MODULE == "open_brain.services.appliance_application"
    assert RESERVED_APPLIANCE_ENTRYPOINT_MODULE == "open_brain.services.appliance_entrypoints"
    assert reserved_appliance_entrypoints() == (
        "open_brain.services.appliance_entrypoints:run_cli",
        "open_brain.services.appliance_entrypoints:run_http",
        "open_brain.services.appliance_entrypoints:run_mcp",
    )


def test_default_entrypoint_module_imports_are_legacy_free_in_a_fresh_process() -> None:
    program = f"""
import importlib
import json
import sys

sys.path[:0] = {PACKAGE_IMPORT_PATHS!r}
module = importlib.import_module("open_brain.services.phase1_entrypoints")
print(json.dumps({{
    "callables": [callable(getattr(module, name)) for name in ("run_cli", "run_http", "run_mcp")],
    "forbidden": sorted(
        name for name in sys.modules
        if name.startswith((
            "open_brain_legacy",
            "open_brain.legacy",
            "open_brain.operations",
            "open_brain.production",
            "open_brain.services.application",
            "open_brain.services.connectors",
            "open_brain.services.entrypoints",
            "open_brain.providers.optional_cloud",
        ))
    ),
}}))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {"callables": [True, True, True], "forbidden": []}


def test_cli_help_and_version_do_not_require_a_brain_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_mcp.__module__ == "open_brain.services.phase1_entrypoints"

    assert run_cli(("--help",), environment={}) == 0
    assert "capture" in capsys.readouterr().out
    assert run_cli(("capture", "--help"), environment={}) == 0
    assert "usage: open-brain capture" in capsys.readouterr().out
    for arguments in (
        ("--version",),
        ("--json", "--version"),
        ("--version", "--json"),
    ):
        assert run_cli(arguments, environment={}) == 0
        assert capsys.readouterr().out == "open-brain 0.1.0\n"


@pytest.mark.parametrize(
    "arguments",
    (
        (
            "--dry-run",
            "capture",
            "quick",
            "text",
            "synthetic non-mutating request",
            "--delivery=dry-run.capture",
            "--json",
        ),
        (
            "capture",
            "quick",
            "text",
            "synthetic non-mutating request",
            "--delivery=dry-run.capture",
            "--dry-run",
            "--json",
        ),
    ),
)
def test_global_dry_run_before_composition_never_mutates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
) -> None:
    root = tmp_path / "brain"

    exit_code = run_cli(
        arguments,
        environment={"OPEN_BRAIN_ROOT": str(root)},
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert output["status"] == "invalid"
    assert not root.exists()


@pytest.mark.parametrize(
    ("arguments", "expected"),
    (
        (("--help",), "usage: open-brain"),
        (("--version",), "open-brain 0.1.0"),
        (("--json", "--version"), "open-brain 0.1.0"),
        (("--version", "--json"), "open-brain 0.1.0"),
    ),
)
@pytest.mark.parametrize(
    "statement",
    (
        "from open_brain.services.appliance_entrypoints import run_cli; "
        "raise SystemExit(run_cli())",
        "import runpy; runpy.run_module('open_brain', run_name='__main__', alter_sys=True)",
    ),
    ids=("app-entrypoint-callable", "module"),
)
def test_installed_entrypoint_and_module_cli_are_root_free(
    arguments: tuple[str, ...],
    expected: str,
    statement: str,
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment.pop("OPEN_BRAIN_ROOT", None)
    environment.pop("OPEN_BRAIN_JOB_ID", None)
    command = (
        sys.executable,
        "-I",
        "-B",
        "-c",
        _installed_package_program(statement),
        *arguments,
    )

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert expected in completed.stdout


def _config(tmp_path: Path, *, with_token: bool = False) -> AppConfig:
    roots = {
        name: tmp_path / name
        for name in ("work", "personal", "capture", "saved", "state", "backup")
    }
    for root in roots.values():
        root.mkdir()
    pages = roots["work"] / "pages"
    pages.mkdir()
    (pages / "topic.md").write_text("# Synthetic topic\n\nBounded work text.\n")
    return AppConfig(
        roots=RetainedRoots(
            work=roots["work"],
            personal=roots["personal"],
            capture=roots["capture"],
            saved_content=roots["saved"],
            state=roots["state"],
        ),
        backup=roots["backup"],
        secret_refs=(
            (
                NamedSecretRef(
                    "service_token",
                    SecretRef.parse("env:SYNTHETIC_SERVICE_TOKEN"),
                ),
            )
            if with_token
            else ()
        ),
    )


def _clock() -> datetime:
    return datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def test_mcp_composes_without_an_http_secret_and_serves_work_tools(tmp_path: Path) -> None:
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    lifecycle = compose_mcp_from_config(application=application)
    requests = (
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
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    source = io.BytesIO(
        b"".join(
            json.dumps(request, separators=(",", ":")).encode() + b"\n" for request in requests
        )
    )
    output = io.BytesIO()

    lifecycle.serve(input_stream=source, output_stream=output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]

    assert responses[0]["result"]["serverInfo"]["name"] == "open-brain"
    names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert names == {"brain_fetch", "brain_query", "brain_retrieval_feedback"}


def test_http_composition_requires_and_resolves_only_named_secret_ref(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, with_token=True)
    application = SingleUserLocalApplication.open(tmp_path / "brain")

    lifecycle = compose_http_from_config(
        config=config,
        application=application,
        environment={"SYNTHETIC_SERVICE_TOKEN": "synthetic-service-token"},
        file_reader=lambda _path: "not-used",
    )
    response = lifecycle.dispatch(
        method="GET",
        path="/health",
        headers=(("Authorization", "Bearer synthetic-service-token"),),
        body_reader=lambda _maximum, _timeout: b"",
    )

    assert response.status == 200
    assert response.body == b'{"status":"ok"}'


def test_http_composition_fails_closed_without_service_secret(tmp_path: Path) -> None:
    config = _config(tmp_path)
    application = SingleUserLocalApplication.open(tmp_path / "brain")

    with pytest.raises(ServiceConfigurationError, match="service credential unavailable"):
        compose_http_from_config(
            config=config,
            application=application,
            environment={},
            file_reader=lambda _path: "not-used",
        )


def test_http_jobs_select_distinct_named_tokens_and_route_modes(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        secret_refs=(
            NamedSecretRef(
                "ingress_service_token",
                SecretRef.parse("env:SYNTHETIC_INGRESS_TOKEN"),
            ),
            NamedSecretRef(
                "ui_service_token",
                SecretRef.parse("env:SYNTHETIC_UI_TOKEN"),
            ),
        ),
    )
    application = SingleUserLocalApplication.open(tmp_path / "brain")
    environment = {
        "SYNTHETIC_UI_TOKEN": "synthetic-ui-token",
        "SYNTHETIC_INGRESS_TOKEN": "synthetic-ingress-token",
    }

    ui = compose_http_from_config(
        config=config,
        application=application,
        environment=environment,
        file_reader=lambda _path: "not-used",
        secret_name="ui_service_token",
        route_mode=HttpRouteMode.UI_ONLY,
    )
    ingress = compose_http_from_config(
        config=config,
        application=application,
        environment=environment,
        file_reader=lambda _path: "not-used",
        secret_name="ingress_service_token",
        route_mode=HttpRouteMode.SHARE_ONLY,
    )

    assert ui.service.config.route_mode is HttpRouteMode.UI_ONLY
    assert ingress.service.config.route_mode is HttpRouteMode.SHARE_ONLY
    assert (
        ui.dispatch(
            method="GET",
            path="/health",
            headers=(("Authorization", "Bearer synthetic-ui-token"),),
            body_reader=lambda _a, _b: b"",
        ).status
        == 200
    )
    assert (
        ingress.dispatch(
            method="GET",
            path="/health",
            headers=(("Authorization", "Bearer synthetic-ingress-token"),),
            body_reader=lambda _a, _b: b"",
        ).status
        == 405
    )


def test_private_http_bind_config_is_owner_only_and_canonical(tmp_path: Path) -> None:
    path = tmp_path / "http-bind.json"
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "host": "127.0.0.1",
                "port": 8789,
                "allow_private_network": False,
            }
        )
    )
    path.chmod(0o600)

    assert load_private_http_bind_config(path) == UiBindConfig(
        host="127.0.0.1",
        port=8789,
        allow_private_network=False,
    )

    path.chmod(0o644)
    with pytest.raises(ServiceConfigurationError, match="HTTP service configuration"):
        load_private_http_bind_config(path)


class _McpLifecycle:
    def serve(self, *, input_stream: object, output_stream: object) -> None:
        del input_stream, output_stream


class _HttpServer:
    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _HttpLifecycle:
    def start(self) -> _HttpServer:
        return _HttpServer()


def test_phase1_run_cli_delegates_to_the_appliance_cli_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []

    def delegated(
        argv: tuple[str, ...] | list[str] | None = None,
        *,
        environment: dict[str, object] | None = None,
    ) -> int:
        observed.extend((argv, environment))
        return 0

    monkeypatch.setattr("open_brain.services.phase1_entrypoints.appliance_run_cli", delegated)

    environment = {"OPEN_BRAIN_ROOT": "/tmp/brain"}
    assert run_cli(("status", "--json"), environment=environment) == 0
    assert observed == [("status", "--json"), environment]


def test_phase1_run_mcp_delegates_to_the_appliance_mcp_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {"count": 0}

    def delegated() -> int:
        observed["count"] += 1
        return 0

    monkeypatch.setattr("open_brain.services.phase1_entrypoints.appliance_run_mcp", delegated)

    assert run_mcp() == 0
    assert observed == {"count": 1}
    source = inspect.getsource(run_mcp)
    assert "SingleUserLocalApplication" not in source
    assert "compose_mcp_from_config" not in source


def test_phase1_run_mcp_propagates_the_appliance_fail_closed_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "open_brain.services.phase1_entrypoints.appliance_run_mcp",
        lambda: 78,
    )
    assert run_mcp() == 78


def test_phase1_run_http_fails_closed_via_the_appliance_stub_without_opening_a_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {"count": 0}

    def delegated() -> int:
        observed["count"] += 1
        return 78

    monkeypatch.setattr("open_brain.services.phase1_entrypoints.appliance_run_http", delegated)

    assert run_http() == 78
    assert observed == {"count": 1}
    source = inspect.getsource(run_http)
    assert "SingleUserLocalApplication" not in source
    assert "compose_http_from_config" not in source
