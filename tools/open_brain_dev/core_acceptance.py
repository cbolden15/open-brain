"""Run the frozen T23 local macOS acceptance gate against exact native archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final

PUBLIC_CANARY: Final = "T23_PUBLIC_CUSTODY_CANARY_7d64f4"
SECRET_CANARY: Final = "SYNTHETIC_T23_SECRET_CANARY"
PATH_CANARY: Final = "/synthetic/private/t23-path-canary"
PROTECTED_INPUTS: Final = (
    "packages",
    "release",
    "scripts",
    ".github",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    "tools",
)
ALLOWED_ARTIFACT_SOURCE_DRIFT: Final = ("tools/open_brain_dev/documentation_examples.py",)


class AcceptanceError(RuntimeError):
    """The exact candidate or its evidence failed the frozen T23 contract."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"invalid JSON evidence: {path.name}") from error
    if not isinstance(value, dict):
        raise AcceptanceError(f"JSON evidence is not an object: {path.name}")
    return value


def _require_absolute_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise AcceptanceError("gate paths must be absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise AcceptanceError("gate directory has the wrong type")
    return resolved


def _contains(actual: object, required: object) -> bool:
    if isinstance(required, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value) for key, value in required.items()
        )
    if isinstance(required, list):
        return isinstance(actual, list) and actual == required
    return actual == required


def validate_reused_evidence(evidence_dir: Path, contract: Mapping[str, Any]) -> dict[str, str]:
    """Bind reused T20/T21 claims to exact bytes and required result/provenance fields."""
    observed: dict[str, str] = {}
    specifications = contract.get("reused_evidence")
    if not isinstance(specifications, dict):
        raise AcceptanceError("reused evidence contract is missing")
    for name, raw_specification in specifications.items():
        if not isinstance(name, str) or not isinstance(raw_specification, dict):
            raise AcceptanceError("invalid reused evidence contract")
        path = evidence_dir / name
        if not path.is_file():
            raise AcceptanceError(f"required reused evidence is missing: {name}")
        digest = sha256(path)
        if digest != raw_specification.get("sha256"):
            raise AcceptanceError(f"reused evidence digest mismatch: {name}")
        payload = _json_object(path)
        if not _contains(payload, raw_specification.get("required_fields")):
            raise AcceptanceError(f"required evidence field mismatch: {name}")
        observed[name] = digest

    gate = _json_object(evidence_dir / "T21-COMMITTED-GATE.json")
    completed = gate.get("completed_examples")
    examples = gate.get("examples")
    if not isinstance(completed, list) or not isinstance(examples, list):
        raise AcceptanceError("T21 example provenance is missing")
    if len(completed) != 11 or len(examples) != 11:
        raise AcceptanceError("T21 example inventory mismatch")
    identifiers = [item.get("id") for item in examples if isinstance(item, dict)]
    digests = [item.get("sha256") for item in examples if isinstance(item, dict)]
    if completed != identifiers or len(digests) != 11 or not all(
        isinstance(value, str) and len(value) == 64 for value in digests
    ):
        raise AcceptanceError("T21 example identity mismatch")
    if gate.get("runtime_isolation") != {
        "ambient_credentials": False,
        "cwd": "temporary",
        "home": "temporary",
        "path": "/bin:/usr/bin",
        "pythonpath": False,
    }:
        raise AcceptanceError("T21 runtime isolation provenance mismatch")
    return observed


def validate_reviewed_files(root: Path, contract: Mapping[str, Any]) -> dict[str, str]:
    specifications = contract.get("reviewed_files")
    if not isinstance(specifications, dict) or len(specifications) != 21:
        raise AcceptanceError("reviewed-file contract must contain 21 files")
    observed: dict[str, str] = {}
    for relative, expected in specifications.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise AcceptanceError("invalid reviewed-file contract")
        path = root / relative
        if not path.is_file() or sha256(path) != expected:
            raise AcceptanceError(f"reviewed file digest mismatch: {relative}")
        observed[relative] = expected
    return observed


def _git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ("git", *arguments), cwd=root, text=True, capture_output=True, timeout=30, check=False
    )
    if process.returncode != 0:
        raise AcceptanceError("git provenance check failed")
    return process.stdout.rstrip("\n")


def validate_source_preservation(root: Path, contract: Mapping[str, Any]) -> tuple[str, list[str]]:
    source = contract["source"]
    head = _git(root, "rev-parse", "HEAD").strip()
    baseline = source["baseline"]
    ancestor = subprocess.run(
        ("git", "merge-base", "--is-ancestor", baseline, head),
        cwd=root,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if ancestor.returncode != 0:
        raise AcceptanceError("frozen baseline is not an ancestor of source HEAD")
    allowed = set(contract["allowed_repository_paths"])
    committed_scope = set(_git(root, "diff", "--name-only", f"{baseline}..{head}").splitlines())
    if not committed_scope.issubset(allowed):
        raise AcceptanceError("committed repository scope exceeds frozen T23 paths")
    committed = _git(
        root,
        "diff",
        "--name-only",
        f"{source['artifact_source']}..{head}",
        "--",
        *PROTECTED_INPUTS,
    ).splitlines()
    if set(committed) - {"tools/open_brain_dev/core_acceptance.py"} != set(
        ALLOWED_ARTIFACT_SOURCE_DRIFT
    ):
        raise AcceptanceError("artifact build-input preservation mismatch")
    dirty = _dirty_paths(root)
    forbidden = [path for path in dirty if path not in allowed]
    if forbidden:
        raise AcceptanceError("dirty product or build input: " + ", ".join(forbidden))
    return head, dirty


def _dirty_paths(root: Path) -> list[str]:
    output = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    result: list[str] = []
    for line in output.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        result.append(path)
    return sorted(result)


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract only ordinary root-confined files/directories from a trusted-hash archive."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            pure = PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise AcceptanceError(f"unsafe archive member: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise AcceptanceError(f"unsupported archive member: {member.name}")
            target = destination.joinpath(*pure.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise AcceptanceError(f"unreadable archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o777)


def stage_candidate(release_dir: Path, stage: Path, contract: Mapping[str, Any]) -> Path:
    artifacts = contract["artifacts"]
    manifest = release_dir / artifacts["manifest"]["filename"]
    if sha256(manifest) != artifacts["manifest"]["sha256"]:
        raise AcceptanceError("manifest digest mismatch")
    manifest_text = manifest.read_text(encoding="utf-8")
    prefix = stage / "prefix"
    for role in ("base", "graphify"):
        specification = artifacts[role]
        archive = release_dir / specification["filename"]
        if sha256(archive) != specification["archive_sha256"]:
            raise AcceptanceError(f"{role} archive digest mismatch")
        expected_line = (
            f"resource {role} macos-arm64 {specification['archive_sha256']} "
            f"{specification['executable_sha256']} {specification['filename']} "
            f"{specification['destination']}"
        )
        if expected_line not in manifest_text.splitlines():
            raise AcceptanceError(f"manifest resource mismatch: {role}")
        extracted = stage / f"extract-{role}"
        safe_extract(archive, extracted)
        source = extracted / "open-brain"
        destination = prefix / specification["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if sha256(destination) != specification["executable_sha256"]:
            raise AcceptanceError(f"staged executable digest mismatch: {role}")
        if role == "base":
            asset_source = extracted / "obsidian-plugin"
            asset_destination = prefix / "share/open-brain/obsidian-plugin"
            asset_destination.mkdir(parents=True)
            for name, expected in artifacts["obsidian_assets"].items():
                source_asset = asset_source / name
                if sha256(source_asset) != expected:
                    raise AcceptanceError(f"archive asset digest mismatch: {name}")
                shutil.copy2(source_asset, asset_destination / name)
                if sha256(asset_destination / name) != expected:
                    raise AcceptanceError(f"staged asset digest mismatch: {name}")
    return prefix / "bin/open-brain"


def _run(
    executable: Path,
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdin: str | None = None,
    expected: int = 0,
    timeout: int = 30,
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    process = subprocess.run(
        (os.fspath(executable), *arguments),
        cwd=cwd,
        env=dict(environment),
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.monotonic() - started
    if process.returncode != expected:
        raise AcceptanceError(
            f"exact artifact command failed: {arguments[0]} exit {process.returncode}"
        )
    return process, elapsed


def _payload(process: subprocess.CompletedProcess[str], operation: str) -> dict[str, Any]:
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"{operation} did not return JSON") from error
    if not isinstance(payload, dict):
        raise AcceptanceError(f"{operation} JSON is not an object")
    return payload


def _scan_files(root: Path, needle: bytes) -> int:
    return sum(path.read_bytes().count(needle) for path in root.rglob("*") if path.is_file())


def validate_search_result(payload: Mapping[str, Any], capture_id: str) -> None:
    """Require the unique fixture result to identify the capture and its source text."""
    results = payload.get("results")
    if payload.get("status") != "ok" or not isinstance(results, list) or len(results) != 1:
        raise AcceptanceError("default search result mismatch")
    result = results[0]
    if not isinstance(result, dict) or (
        result.get("capture_id") != capture_id
        or result.get("result_id") != capture_id
        or result.get("record_type") != "source"
        or result.get("source_origin") != "owner_authored"
        or result.get("title") != PUBLIC_CANARY
        or PUBLIC_CANARY not in str(result.get("excerpt", ""))
    ):
        raise AcceptanceError("default search did not return the captured source identity")


def validate_export_result(payload: Mapping[str, Any], operation: str) -> None:
    expected = {
        "captures": 1,
        "history_records": 0,
        "portable_files": 3,
        "schema_version": 4,
        "status": "exported",
        "verification": "verified",
    }
    if payload != expected:
        raise AcceptanceError(f"{operation} verified export result mismatch")


def validate_mcp_failure_responses(responses: Sequence[object]) -> None:
    expected_initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "open-brain", "version": "0.1.0"},
        },
    }
    expected_denial = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [{"type": "text", "text": "unknown tool"}],
            "isError": True,
        },
    }
    expected_parse_error = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "parse error"},
    }
    if list(responses) != [expected_initialize, expected_denial, expected_parse_error]:
        raise AcceptanceError("MCP initialize, denial, or malformed-request result mismatch")


def _files_containing(root: Path, needle: bytes) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and needle in path.read_bytes()
    }


def _portable_inventory(root: Path, capture_id: str) -> dict[str, str]:
    capture_pattern = re.compile(
        rf"sources/captures/[0-9]{{4}}/[0-9]{{2}}/{re.escape(capture_id)}\.json"
    )
    files = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
    }
    captures = [relative for relative in files if capture_pattern.fullmatch(relative)]
    expected = {"brain.toml", "portable-manifest.json", "sources/logical-sources.json", *captures}
    if len(captures) != 1 or set(files) != expected:
        raise AcceptanceError("Portable Brain file-role inventory mismatch")
    # The manifest alone carries a new export ID/time and binds the otherwise immutable files.
    return {
        relative: sha256(path)
        for relative, path in files.items()
        if relative != "portable-manifest.json"
    }


def validate_custody(
    data_root: Path,
    first_export: Path,
    post_denial_export: Path,
    *,
    capture_id: str,
    private_paths: Sequence[Path],
) -> dict[str, int]:
    capture_pattern = re.compile(
        rf"sources/captures/[0-9]{{4}}/[0-9]{{2}}/{re.escape(capture_id)}\.json"
    )
    data_public = _files_containing(data_root, PUBLIC_CANARY.encode())
    capture_sources = {path for path in data_public if capture_pattern.fullmatch(path)}
    if len(capture_sources) != 1 or data_public != {
        *capture_sources,
        ".open-brain/state/phase1.sqlite3",
    }:
        raise AcceptanceError("public canary appeared outside expected product source roles")
    source_relative = next(iter(capture_sources))
    first_public = _files_containing(first_export, PUBLIC_CANARY.encode())
    post_public = _files_containing(post_denial_export, PUBLIC_CANARY.encode())
    if first_public != {source_relative} or post_public != {source_relative}:
        raise AcceptanceError("public canary appeared outside expected export source role")
    source_bytes = (data_root / source_relative).read_bytes()
    if (first_export / source_relative).read_bytes() != source_bytes:
        raise AcceptanceError("exported captured source bytes differ from owner source")

    for export_root in (first_export, post_denial_export):
        for forbidden in (
            SECRET_CANARY.encode(),
            PATH_CANARY.encode(),
            b"denied",
            *(os.fspath(path).encode() for path in private_paths),
        ):
            if _scan_files(export_root, forbidden):
                raise AcceptanceError("protected canary, denied text, or local path entered export")
    first_inventory = _portable_inventory(first_export, capture_id)
    post_inventory = _portable_inventory(post_denial_export, capture_id)
    if first_inventory != post_inventory:
        raise AcceptanceError("post-denial Portable Brain logical inventory changed")
    return {
        "owner_capture_source_files": len(capture_sources),
        "owner_database_files": 1,
        "first_export_capture_files": len(first_public),
        "post_denial_export_capture_files": len(post_public),
        "compared_nonvolatile_files": len(first_inventory),
    }


def _hardware_scope() -> dict[str, object]:
    def sysctl(name: str) -> str:
        process = subprocess.run(
            ("/usr/sbin/sysctl", "-n", name),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if process.returncode != 0:
            raise AcceptanceError("hardware scope probe failed")
        return process.stdout.strip()

    memory = int(sysctl("hw.memsize"))
    return {
        "machine": platform.machine(),
        "model": sysctl("machdep.cpu.brand_string"),
        "memory_gib": round(memory / 1024**3),
    }


def _row(
    identifier: str,
    *,
    contract: Mapping[str, Any],
    command: Sequence[str],
    elapsed: float,
    expected: str,
    actual: str,
    acceptance_ids: Sequence[str],
    source_commit: str | None = None,
    fixture_id: str | None = None,
    reused_receipt: str | None = None,
) -> dict[str, Any]:
    artifacts = contract["artifacts"]
    row = {
        "row_id": identifier,
        "acceptance_ids": list(acceptance_ids),
        "source_commit": source_commit or contract["source"]["artifact_source"],
        "artifact_sha256": artifacts["base"]["executable_sha256"],
        "archive_sha256": artifacts["base"]["archive_sha256"],
        "artifact_hashes": {
            "base": artifacts["base"]["executable_sha256"],
            "graphify": artifacts["graphify"]["executable_sha256"],
        },
        "archive_hashes": {
            "base": artifacts["base"]["archive_sha256"],
            "graphify": artifacts["graphify"]["archive_sha256"],
        },
        "obsidian_asset_hashes": artifacts["obsidian_assets"],
        "manifest_sha256": artifacts["manifest"]["sha256"],
        "platform": contract["platform"],
        "fixture_id": fixture_id or contract["fixture"]["id"],
        "command": list(command),
        "expected": expected,
        "actual": actual,
        "status": "passed" if actual == expected else "failed",
        "elapsed_seconds": round(elapsed, 6),
        "open_reason": None,
    }
    if reused_receipt is not None:
        row["reused_receipt"] = reused_receipt
    return row


def run_new_proof(
    executable: Path, stage: Path, contract: Mapping[str, Any]
) -> list[dict[str, Any]]:
    home = stage / "home"
    cwd = stage / "cwd"
    export = stage / "portable-export"
    home.mkdir(mode=0o700)
    cwd.mkdir(mode=0o700)
    environment = {
        "HOME": os.fspath(home),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": os.fspath(stage / "tmp"),
        "LC_ALL": "C.UTF-8",
    }
    Path(environment["TMPDIR"]).mkdir(mode=0o700)
    expected_data = home / "Library/Application Support/open-brain/brain"
    if expected_data.exists():
        raise AcceptanceError("default data directory was preseeded")

    timings: dict[str, float] = {}
    total_started = time.monotonic()
    process, timings["init"] = _run(
        executable, ("init", "--json"), cwd=cwd, environment=environment
    )
    if _payload(process, "init").get("status") != "initialized" or not expected_data.is_dir():
        raise AcceptanceError("default init result mismatch")
    if stat.S_IMODE(expected_data.stat().st_mode) & 0o077:
        raise AcceptanceError("default data directory is not private")
    process, timings["capture"] = _run(
        executable,
        ("capture", PUBLIC_CANARY, "--json"),
        cwd=cwd,
        environment=environment,
    )
    captured = _payload(process, "capture")
    capture_id = captured.get("capture_id")
    if (
        captured.get("status") != "captured"
        or not isinstance(capture_id, str)
        or not re.fullmatch(r"capture_[0-9a-f-]{36}", capture_id)
    ):
        raise AcceptanceError("default capture result mismatch")
    process, timings["search"] = _run(
        executable, ("search", PUBLIC_CANARY, "--json"), cwd=cwd, environment=environment
    )
    search = _payload(process, "search")
    validate_search_result(search, capture_id)
    process, timings["export"] = _run(
        executable,
        ("export", os.fspath(export), "--verify", "--json"),
        cwd=cwd,
        environment=environment,
    )
    validate_export_result(_payload(process, "export"), "initial")
    total_elapsed = time.monotonic() - total_started

    search_before = search
    denied_lines = "\n".join(
        (
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t23","version":"1"}}}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"brain_capture","arguments":{"text":"denied"}}}',
            (
                '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":'
                '{"name":"brain_search","arguments":{"query":'
                f'{json.dumps(SECRET_CANARY)},"query":{json.dumps(PATH_CANARY)}}}}}'
            ),
        )
    ) + "\n"
    process, denied_elapsed = _run(
        executable,
        ("mcp", "--allow-search"),
        cwd=cwd,
        environment=environment,
        stdin=denied_lines,
    )
    projected = process.stdout + process.stderr
    if SECRET_CANARY in projected or PATH_CANARY in projected or PUBLIC_CANARY in projected:
        raise AcceptanceError("canary leaked through projected MCP/error surface")
    try:
        responses = [json.loads(line) for line in process.stdout.splitlines()]
    except json.JSONDecodeError as error:
        raise AcceptanceError("MCP failure proof returned malformed output") from error
    validate_mcp_failure_responses(responses)
    after_process, post_search_elapsed = _run(
        executable, ("search", PUBLIC_CANARY, "--json"), cwd=cwd, environment=environment
    )
    if _payload(after_process, "post-denial search") != search_before:
        raise AcceptanceError("denied MCP write changed captured product state")
    denied_search_process, denied_search_elapsed = _run(
        executable, ("search", "denied", "--json"), cwd=cwd, environment=environment
    )
    denied_search = _payload(denied_search_process, "denied text search")
    if denied_search.get("results"):
        raise AcceptanceError("denied MCP write persisted a product record")

    no_grant, no_grant_elapsed = _run(
        executable, ("mcp",), cwd=cwd, environment=environment, stdin="", expected=2
    )
    if no_grant.stdout or no_grant.stderr != "Open Brain could not parse the command.\n":
        raise AcceptanceError("no-grant refusal result mismatch")
    _, eof_elapsed = _run(
        executable,
        ("mcp", "--allow-search"),
        cwd=cwd,
        environment=environment,
        stdin="",
        timeout=5,
    )
    if eof_elapsed >= 5:
        raise AcceptanceError("foreground MCP EOF exit was not bounded")

    post_denial_export = stage / "portable-export-post-denial"
    post_process, post_export_elapsed = _run(
        executable,
        ("export", os.fspath(post_denial_export), "--verify", "--json"),
        cwd=cwd,
        environment=environment,
    )
    validate_export_result(_payload(post_process, "post-denial export"), "post-denial")
    custody_started = time.monotonic()
    custody = validate_custody(
        expected_data,
        export,
        post_denial_export,
        capture_id=capture_id,
        private_paths=(stage, home, cwd, Path(environment["TMPDIR"]), executable.parent.parent),
    )
    custody_scan_elapsed = time.monotonic() - custody_started

    custody_result = (
        "public source retained in inspected owner roles; "
        "protected values absent from inspected exports"
    )
    return [
        _row(
            "default-home-journey",
            contract=contract,
            command=(
                "open-brain init --json",
                "open-brain capture <synthetic-public-canary> --json",
                "open-brain search <synthetic-public-canary> --json",
                "open-brain export <synthetic-export> --verify --json",
            ),
            elapsed=total_elapsed,
            expected="initialized, captured, searchable, verified export",
            actual="initialized, captured, searchable, verified export",
            acceptance_ids=("A01", "A05", "A15", "A17"),
        )
        | {"phase_seconds": {key: round(value, 6) for key, value in timings.items()}},
        _row(
            "mcp-failure-redaction",
            contract=contract,
            command=(
                "open-brain mcp <EOF> [no grants] => exit 2",
                "open-brain mcp --allow-search <fixture:mcp-denial-and-malformed-jsonl>",
                "open-brain search <synthetic-public-canary> --json [post-denial]",
                "open-brain search denied --json [post-denial]",
            ),
            elapsed=(
                denied_elapsed + no_grant_elapsed + post_search_elapsed + denied_search_elapsed
            ),
            expected="no-grant refused; read-only capture denied; malformed request redacted",
            actual="no-grant refused; read-only capture denied; malformed request redacted",
            acceptance_ids=("A02", "A05", "A17"),
        )
        | {
            "phase_seconds": {
                "authorized_mcp_denials": round(denied_elapsed, 6),
                "no_grant_refusal": round(no_grant_elapsed, 6),
                "post_denial_public_search": round(post_search_elapsed, 6),
                "post_denial_denied_text_search": round(denied_search_elapsed, 6),
            }
        },
        _row(
            "custody-export",
            contract=contract,
            command=(
                "open-brain export <synthetic-post-denial-export> --verify --json",
                "harness:inspect expected source and Portable Brain file roles",
                "harness:compare nonvolatile Portable Brain inventory",
            ),
            elapsed=post_export_elapsed + custody_scan_elapsed,
            expected=custody_result,
            actual=custody_result,
            acceptance_ids=("A01", "A17"),
        )
        | {
            "custody": custody,
            "phase_seconds": {
                "post_denial_verified_export": round(post_export_elapsed, 6),
                "role_path_and_inventory_scan": round(custody_scan_elapsed, 6),
            },
        },
        _row(
            "foreground-eof",
            contract=contract,
            command=("open-brain mcp --allow-search <EOF>",),
            elapsed=eof_elapsed,
            expected="foreground process exits successfully before 5 seconds",
            actual="foreground process exits successfully before 5 seconds",
            acceptance_ids=("A05", "A17"),
        ),
    ]


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def _sha256_value(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _commit_value(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _utc_value(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        return None
    return parsed


def validate_final_receipt(receipt: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    """Fail closed on incomplete matrices, false results, dirty certification, or canary leaks."""
    serialized = json.dumps(receipt, sort_keys=True, ensure_ascii=False)
    if any(value in serialized for value in (PUBLIC_CANARY, SECRET_CANARY, PATH_CANARY)):
        raise AcceptanceError("canary leak in public/private bounded receipt")
    required_header = {
        "schema_version",
        "result",
        "certification_scope",
        "global_acceptance_complete",
        "head",
        "baseline",
        "baseline_ancestor",
        "dirty_paths",
        "platform",
        "hardware",
        "artifacts",
        "contract_sha256",
        "harness_sha256",
        "input_evidence_sha256",
        "reviewed_file_count",
        "reviewed_files_aggregate_sha256",
        "utc_started_at",
        "utc_finished_at",
        "monotonic_elapsed_seconds",
        "rows",
        "counts",
        "staging_to_export_seconds",
        "open_gates",
        "claims",
    }
    if not required_header.issubset(receipt):
        raise AcceptanceError("mandatory receipt header field missing")
    if (
        receipt["schema_version"] != 1
        or receipt["certification_scope"] != "local macOS arm64 T23 only"
        or receipt["global_acceptance_complete"] is not False
        or receipt["platform"] != contract["platform"]
        or receipt["baseline"] != contract["source"]["baseline"]
        or not _commit_value(receipt["head"])
    ):
        raise AcceptanceError("receipt scope or source header mismatch")
    dirty = receipt["dirty_paths"]
    if (
        not isinstance(dirty, list)
        or dirty != sorted(set(dirty))
        or not all(
            isinstance(path, str) and path in contract["allowed_repository_paths"] for path in dirty
        )
    ):
        raise AcceptanceError("receipt dirty-path inventory is invalid")
    expected_result = "interim_passed" if dirty else "certified"
    if receipt["result"] != expected_result:
        raise AcceptanceError("premature certification or invalid interim result")
    if not _sha256_value(receipt["contract_sha256"]) or not _sha256_value(
        receipt["harness_sha256"]
    ):
        raise AcceptanceError("receipt contract or harness hash is invalid")
    expected_evidence = {
        name: specification["sha256"]
        for name, specification in contract["reused_evidence"].items()
    }
    if receipt["input_evidence_sha256"] != expected_evidence:
        raise AcceptanceError("receipt input evidence association mismatch")
    if receipt["reviewed_file_count"] != 21 or not _sha256_value(
        receipt["reviewed_files_aggregate_sha256"]
    ):
        raise AcceptanceError("reviewed-file receipt binding is invalid")
    utc_started = _utc_value(receipt["utc_started_at"])
    utc_finished = _utc_value(receipt["utc_finished_at"])
    if utc_started is None or utc_finished is None or utc_finished < utc_started:
        raise AcceptanceError("receipt UTC interval is invalid")
    if not _finite_nonnegative(receipt["monotonic_elapsed_seconds"]) or not _finite_nonnegative(
        receipt["staging_to_export_seconds"]
    ):
        raise AcceptanceError("receipt timing is invalid")
    if receipt["staging_to_export_seconds"] > receipt["monotonic_elapsed_seconds"]:
        raise AcceptanceError("staging-to-export timing exceeds total timing")
    expected_claims = {
        "historical_300_second_gui_provider_clock": "not_claimed",
        "hostile_same_user_isolation": "not_claimed",
        "production_credential_custody": "not_claimed",
        "public_readiness": "not_claimed",
    }
    if receipt["claims"] != expected_claims:
        raise AcceptanceError("receipt claim boundary mismatch")

    rows = receipt.get("rows")
    if not isinstance(rows, list):
        raise AcceptanceError("receipt rows are missing")
    by_identifier = {
        row.get("row_id"): row for row in rows if isinstance(row, dict) and row.get("row_id")
    }
    required = contract["receipt_contract"]["required_rows"]
    if set(by_identifier) != set(required) or len(rows) != len(required):
        raise AcceptanceError("mandatory row inventory mismatch")
    required_fields = {
        "row_id",
        "acceptance_ids",
        "source_commit",
        "artifact_sha256",
        "archive_sha256",
        "artifact_hashes",
        "archive_hashes",
        "obsidian_asset_hashes",
        "manifest_sha256",
        "platform",
        "fixture_id",
        "command",
        "expected",
        "actual",
        "status",
        "elapsed_seconds",
        "open_reason",
    }
    for identifier in required:
        row = by_identifier[identifier]
        if not required_fields.issubset(row):
            raise AcceptanceError(f"mandatory row fields missing: {identifier}")
        if row["status"] != "passed" or row["actual"] != row["expected"]:
            raise AcceptanceError(f"row did not pass: {identifier}")
        if not _finite_nonnegative(row["elapsed_seconds"]):
            raise AcceptanceError(f"invalid row timing: {identifier}")
        if (
            not isinstance(row["command"], list)
            or not row["command"]
            or not all(isinstance(command, str) and command for command in row["command"])
        ):
            raise AcceptanceError(f"invalid row command: {identifier}")
        artifacts = contract["artifacts"]
        if row["platform"] != contract["platform"]:
            raise AcceptanceError(f"row platform mismatch: {identifier}")
        if row["artifact_sha256"] != artifacts["base"]["executable_sha256"]:
            raise AcceptanceError(f"row artifact mismatch: {identifier}")
        if row["archive_sha256"] != artifacts["base"]["archive_sha256"]:
            raise AcceptanceError(f"row archive mismatch: {identifier}")
        if row["manifest_sha256"] != artifacts["manifest"]["sha256"]:
            raise AcceptanceError(f"row manifest mismatch: {identifier}")
        expected_artifacts = {
            "base": artifacts["base"]["executable_sha256"],
            "graphify": artifacts["graphify"]["executable_sha256"],
        }
        expected_archives = {
            "base": artifacts["base"]["archive_sha256"],
            "graphify": artifacts["graphify"]["archive_sha256"],
        }
        if row["artifact_hashes"] != expected_artifacts:
            raise AcceptanceError(f"row artifact inventory mismatch: {identifier}")
        if row["archive_hashes"] != expected_archives:
            raise AcceptanceError(f"row archive inventory mismatch: {identifier}")
        if row["obsidian_asset_hashes"] != artifacts["obsidian_assets"]:
            raise AcceptanceError(f"row plugin asset inventory mismatch: {identifier}")
        expected_source = (
            contract["source"]["t21_implementation"]
            if identifier == "reused-documentation-journey"
            else contract["source"]["artifact_source"]
        )
        if row["source_commit"] != expected_source:
            raise AcceptanceError(f"row source mismatch: {identifier}")
        new_contract = contract["receipt_contract"]["new_row_contracts"].get(identifier)
        if new_contract is not None and any(
            row[field] != new_contract[field]
            for field in ("acceptance_ids", "fixture_id", "command", "expected")
        ):
            raise AcceptanceError(
                f"row command, fixture, or acceptance mapping mismatch: {identifier}"
            )

    native_row = by_identifier["reused-native-homebrew"]
    if (
        native_row.get("fixture_id") != "T20 retained native/Homebrew receipt"
        or native_row.get("acceptance_ids") != ["A14", "A15", "A17"]
        or native_row.get("command") != ["make", "-j1", "native-audit", "homebrew-smoke"]
        or native_row.get("expected")
        != "native audit and local-file Homebrew journey passed"
        or native_row.get("elapsed_seconds") != 79.73
        or native_row.get("reused_receipt")
        != contract["reused_evidence"]["T20-NATIVE-HOMEBREW.json"]["sha256"]
        or native_row.get("original_started_at")
        != contract["reused_evidence"]["T20-NATIVE-HOMEBREW.json"]["required_fields"][
            "started_at"
        ]
    ):
        raise AcceptanceError("T20 reused row provenance mismatch")
    documentation_row = by_identifier["reused-documentation-journey"]
    documentation_command = documentation_row.get("command")
    if (
        documentation_row.get("fixture_id") != "T21 retained 11-block synthetic journey"
        or documentation_row.get("acceptance_ids")
        != ["A01", "A04", "A05", "A14", "A16", "A17"]
        or documentation_row.get("expected")
        != "all 11 reviewed documentation blocks completed"
        or not isinstance(documentation_command, list)
        or documentation_command[:7]
        != [
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "tools.open_brain_dev.documentation_examples",
            "--artifact",
        ]
        or len(documentation_command) != 12
        or documentation_command[8] != "--root"
        or documentation_command[10] != "--output"
        or documentation_row.get("elapsed_seconds") != 26.891
        or documentation_row.get("reused_receipt")
        != contract["reused_evidence"]["T21-COMMITTED-GATE.json"]["sha256"]
        or documentation_row.get("original_started_at")
        != contract["reused_evidence"]["T21-COMMITTED-GATE-RUN.json"]["required_fields"][
            "started_at"
        ]
        or documentation_row.get("shared_aggregate_timing") is not True
        or documentation_row.get("completed_block_count") != 11
    ):
        raise AcceptanceError("T21 reused row provenance mismatch")
    for identifier, phase_names in {
        "default-home-journey": {"init", "capture", "search", "export"},
        "mcp-failure-redaction": {
            "authorized_mcp_denials",
            "no_grant_refusal",
            "post_denial_public_search",
            "post_denial_denied_text_search",
        },
        "custody-export": {"post_denial_verified_export", "role_path_and_inventory_scan"},
    }.items():
        row = by_identifier[identifier]
        phases = row.get("phase_seconds")
        if (
            not isinstance(phases, dict)
            or set(phases) != phase_names
            or not all(_finite_nonnegative(value) for value in phases.values())
            or sum(phases.values()) > row["elapsed_seconds"] + 0.0001
        ):
            raise AcceptanceError(f"row phase timing mismatch: {identifier}")
    if by_identifier["foreground-eof"]["elapsed_seconds"] >= 5:
        raise AcceptanceError("foreground EOF timing is not bounded")
    if by_identifier["custody-export"].get("custody") != {
        "owner_capture_source_files": 1,
        "owner_database_files": 1,
        "first_export_capture_files": 1,
        "post_denial_export_capture_files": 1,
        "compared_nonvolatile_files": 3,
    }:
        raise AcceptanceError("custody observation inventory mismatch")
    expected_staging_to_export = (
        by_identifier["archive-staging"]["elapsed_seconds"]
        + by_identifier["default-home-journey"]["elapsed_seconds"]
    )
    if not math.isclose(
        receipt["staging_to_export_seconds"], expected_staging_to_export, abs_tol=0.000002
    ):
        raise AcceptanceError("staging-to-export row timing association mismatch")
    if receipt.get("baseline_ancestor") is not True:
        raise AcceptanceError("source does not descend from frozen baseline")
    if receipt.get("artifacts") != contract["artifacts"]:
        raise AcceptanceError("receipt artifact inventory mismatch")
    hardware = receipt.get("hardware")
    if (
        not isinstance(hardware, dict)
        or set(hardware) != {"machine", "model", "memory_gib"}
        or hardware.get("machine") != "arm64"
        or not isinstance(hardware.get("model"), str)
        or not hardware["model"]
        or isinstance(hardware.get("memory_gib"), bool)
        or not isinstance(hardware.get("memory_gib"), int)
        or hardware["memory_gib"] <= 0
    ):
        raise AcceptanceError("receipt hardware scope mismatch")
    expected_counts = {
        "new": len(contract["receipt_contract"]["new_rows"]),
        "reused": len(contract["receipt_contract"]["reused_rows"]),
        "open": len(contract["open_gates"]),
    }
    if receipt.get("counts") != expected_counts:
        raise AcceptanceError("receipt row counts mismatch")
    open_gates = receipt.get("open_gates")
    expected_open = [
        {"status": "open", "reason": reason} for reason in contract["open_gates"]
    ]
    if open_gates != expected_open:
        raise AcceptanceError("open gate inventory mismatch")


def run_gate(
    root: Path, release_dir: Path, evidence_dir: Path, output: Path, contract_path: Path
) -> dict[str, Any]:
    root = _require_absolute_directory(root)
    release_dir = _require_absolute_directory(release_dir)
    evidence_dir = _require_absolute_directory(evidence_dir)
    if not output.is_absolute() or output.parent.resolve(strict=True) != evidence_dir:
        raise AcceptanceError("output must be an absolute child of the evidence directory")
    contract_path = contract_path.resolve(strict=True)
    contract = _json_object(contract_path)
    if contract.get("schema_version") != 1 or contract.get("platform") != "macos-arm64":
        raise AcceptanceError("unsupported T23 contract")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise AcceptanceError("T23 gate requires macOS arm64")
    for name, value in (
        ("public", PUBLIC_CANARY),
        ("secret", SECRET_CANARY),
        ("path", PATH_CANARY),
    ):
        expected = contract["fixture"][f"{name}_canary_sha256"]
        if hashlib.sha256(value.encode()).hexdigest() != expected:
            raise AcceptanceError(f"synthetic {name} canary identity mismatch")

    utc_started = datetime.now(UTC).isoformat()
    monotonic_started = time.monotonic()
    head, dirty = validate_source_preservation(root, contract)
    reviewed = validate_reviewed_files(root, contract)
    reused = validate_reused_evidence(evidence_dir, contract)
    # macOS /var is a symlink; default-data bootstrap deliberately rejects symlinked
    # path components. Keep the disposable root below the already validated private
    # evidence directory so HOME has an ordinary absolute path.
    with tempfile.TemporaryDirectory(prefix="T23-stage-", dir=evidence_dir) as temporary:
        stage = Path(temporary)
        stage_started = time.monotonic()
        executable = stage_candidate(release_dir, stage, contract)
        staging_elapsed = time.monotonic() - stage_started
        rows = [
            _row(
                "reused-native-homebrew",
                contract=contract,
                command=("make", "-j1", "native-audit", "homebrew-smoke"),
                elapsed=79.73,
                expected="native audit and local-file Homebrew journey passed",
                actual="native audit and local-file Homebrew journey passed",
                acceptance_ids=("A14", "A15", "A17"),
                source_commit=contract["source"]["artifact_source"],
                fixture_id="T20 retained native/Homebrew receipt",
                reused_receipt=reused["T20-NATIVE-HOMEBREW.json"],
            )
            | {
                "original_started_at": _json_object(
                    evidence_dir / "T20-NATIVE-HOMEBREW.json"
                )["started_at"]
            },
            _row(
                "reused-documentation-journey",
                contract=contract,
                command=tuple(
                    _json_object(evidence_dir / "T21-COMMITTED-GATE-RUN.json")["command"]
                ),
                elapsed=26.891,
                expected="all 11 reviewed documentation blocks completed",
                actual="all 11 reviewed documentation blocks completed",
                acceptance_ids=("A01", "A04", "A05", "A14", "A16", "A17"),
                source_commit=contract["source"]["t21_implementation"],
                fixture_id="T21 retained 11-block synthetic journey",
                reused_receipt=reused["T21-COMMITTED-GATE.json"],
            )
            | {
                "original_started_at": _json_object(
                    evidence_dir / "T21-COMMITTED-GATE-RUN.json"
                )["started_at"],
                "shared_aggregate_timing": True,
                "completed_block_count": 11,
            },
            _row(
                "archive-staging",
                contract=contract,
                command=(
                    "harness:verify manifest/archive members and stage base/helper/plugin roles",
                ),
                elapsed=staging_elapsed,
                expected="safe members and exact staged base/helper/plugin hashes",
                actual="safe members and exact staged base/helper/plugin hashes",
                acceptance_ids=("A15", "A17"),
            ),
            *run_new_proof(executable, stage, contract),
        ]
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "result": "certified" if not dirty else "interim_passed",
        "certification_scope": "local macOS arm64 T23 only",
        "global_acceptance_complete": False,
        "head": head,
        "baseline": contract["source"]["baseline"],
        "baseline_ancestor": True,
        "dirty_paths": dirty,
        "platform": "macos-arm64",
        "hardware": _hardware_scope(),
        "artifacts": contract["artifacts"],
        "contract_sha256": sha256(contract_path),
        "harness_sha256": sha256(root / "tools/open_brain_dev/core_acceptance.py"),
        "input_evidence_sha256": reused,
        "reviewed_file_count": len(reviewed),
        "reviewed_files_aggregate_sha256": hashlib.sha256(
            json.dumps(reviewed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "utc_started_at": utc_started,
        "utc_finished_at": datetime.now(UTC).isoformat(),
        "monotonic_elapsed_seconds": round(time.monotonic() - monotonic_started, 6),
        "rows": rows,
        "counts": {
            "new": 5,
            "reused": 2,
            "open": len(contract["open_gates"]),
        },
        "staging_to_export_seconds": round(
            staging_elapsed
            + next(
                row["elapsed_seconds"]
                for row in rows
                if row["row_id"] == "default-home-journey"
            ),
            6,
        ),
        "open_gates": [
            {"status": "open", "reason": reason} for reason in contract["open_gates"]
        ],
        "claims": {
            "historical_300_second_gui_provider_clock": "not_claimed",
            "hostile_same_user_isolation": "not_claimed",
            "production_credential_custody": "not_claimed",
            "public_readiness": "not_claimed",
        },
    }
    validate_final_receipt(receipt, contract)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, output)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    contract = arguments.root / "docs/acceptance/core-v01-t23.json"
    try:
        receipt = run_gate(
            arguments.root,
            arguments.release_dir,
            arguments.evidence_dir,
            arguments.output,
            contract,
        )
    except AcceptanceError as error:
        print(f"core acceptance failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "result": receipt["result"],
                "new_rows": receipt["counts"]["new"],
                "reused_rows": receipt["counts"]["reused"],
                "open_rows": receipt["counts"]["open"],
                "elapsed_seconds": receipt["monotonic_elapsed_seconds"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
