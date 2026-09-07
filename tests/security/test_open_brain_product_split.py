from __future__ import annotations

import ast
import inspect
import subprocess
import sys
import tomllib
from pathlib import Path

import open_brain.services.local_entrypoints as local_entrypoints

ROOT = Path(__file__).resolve().parents[2]


def test_package_metadata_freezes_base_extra_and_command_graph() -> None:
    app = tomllib.loads((ROOT / "packages/app/pyproject.toml").read_text(encoding="utf-8"))
    engine = tomllib.loads((ROOT / "packages/engine/pyproject.toml").read_text(encoding="utf-8"))

    assert app["project"]["dependencies"] == ["open-brain-engine==0.1.0"]
    assert app["project"]["optional-dependencies"]["secure-node"] == [
        "open-brain-engine[secure-node]==0.1.0",
        "starlette>=0.48,<1",
        "uvicorn>=0.40,<1",
    ]
    assert set(engine["project"]["optional-dependencies"]) == {"secure-node"}
    assert app["project"]["scripts"] == {
        "open-brain": "open_brain.services.local_entrypoints:run_cli",
        "open-brain-secure-node": "open_brain.services.secure_node_entrypoints:run_cli",
        "open-brain-secure-node-mcp": "open_brain.services.secure_node_entrypoints:run_mcp",
    }


def test_local_entrypoint_source_has_no_secure_node_import() -> None:
    tree = ast.parse(inspect.getsource(local_entrypoints))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(
        module.startswith("open_brain.services.appliance")
        or module.split(".", 1)[0]
        in {"argon2", "cryptography", "keyring", "sqlcipher3", "starlette", "uvicorn"}
        for module in imports
    )


def test_fresh_process_imports_default_cli_without_advanced_modules() -> None:
    app_source = str(ROOT / "packages/app/src")
    engine_source = str(ROOT / "packages/engine/src")
    program = f"""
import sys
sys.path[:0] = [{app_source!r}, {engine_source!r}]
import open_brain.services.local_entrypoints
forbidden = [
    name for name in sys.modules
    if name.startswith("open_brain.services.appliance")
    or name.split(".", 1)[0] in {{
        "argon2", "cryptography", "keyring", "sqlcipher3", "starlette", "uvicorn"
    }}
]
raise SystemExit(1 if forbidden else 0)
"""

    completed = subprocess.run(
        (sys.executable, "-I", "-B", "-c", program),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
