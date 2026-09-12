"""Run the frozen E43 unprivileged transport proof in an isolated directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from tools.nw0_graphify_probe import run as command_runner

run_command = command_runner.run

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[1]
CANDIDATE_FILES = (
    "fixtures.py",
    "owned_transport.py",
    "runtime_probe.py",
    "test_unprivileged_transport.py",
)
INPUT_FILES = (
    "requirements.txt",
    "dependency-wheels.json",
    "patches/pycares-5.0.1-terminal-shutdown.patch",
    *(f"payload/{name}.txt" for name in CANDIDATE_FILES),
    "payload/run_bundle.py.txt",
)
EXPECTED_VERSIONS = {
    "aiodns": "4.0.4",
    "cffi": "2.0.0",
    "cryptography": "46.0.5",
    "pycares": "5.0.1",
    "pycparser": "3.0",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_inputs(source: Path = SOURCE) -> tuple[dict[str, str], dict[str, bytes], str]:
    manifest_bytes = (source / "source-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or set(manifest) != set(INPUT_FILES):
        raise ValueError("unexpected proof input inventory")
    bindings: dict[str, str] = {}
    contents: dict[str, bytes] = {}
    for name in INPUT_FILES:
        path = source / name
        expected = manifest[name]
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_nlink != 1
        ):
            raise ValueError("proof input binding failed")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("proof input binding failed")
        bindings[name] = expected
        contents[name] = content
    return bindings, contents, hashlib.sha256(manifest_bytes).hexdigest()


def materialize(output: Path, source: Path = SOURCE) -> tuple[dict[str, str], str]:
    bindings, contents, manifest_sha256 = load_inputs(source)
    runtime = output / "runtime"
    candidate = runtime / "candidate"
    runner = runtime / "runner"
    candidate.mkdir(parents=True)
    runner.mkdir()
    for name in CANDIDATE_FILES:
        (candidate / name).write_bytes(contents[f"payload/{name}.txt"])
    (candidate / "requirements.txt").write_bytes(contents["requirements.txt"])
    (runner / "run_bundle.py").write_bytes(contents["payload/run_bundle.py.txt"])
    (runtime / "dependency-wheels.json").write_bytes(contents["dependency-wheels.json"])
    (runtime / "pycares.patch").write_bytes(
        contents["patches/pycares-5.0.1-terminal-shutdown.patch"]
    )
    return bindings, manifest_sha256


def target_name() -> str:
    target = (platform.system(), platform.machine())
    if target == ("Darwin", "arm64"):
        return "macos-arm64"
    if target == ("Linux", "x86_64"):
        return "linux-x86_64"
    raise ValueError("unsupported proof target")


def wheel_inventory(path: Path, target: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("schema") != (
        "open-brain-e43-dependency-wheel-manifest-v1"
    ):
        raise ValueError("dependency manifest schema")
    platforms = value.get("platforms")
    if not isinstance(platforms, dict) or set(platforms) != {"macos-arm64", "linux-x86_64"}:
        raise ValueError("dependency platform closure")
    rows = platforms.get(target)
    if not isinstance(rows, list) or {
        row.get("name"): row.get("version") for row in rows if isinstance(row, dict)
    } != EXPECTED_VERSIONS:
        raise ValueError("dependency version closure")
    excluded = value.get("excluded_runtime_dependencies")
    if (
        not isinstance(excluded, list)
        or len(excluded) != 1
        or excluded[0].get("name") != "aiohttp"
        or excluded[0].get("version") != "3.14.3"
    ):
        raise ValueError("aiohttp exclusion boundary")
    return value, rows


def verify_native_members(dependencies: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        loader = row.get("native_loader")
        members = loader.get("native_members") if isinstance(loader, dict) else None
        needed = loader.get("needed") if isinstance(loader, dict) else None
        if not isinstance(members, list) or not isinstance(needed, list):
            raise ValueError("native loader manifest")
        selected = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("native member manifest")
            path = dependencies / str(member.get("path"))
            if (
                not path.is_file()
                or path.stat().st_size != member.get("bytes")
                or digest(path) != member.get("sha256")
            ):
                raise ValueError("native member binding")
            selected.append(
                {
                    "path": member["path"],
                    "bytes": member["bytes"],
                    "sha256": member["sha256"],
                }
            )
        result.append({"name": row["name"], "native_members": selected, "needed": needed})
    return result


def public_receipt(value: Any, bindings: dict[str, str]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema") != "open-brain-e43-unprivileged-runner-v1"
        or value.get("passed") is not True
        or value.get("provider_calls") != 0
        or value.get("dependency_versions") != EXPECTED_VERSIONS
    ):
        raise ValueError("runner receipt boundary")
    expected_candidate = {
        name: bindings[f"payload/{name}.txt"] for name in CANDIDATE_FILES
    }
    expected_candidate["requirements.txt"] = bindings["requirements.txt"]
    if value.get("candidate_files_sha256") != expected_candidate:
        raise ValueError("candidate execution binding")
    runs = value.get("test_runs")
    if not isinstance(runs, list) or len(runs) != 2:
        raise ValueError("test run inventory")
    for label, row in zip(("normal", "optimized"), runs, strict=True):
        if (
            not isinstance(row, dict)
            or row.get("label") != label
            or row.get("tests") != 13
            or row.get("returncode") != 0
            or row.get("reaped") is not True
            or row.get("group_terminated") is not True
            or type(row.get("elapsed_seconds")) not in (int, float)
            or not math.isfinite(row["elapsed_seconds"])
            or not 0 <= row["elapsed_seconds"] <= 30
        ):
            raise ValueError("test run receipt")
    runtime_run = value.get("runtime_run")
    runtime = value.get("runtime")
    if (
        not isinstance(runtime_run, dict)
        or runtime_run.get("returncode") != 0
        or runtime_run.get("reaped") is not True
        or runtime_run.get("group_terminated") is not True
        or not isinstance(runtime, dict)
        or runtime.get("passed") is not True
        or runtime.get("provider_calls") != 0
    ):
        raise ValueError("runtime execution receipt")
    return {
        "test_runs": runs,
        "runtime_run": runtime_run,
        "runtime": runtime,
        "dependency_versions": EXPECTED_VERSIONS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=REPO / "build/nw0-unprivileged-transport"
    )
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    release = output / "release"
    release.mkdir()
    result: dict[str, Any] = {
        "schema": "open-brain-e43-unprivileged-proof-v1",
        "passed": False,
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "provider_calls": 0,
        "product_integration_permitted": False,
        "production_activation": False,
        "privileged_topology_used": False,
        "full_nw0_approved": False,
    }
    phase = "platform"
    try:
        if sys.version_info[:2] != (3, 14):
            raise ValueError("CPython 3.14 required")
        target = target_name()
        phase = "materialize"
        bindings, manifest_sha256 = materialize(output)
        result["source_sha256"] = bindings
        result["implementation_sha256"] = {
            "coordinator": digest(Path(__file__)),
            "source_manifest": manifest_sha256,
            "command_runner": digest(Path(command_runner.__file__)),
        }
        runtime = output / "runtime"
        dependencies = output / "dependencies"
        manifest, rows = wheel_inventory(runtime / "dependency-wheels.json", target)
        executable = shutil.which("uv")
        patch_tool = shutil.which("patch", path="/usr/bin:/bin")
        if executable is None or patch_tool is None:
            raise ValueError("required proof tool unavailable")
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_CACHE_DIR": str(output / "uv-cache"),
        }
        command = [
            executable,
            "--no-config",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(dependencies),
            "--no-deps",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "-r",
            str(runtime / "candidate/requirements.txt"),
        ]
        if args.wheelhouse is None:
            command.extend(["--index-url", "https://pypi.org/simple"])
        else:
            command.extend(
                ["--no-index", "--find-links", str(args.wheelhouse.resolve(strict=True))]
            )
        phase = "install"
        run_command(command, output, environment, "install", timeout=120, max_log_bytes=1024 * 1024)
        pycares_source = dependencies / "pycares/__init__.py"
        patch_metadata = manifest.get("pycares_source")
        if (
            not isinstance(patch_metadata, dict)
            or digest(pycares_source) != patch_metadata.get("module_sha256_before_patch")
            or digest(runtime / "pycares.patch") != patch_metadata.get("patch_sha256")
        ):
            raise ValueError("pycares patch input binding")
        phase = "dependency-patch"
        run_command(
            [
                patch_tool,
                "--batch",
                "--forward",
                "-p1",
                "-d",
                str(dependencies),
                "-i",
                str(runtime / "pycares.patch"),
            ],
            output,
            environment,
            "dependency-patch",
            timeout=10,
            max_log_bytes=64 * 1024,
        )
        if digest(pycares_source) != patch_metadata.get("module_sha256_after_patch"):
            raise ValueError("pycares patch output binding")
        result["dependency_patch"] = patch_metadata
        result["native_loader_closure"] = verify_native_members(dependencies, rows)
        phase = "tests"
        tests = output / "tests"
        tests.mkdir()
        run_command(
            [
                sys.executable,
                "-I",
                "-S",
                str(runtime / "runner/run_bundle.py"),
                str(runtime / "candidate"),
                str(dependencies),
                str(tests),
                sys.executable,
            ],
            output,
            environment,
            "runner",
            timeout=90,
            max_log_bytes=1024 * 1024,
        )
        receipt = json.loads((tests / "receipt.json").read_text())
        if receipt.get("runner_sha256") != bindings["payload/run_bundle.py.txt"]:
            raise ValueError("runner execution binding")
        result.update(public_receipt(receipt, bindings))
        result["passed"] = True
        phase = "complete"
        return 0
    except Exception as error:
        result["failure_class"] = type(error).__name__
        return 1
    finally:
        result["phase"] = phase
        result["local_logs_published"] = False
        result["log_sha256"] = {
            path.name: digest(path)
            for path in output.glob("*.log")
            if path.is_file()
        }
        (release / "verification.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
