"""Run and preserve the reproducible M1 encrypted-storage compatibility matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tools/m1/compatibility_probe.py"
PYTHON_VERSIONS = ("3.12", "3.13", "3.14")
INSTALL_MODES = ("wheel", "source")
DEPENDENCIES = ("rfc8785==0.1.4", "sqlcipher3==0.6.2")
CELL_TIMEOUT_SECONDS = 900


def command_templates(platform_name: str, python_version: str, install: str) -> list[str]:
    """Return sanitized commands that reproduce one matrix cell."""
    binary_policy = "--only-binary=:all:" if install == "wheel" else "--no-binary=:all:"
    dependencies = " ".join(DEPENDENCIES)
    if platform_name == "macos":
        return [
            f"uv venv --python {python_version} ${{TMP}}/env",
            " ".join(
                (
                    "UV_CACHE_DIR=${UV_CACHE_DIR}",
                    "CONAN_HOME=${TMP}/conan",
                    "uv pip install --python ${TMP}/env/bin/python",
                    binary_policy,
                    dependencies,
                )
            ),
            "${TMP}/env/bin/python tools/m1/compatibility_probe.py",
        ]
    if platform_name == "linux":
        prerequisite = (
            "apt-get update -qq && apt-get install -y -qq build-essential >/dev/null && "
            if install == "source"
            else ""
        )
        return [
            " ".join(
                (
                    "docker run --rm --platform linux/amd64",
                    "--mount type=bind,src=${REPO},dst=/work,readonly -w /work",
                    f"python:{python_version}-slim sh -lc",
                    f"'{prerequisite}python -m pip install --disable-pip-version-check",
                    f"{binary_policy} {dependencies} &&",
                    "python tools/m1/compatibility_probe.py'",
                )
            )
        ]
    raise ValueError(f"unsupported platform: {platform_name}")


def _run(command: Sequence[str], *, environment: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=CELL_TIMEOUT_SECONDS,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _probe_result(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("compatibility probe produced no JSON receipt")
    value = json.loads(lines[-1])
    if not isinstance(value, dict):
        raise TypeError("compatibility probe receipt must be an object")
    return value


def _receipt(
    *,
    platform_name: str,
    python_version: str,
    install: str,
    setup: list[dict[str, Any]],
    probe: dict[str, Any] | None,
    probe_execution: dict[str, Any] | None,
) -> dict[str, Any]:
    succeeded = all(step["returncode"] == 0 for step in setup)
    succeeded = succeeded and probe_execution is not None and probe_execution["returncode"] == 0
    succeeded = succeeded and probe is not None and probe.get("status") == "pass"
    return {
        "platform": platform_name,
        "architecture": "arm64" if platform_name == "macos" else "x86_64",
        "python": python_version,
        "install": install,
        "status": "passed" if succeeded else "failed",
        "commands": command_templates(platform_name, python_version, install),
        "setup": [
            {
                "returncode": step["returncode"],
                "stdout_sha256": hashlib.sha256(step["stdout"].encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(step["stderr"].encode()).hexdigest(),
            }
            for step in setup
        ],
        "probe_returncode": None if probe_execution is None else probe_execution["returncode"],
        "probe_stdout": None if probe_execution is None else probe_execution["stdout"].strip(),
        "probe_stderr": None if probe_execution is None else probe_execution["stderr"].strip(),
        "probe": probe,
    }


def _macos_cell(python_version: str, install: str) -> dict[str, Any]:
    if platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise RuntimeError("macOS cells require a native Apple Silicon host")
    setup: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="open-brain-m1-matrix-") as temporary:
        temporary_root = Path(temporary)
        environment = os.environ.copy()
        environment["UV_CACHE_DIR"] = str(
            Path(tempfile.gettempdir()) / "open-brain-m1-uv-cache"
        )
        environment["CONAN_HOME"] = str(temporary_root / "conan")
        virtual_environment = temporary_root / "env"
        setup.append(
            _run(
                ["uv", "venv", "--python", python_version, str(virtual_environment)],
                environment=environment,
            )
        )
        if setup[-1]["returncode"] != 0:
            return _receipt(
                platform_name="macos",
                python_version=python_version,
                install=install,
                setup=setup,
                probe=None,
                probe_execution=None,
            )
        binary_policy = "--only-binary=:all:" if install == "wheel" else "--no-binary=:all:"
        setup.append(
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(virtual_environment / "bin/python"),
                    binary_policy,
                    *DEPENDENCIES,
                ],
                environment=environment,
            )
        )
        if setup[-1]["returncode"] != 0:
            return _receipt(
                platform_name="macos",
                python_version=python_version,
                install=install,
                setup=setup,
                probe=None,
                probe_execution=None,
            )
        probe_execution = _run(
            [str(virtual_environment / "bin/python"), str(PROBE)],
            environment=environment,
        )
        probe = (
            _probe_result(probe_execution["stdout"])
            if probe_execution["returncode"] == 0
            else None
        )
        return _receipt(
            platform_name="macos",
            python_version=python_version,
            install=install,
            setup=setup,
            probe=probe,
            probe_execution=probe_execution,
        )


def _linux_cell(python_version: str, install: str) -> dict[str, Any]:
    binary_policy = "--only-binary=:all:" if install == "wheel" else "--no-binary=:all:"
    prerequisite = (
        "apt-get update -qq && apt-get install -y -qq build-essential >/dev/null && "
        if install == "source"
        else ""
    )
    script = (
        f"{prerequisite}python -m pip install --disable-pip-version-check "
        f"{binary_policy} {' '.join(DEPENDENCIES)} && "
        "python tools/m1/compatibility_probe.py"
    )
    probe_execution = _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--mount",
            f"type=bind,src={ROOT},dst=/work,readonly",
            "-w",
            "/work",
            f"python:{python_version}-slim",
            "sh",
            "-lc",
            script,
        ]
    )
    probe = (
        _probe_result(probe_execution["stdout"])
        if probe_execution["returncode"] == 0
        else None
    )
    return _receipt(
        platform_name="linux",
        python_version=python_version,
        install=install,
        setup=[],
        probe=probe,
        probe_execution=probe_execution,
    )


def run_matrix(
    platforms: Sequence[str],
    python_versions: Sequence[str],
    install_modes: Sequence[str],
) -> dict[str, Any]:
    """Run selected cells and retain their exact probe stdout as durable evidence."""
    cells = []
    for platform_name in platforms:
        for python_version in python_versions:
            for install in install_modes:
                runner = _macos_cell if platform_name == "macos" else _linux_cell
                cells.append(runner(python_version, install))
    return {
        "schema_version": 1,
        "generated_at": date.today().isoformat(),
        "status": "passed" if all(cell["status"] == "passed" for cell in cells) else "failed",
        "probe_path": "tools/m1/compatibility_probe.py",
        "probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
        "runner_path": "tools/m1/compatibility_matrix.py",
        "cells": cells,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("all", "macos", "linux"), default="all")
    parser.add_argument("--python", choices=("all", *PYTHON_VERSIONS), default="all")
    parser.add_argument("--install", choices=("all", *INSTALL_MODES), default="all")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    platforms = ("macos", "linux") if arguments.platform == "all" else (arguments.platform,)
    python_versions = PYTHON_VERSIONS if arguments.python == "all" else (arguments.python,)
    install_modes = INSTALL_MODES if arguments.install == "all" else (arguments.install,)
    receipt = run_matrix(platforms, python_versions, install_modes)
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(encoded, end="")
    else:
        arguments.output.write_text(encoded, encoding="utf-8")
    if receipt["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
