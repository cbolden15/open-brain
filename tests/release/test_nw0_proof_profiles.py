from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tools.nw0_api_probe import run


def test_fixed_profile_preserves_transport() -> None:
    assert run.TRANSPORT_PROFILE.source == run.SOURCE
    assert run.TRANSPORT_PROFILE.candidate_files == run.CANDIDATE_FILES
    assert run.TRANSPORT_PROFILE.expected_tests == 23


def receipt(tests: int) -> dict[str, Any]:
    return {
        "ok": True,
        "dependency_pins_match": True,
        "dependency_versions": {"example": "1"},
        "runs": [
            {
                "label": label, "tests": tests, "returncode": 0, "failure": None,
                "reaped": True, "direct_reaped": True, "group_terminated": True,
                "cleanup_known": True, "elapsed_seconds": 1.0,
            }
            for label in ("normal", "optimized")
        ],
    }


def test_receipt_counts_match_the_transport_profile() -> None:
    assert run.public_result(receipt(23), {"example": "1"})["runs"][0]["tests"] == 23
    with pytest.raises(ValueError, match="test run"):
        run.public_result(receipt(40), {"example": "1"})


def test_transport_coordinator_preserves_install_and_runner_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = run.TRANSPORT_PROFILE
    source = tmp_path / "source"
    manifest = {}
    for name in run.input_files(profile.candidate_files):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = b"# synthetic\n"
        if name == "dependency-wheels.json":
            data = json.dumps({"platforms": {"macos-arm64": [
                {"name": "example", "version": "1"},
            ]}}).encode()
        path.write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    (source / "source-manifest.json").write_text(json.dumps(manifest))
    selected = replace(profile, source=source)
    output = tmp_path / "output"
    commands = []

    def fake_command(
        command: list[str], root: Path, env: dict[str, str], label: str,
        *, timeout: int, max_log_bytes: int,
    ) -> None:
        commands.append(command)
        assert root == output and 0 < timeout <= 120 and max_log_bytes == 1024 * 1024
        assert set(env) == {
            "PATH", "LANG", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "UV_CACHE_DIR",
        }
        if label == "runner":
            value = receipt(profile.expected_tests)
            value["runner_sha256"] = manifest["payload/run_bundle.py.txt"]
            value["candidate_files_sha256"] = {
                name: manifest[f"payload/{name}.txt"] for name in profile.candidate_files
            }
            value["candidate_files_sha256"]["requirements.txt"] = manifest["requirements.txt"]
            (output / "tests").mkdir()
            (output / "tests/receipt.json").write_text(json.dumps(value))

    monkeypatch.setattr(run, "run_command", fake_command)
    monkeypatch.setattr(shutil, "which", lambda _: "/synthetic/uv")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sys, "argv", ["proof", "--output", str(output)])
    assert run.run_proof(selected) == 0
    value = json.loads((output / "release/verification.json").read_text())
    assert value["passed"] is True
    assert value["schema"] == f"open-brain-nw0-{profile.name}-proof-v1"
    assert len(commands) == 2
    assert commands[0][commands[0].index("-r") + 1] == str(
        output / "runtime/candidate/requirements.txt"
    )
