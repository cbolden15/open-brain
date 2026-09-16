from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

from open_brain_collector.boundary import COLLECTOR_BOUNDARY, assert_collector_boundary

ROOT = Path(__file__).resolve().parents[4]
APP_PYPROJECT = ROOT / "packages/app/pyproject.toml"
COLLECTOR_PYPROJECT = ROOT / "packages/collector/pyproject.toml"


def test_collector_is_a_separate_optional_distribution() -> None:
    app = tomllib.loads(APP_PYPROJECT.read_text(encoding="utf-8"))
    collector = tomllib.loads(COLLECTOR_PYPROJECT.read_text(encoding="utf-8"))
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert collector["project"]["name"] == "open-brain-collector"
    assert collector["project"]["dependencies"] == [
        "open-brain-connectors==0.1.0",
        "open-brain-engine==0.1.0",
    ]
    assert collector["project"]["scripts"] == {
        "open-brain-collector": "open_brain_collector.cli:run_cli",
    }
    assert app["project"]["dependencies"] == ["open-brain-engine==0.1.0"]
    assert app["project"]["scripts"] == {
        "open-brain": "open_brain.services.local_entrypoints:run_cli",
    }
    assert "packages/collector" in workspace["tool"]["uv"]["workspace"]["members"]
    assert "open-brain-collector" not in workspace["dependency-groups"]["native-build"]


def test_collector_boundary_is_opt_in_and_foreground_inert_by_default() -> None:
    boundary = assert_collector_boundary()

    assert boundary is COLLECTOR_BOUNDARY
    assert boundary.dependency_packages == ("open-brain-connectors", "open-brain-engine")
    assert boundary.service_install_enabled is True
    assert boundary.unattended_enabled_by_default is False
    assert boundary.network_enabled_by_default is False


def test_collector_runtime_does_not_import_app_or_service_modules() -> None:
    source_root = ROOT / "packages/collector/src/open_brain_collector"
    imported_roots: set[str] = set()

    for path in source_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "open_brain.profile" not in source
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = (node.module,)
            imported_roots.update(name.partition(".")[0] for name in names)

    assert "open_brain" not in imported_roots
    assert not (imported_roots & set(COLLECTOR_BOUNDARY.forbidden_runtime_modules))


def test_collector_cli_reports_boundary_without_starting_collection() -> None:
    completed = subprocess.run(
        (sys.executable, "-m", "open_brain_collector.cli", "--boundary-json"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert '"package": "open-brain-collector"' in completed.stdout
    assert '"service_install_enabled": true' in completed.stdout
    assert '"unattended_enabled_by_default": false' in completed.stdout


def test_optional_control_uses_only_private_unix_transport() -> None:
    source = ROOT / "packages/collector/src/open_brain_collector/control.py"
    tree = ast.parse(source.read_text(), filename=str(source))
    socket_calls = []
    server_types = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "socketserver"
        ):
            server_types.add(node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "socket"
            and node.func.attr == "socket"
        ):
            socket_calls.append(node)
    assert server_types == {"ThreadingMixIn", "UnixStreamServer", "BaseRequestHandler"}
    assert socket_calls
    for call in socket_calls:
        assert ast.unparse(call.args[0]) == "socket.AF_UNIX"
        assert ast.unparse(call.args[1]) == "socket.SOCK_STREAM"
