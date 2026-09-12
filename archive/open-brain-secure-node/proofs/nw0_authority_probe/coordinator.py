"""Prepare the fixed authority bundle and project its native proof receipt."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tools.nw0_api_probe import run

MODULES = [
    "test_authority_gate", "test_async_authority", "test_host_lifetime",
    "test_terminal_ownership", "test_credential_preflight", "test_preflight_close",
]
TARGETS = {"macos-arm64", "linux-x86_64"}


def regular(path: Path) -> bytes:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("linked proof input")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("nonregular proof input")
    return path.read_bytes()


def bound_bytes(path: Path, expected: str) -> bytes:
    data = regular(path)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("materialized input changed")
    return data


def source_snapshot(source: Path, destination: Path, expected: str) -> None:
    """Copy verified source bytes once; never execute repository bytecode caches."""
    if any(part.is_symlink() for part in (source, *source.parents)):
        raise ValueError("linked source root")
    contents = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("linked source input")
        if path.is_dir():
            continue
        data = regular(path)
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        contents[path.relative_to(source).as_posix()] = data
    digest = hashlib.sha256()
    for name, data in contents.items():
        digest.update(name.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    if digest.hexdigest() != expected:
        raise ValueError("base source snapshot mismatch")
    destination.mkdir(parents=True)
    for name, data in contents.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def target_inputs(
    metadata: Any, manifests: Any, target: str,
) -> tuple[list[dict[str, Any]], str, str]:
    if (
        target not in TARGETS
        or not isinstance(metadata, dict)
        or not isinstance(manifests, dict)
        or set(manifests) != TARGETS
        or set(metadata.get("platforms", {})) != TARGETS
        or set(metadata.get("dependency_tree_sha256", {})) != TARGETS
    ):
        raise ValueError("invalid fixed authority targets")
    rows = metadata["platforms"][target]
    manifest = manifests[target]
    dependency = metadata["dependency_tree_sha256"][target]
    if (
        not isinstance(rows, list) or len(rows) != 14
        or len({row["filename"] for row in rows}) != 14
        or len({row["name"] for row in rows}) != 14
    ):
        raise ValueError("authority requires fourteen distinct wheels")
    for value in [manifest, dependency, *(row["sha256"] for row in rows)]:
        if (
            not isinstance(value, str) or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("invalid authority digest")
    for row in rows:
        url = urlsplit(row["url"])
        if (
            Path(row["filename"]).name != row["filename"]
            or not row["filename"].endswith(".whl")
            or type(row["bytes"]) is not int or not 0 < row["bytes"] <= 16 * 1024 * 1024
            or url.scheme != "https" or url.hostname != "files.pythonhosted.org"
            or url.username is not None or url.password is not None
            or url.port not in (None, 443) or url.query or url.fragment
            or Path(url.path).name != row["filename"]
        ):
            raise ValueError("invalid pinned wheel artifact")
    return rows, manifest, dependency


def prepare_wheels(
    rows: list[dict[str, Any]], output: Path, wheelhouse: Path | None,
    environment: dict[str, str],
) -> Path:
    destination = output / "wheels"
    destination.mkdir()
    if wheelhouse is not None:
        if any(part.is_symlink() for part in (wheelhouse, *wheelhouse.parents)):
            raise ValueError("linked wheelhouse")
        if {path.name for path in wheelhouse.iterdir()} != {row["filename"] for row in rows}:
            raise ValueError("wheelhouse must match the selected fourteen archives")
    executable = None if wheelhouse is not None else shutil.which("curl")
    if wheelhouse is None and executable is None:
        raise ValueError("curl unavailable")
    for index, row in enumerate(rows):
        target = destination / row["filename"]
        if wheelhouse is not None:
            data = regular(wheelhouse / row["filename"])
        else:
            run.run_command(
                [
                    str(executable), "--disable", "--fail", "--silent", "--show-error",
                    "--proto", "=https", "--connect-timeout", "5", "--max-time", "15",
                    "--max-filesize", str(row["bytes"]), "--output", str(target),
                    "--url", row["url"],
                ],
                output, environment, f"wheel-{index:02d}", timeout=20, max_log_bytes=4096,
            )
            data = regular(target)
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("selected wheel bytes mismatch")
        if wheelhouse is not None:
            target.write_bytes(data)
    return destination


def public_result(
    value: Any, preflight: Any, *, profile: run.ProofProfile, target: str,
    rows: list[dict[str, Any]], manifest: str, dependency: str, bindings: dict[str, str],
) -> dict[str, Any]:
    versions = {row["name"]: row["version"] for row in rows}
    wheels = {row["filename"]: row["sha256"] for row in rows}
    candidate = {name: bindings[f"payload/{name}.txt"] for name in profile.candidate_files}
    candidate["requirements.txt"] = bindings["requirements.txt"]
    sources = {name: digest for name, _, digest in profile.base_sources}
    candidate_digest = hashlib.sha256(b"".join(
        name.encode() + b"\0" + digest.encode() + b"\n"
        for name, digest in sorted(candidate.items())
    )).hexdigest()
    snapshots = {
        "candidate": candidate_digest, "dependencies": dependency,
        **{f"source/{name}": digest for name, digest in sources.items()},
    }
    runtime = {
        "system": "Darwin" if target == "macos-arm64" else "Linux",
        "machine": "arm64" if target == "macos-arm64" else "x86_64",
        "python_implementation": "CPython", "python_version": "3.14",
    }
    expected = {
        "schema": "open-brain-native-authority-receipt-v1",
        "target": target,
        "runtime": runtime,
        "dependency_tree_sha256": dependency,
        "manifest_sha256": manifest,
        "runner_sha256": bindings["payload/run_bundle.py.txt"],
        "candidate_files_sha256": candidate,
        "base_source_sha256": sources,
        "dependency_wheels_sha256": wheels,
        "dependency_versions": versions,
        "isolated_flags": ["-I", "-S", "-B"],
        "modules": MODULES,
        "expected_tests_per_run": 40,
        "persisted_key_material": False,
    }
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("authority receipt binding mismatch")
    if (
        value.get("snapshot_sha256") != snapshots
        or value.get("runtime_source_root") != "candidate"
        or value.get("base_source_roots") != {"engine": "source/engine", "app": "source/app"}
        or value.get("dependency_roots") != ["dependencies"]
        or not isinstance(preflight, dict) or preflight.get("ok") is not True
        or preflight.get("schema") != "open-brain-native-authority-preflight-v1"
        or preflight.get("target") != target
        or preflight.get("runtime") != runtime
        or preflight.get("dependency_tree_sha256") != dependency
        or preflight.get("manifest_sha256") != manifest
        or preflight.get("runner_sha256") != expected["runner_sha256"]
        or preflight.get("versions") != versions
        or preflight.get("isolated_flags") != ["-I", "-S", "-B"]
        or preflight.get("snapshot_sha256") != value.get("snapshot_sha256")
        or preflight.get("import_origins") != {
            "open_brain.profile": "source/app",
            "open_brain_engine.core.ids": "source/engine", "rfc8785": "dependencies",
        }
    ):
        raise ValueError("authority preflight or runtime roots mismatch")
    if (
        value.get("ephemeral_tls") is not True or value.get("persisted_key_material") is not False
        or type(value.get("model_calls")) is not int or value["model_calls"] != 0
        or value.get("live_provider_approved") is not False
        or value.get("full_nw0_approved") is not False
    ):
        raise ValueError("authority synthetic scope mismatch")
    runs = value.get("runs")
    if not isinstance(runs, list) or any(
        not isinstance(row, dict) or row.get("truncated") is not False for row in runs
    ):
        raise ValueError("truncated authority run")
    selected = run.public_result({**value, "dependency_pins_match": True}, versions, 40)
    return {
        **selected, "target": target, "manifest_sha256": manifest,
        "runner_sha256": expected["runner_sha256"], "base_source_sha256": sources,
        "dependency_tree_sha256": dependency, "dependency_wheels_sha256": wheels,
        "candidate_files_sha256": candidate, "isolated_flags": ["-I", "-S", "-B"],
        "modules": MODULES, "metadata_preflight_passed": True,
        "synthetic_authority_proof_passed": True,
    }


def execute(
    profile: run.ProofProfile, output: Path, wheelhouse: Path | None,
    target: str, bindings: dict[str, str],
) -> dict[str, Any]:
    runtime = output / "runtime"
    metadata = json.loads(bound_bytes(
        runtime / "dependency-wheels.json", bindings["dependency-wheels.json"],
    ))
    manifests = json.loads(bound_bytes(
        runtime / "expected-bundle-manifests.json", bindings["expected-bundle-manifests.json"],
    ))
    rows, manifest, dependency = target_inputs(metadata, manifests, target)
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    for name, source, expected in profile.base_sources:
        source_snapshot(source, output / "source-inputs" / name, expected)
    wheels = prepare_wheels(rows, output, wheelhouse, environment)
    bundle = output / "bundle"
    command = [
        sys.executable, "-I", "-B", str(runtime / "runner/run_bundle.py"), "build",
        "--target", target, "--output", str(bundle),
        "--candidate", str(runtime / "candidate"),
        "--engine", str(output / "source-inputs/engine"),
        "--app", str(output / "source-inputs/app"), "--wheels", str(wheels),
        "--rfc8785", str(wheels / "rfc8785-0.1.4-py3-none-any.whl"),
    ]
    runner_digest = bindings["payload/run_bundle.py.txt"]
    bound_bytes(runtime / "runner/run_bundle.py", runner_digest)
    run.run_command(command, output, environment, "build", timeout=30, max_log_bytes=262144)
    if run.digest(bundle / "manifest.json") != manifest:
        raise ValueError("built manifest differs from external fixed anchor")
    prefix = [sys.executable, "-I", "-B", str(bundle / "run_bundle.py")]
    common = [
        "--target", target, "--bundle", str(bundle), "--interpreter", sys.executable,
        "--expected-manifest-sha256", manifest,
    ]
    bound_bytes(bundle / "run_bundle.py", runner_digest)
    run.run_command(
        [*prefix, "preflight", *common, "--receipt", str(output / "preflight.json")],
        output, environment, "preflight", timeout=20, max_log_bytes=262144,
    )
    bound_bytes(bundle / "run_bundle.py", runner_digest)
    run.run_command(
        [*prefix, "suite", *common, "--output", str(output / "tests"), "--child-timeout", "30"],
        output, environment, "runner", timeout=75, max_log_bytes=262144,
    )
    return public_result(
        json.loads(regular(output / "tests/receipt.json")),
        json.loads(regular(output / "preflight.json")), profile=profile, target=target,
        rows=rows, manifest=manifest, dependency=dependency, bindings=bindings,
    )
