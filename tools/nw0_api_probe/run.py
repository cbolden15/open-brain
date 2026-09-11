"""Run a fixed synthetic NW0 proof in a separate dependency directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.nw0_graphify_probe import run as command_runner

run_command = command_runner.run

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[1]
CANDIDATE_FILES = (
    "async_transport.py",
    "direct_api.py",
    "test_async_transport.py",
    "test_correction.py",
    "test_boundary_adversarial.py",
    "tls_fixture.py",
)


def input_files(candidate_files: tuple[str, ...]) -> tuple[str, ...]:
    return (
        "requirements.txt",
        "dependency-wheels.json",
        *(f"payload/{name}.txt" for name in candidate_files),
        "payload/run_bundle.py.txt",
    )


INPUT_FILES = input_files(CANDIDATE_FILES)


@dataclass(frozen=True)
class ProofProfile:
    name: str
    source: Path
    candidate_files: tuple[str, ...]
    expected_tests: int
    base_sources: tuple[tuple[str, Path, str], ...] = ()


TRANSPORT_PROFILE = ProofProfile("api", SOURCE, CANDIDATE_FILES, 23)
AUTHORITY_PROFILE = ProofProfile(
    "authority", REPO / "tools/nw0_authority_probe",
    (
        "authority_gate.py", "async_bridge.py", "async_transport.py", "direct_api.py",
        "bridge.py", "wire.py", "source_binding.py", "test_authority_gate.py",
        "test_async_authority.py", "test_terminal_ownership.py", "test_host_lifetime.py",
        "test_async_transport.py", "tls_fixture.py", "test_source_binding.py",
        "semantic-dataset.json",
    ),
    28,
    (
        ("engine", REPO / "packages/engine/src",
         "7f64ba6517dcf86a9ec87bf3d0c0be82257ef4ae601f6d3df4891a1f7b15fe05"),
        ("app", REPO / "packages/app/src",
         "acad205b1378c882aeb8b46d86a6ff4236add67d424037ba001e62b28e0238d2"),
    ),
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_inputs(source: Path) -> dict[str, str]:
    return load_inputs(source)[0]


def load_inputs(
    source: Path, candidate_files: tuple[str, ...] = CANDIDATE_FILES,
) -> tuple[dict[str, str], dict[str, bytes], str]:
    manifest_bytes = (source / "source-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    expected_files = input_files(candidate_files)
    if not isinstance(manifest, dict) or set(manifest) != set(expected_files):
        raise ValueError("unexpected proof input inventory")
    result = {}
    contents = {}
    for name in expected_files:
        path = source / name
        expected = manifest[name]
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or path.is_symlink()
            or any(parent.is_symlink() for parent in path.parents if parent != source.parent)
            or not path.is_file()
            or path.stat().st_nlink != 1
        ):
            raise ValueError("proof input binding failed")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("proof input binding failed")
        result[name] = expected
        contents[name] = data
    return result, contents, hashlib.sha256(manifest_bytes).hexdigest()


def materialize(
    source: Path, output: Path, candidate_files: tuple[str, ...] = CANDIDATE_FILES,
) -> tuple[dict[str, str], str]:
    bindings, contents, manifest_sha256 = load_inputs(source, candidate_files)
    candidate = output / "runtime" / "candidate"
    runner = output / "runtime" / "runner"
    candidate.mkdir(parents=True)
    runner.mkdir()
    for name in candidate_files:
        (candidate / name).write_bytes(contents[f"payload/{name}.txt"])
    (candidate / "requirements.txt").write_bytes(contents["requirements.txt"])
    (runner / "run_bundle.py").write_bytes(contents["payload/run_bundle.py.txt"])
    (output / "runtime/dependency-wheels.json").write_bytes(contents["dependency-wheels.json"])
    return bindings, manifest_sha256


def implementation_sha256(manifest_sha256: str) -> dict[str, str]:
    return {
        "coordinator": digest(Path(__file__)),
        "source_manifest": manifest_sha256,
        "command_runner": digest(Path(command_runner.__file__)),
    }


def public_result(
    value: Any, expected_versions: dict[str, str], expected_tests: int = 23,
) -> dict[str, Any]:
    """Project only fixed, path-free fields; never publish raw commands or errors."""
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise ValueError("runner did not pass")
    runs = value.get("runs")
    versions = value.get("dependency_versions")
    if (
        value.get("dependency_pins_match") is not True
        or versions != expected_versions
        or not isinstance(runs, list)
        or len(runs) != 2
    ):
        raise ValueError("incomplete runner receipt")
    selected = []
    for label, row in zip(("normal", "optimized"), runs, strict=True):
        if (
            not isinstance(row, dict)
            or row.get("label") != label
            or type(row.get("tests")) is not int or row["tests"] != expected_tests
            or type(row.get("returncode")) is not int or row["returncode"] != 0
            or row.get("failure") is not None
            or row.get("reaped") is not True
            or row.get("direct_reaped") is not True
            or row.get("group_terminated") is not True
            or row.get("cleanup_known") is not True
            or type(row.get("elapsed_seconds")) not in (int, float)
            or not math.isfinite(row["elapsed_seconds"])
            or not 0 <= row["elapsed_seconds"] <= 90
        ):
            raise ValueError("incomplete test run")
        selected.append({
            key: row[key]
            for key in (
                "label", "tests", "returncode", "reaped", "group_terminated",
                "cleanup_known", "elapsed_seconds",
            )
        })
    return {"runs": selected, "dependency_versions": versions}


def base_source_arguments(profile: ProofProfile) -> list[str]:
    return [
        arg for name, path, _ in profile.base_sources for arg in (f"--{name}-source", str(path))
    ]


def base_source_result(value: Any, profile: ProofProfile) -> dict[str, Any]:
    if not profile.base_sources:
        return {}
    expected = {name: pin for name, _, pin in profile.base_sources}
    if not isinstance(value, dict) or value.get("base_source_sha256") != expected:
        raise ValueError("executed base source binding mismatch")
    return {"base_source_sha256": expected}


def run_proof(profile: ProofProfile) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO / f"build/nw0-{profile.name}")
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    release = output / "release"
    release.mkdir()
    result: dict[str, Any] = {
        "schema": f"open-brain-nw0-{profile.name}-proof-v1",
        "passed": False,
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "model_calls": 0,
        "live_provider_approved": False,
        "authority_integration_approved": False,
        "full_nw0_approved": False,
    }
    phase = "platform"
    try:
        if sys.version_info[:2] != (3, 14) or (platform.system(), platform.machine()) not in {
            ("Darwin", "arm64"), ("Linux", "x86_64"),
        }:
            raise ValueError("unsupported proof target")
        phase = "materialize"
        bindings, manifest_sha256 = materialize(profile.source, output, profile.candidate_files)
        result["source_sha256"] = bindings
        result["implementation_sha256"] = implementation_sha256(manifest_sha256)
        wheels = json.loads((output / "runtime/dependency-wheels.json").read_bytes())
        target = "macos-arm64" if platform.system() == "Darwin" else "linux-x86_64"
        expected_versions = {row["name"]: row["version"] for row in wheels["platforms"][target]}
        executable = shutil.which("uv")
        if executable is None:
            raise ValueError("uv unavailable")
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_CACHE_DIR": str(output / "uv-cache"),
        }
        inputs = output / "inputs"
        command = [
            executable, "--no-config", "pip", "install", "--python", sys.executable,
            "--target", str(inputs), "--no-deps", "--require-hashes", "--only-binary", ":all:",
            "-r", str(output / "runtime/candidate/requirements.txt"),
        ]
        if args.wheelhouse is None:
            command.extend(["--index-url", "https://pypi.org/simple"])
        else:
            command.extend([
                "--no-index", "--find-links", str(args.wheelhouse.resolve(strict=True)),
            ])
        phase = "install"
        run_command(command, output, environment, "install", timeout=120, max_log_bytes=1024 * 1024)
        phase = "tests"
        run_command(
            [
                sys.executable, "-B", str(output / "runtime/runner/run_bundle.py"),
                str(output / "tests"), "--interpreter", sys.executable,
                "--dependency-path", str(inputs),
                *base_source_arguments(profile),
            ],
            output, environment, "runner", timeout=90, max_log_bytes=1024 * 1024,
        )
        receipt = json.loads((output / "tests/receipt.json").read_text())
        bindings = result["source_sha256"]
        if receipt.get("runner_sha256") != bindings["payload/run_bundle.py.txt"]:
            raise ValueError("executed runner binding mismatch")
        candidate_bindings = {
            name: bindings[f"payload/{name}.txt"] for name in profile.candidate_files
        }
        candidate_bindings["requirements.txt"] = bindings["requirements.txt"]
        if receipt.get("candidate_files_sha256") != candidate_bindings:
            raise ValueError("executed payload binding mismatch")
        result.update(public_result(receipt, expected_versions, profile.expected_tests))
        result.update(base_source_result(receipt, profile))
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
            for path in (output / "install.log", output / "runner.log")
            if path.is_file()
        }
        (release / "verification.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n"
        )


def main() -> int:
    return run_proof(TRANSPORT_PROFILE)


if __name__ == "__main__":
    raise SystemExit(main())
