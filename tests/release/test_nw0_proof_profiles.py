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


def test_fixed_profiles_preserve_transport_and_select_authority() -> None:
    assert run.TRANSPORT_PROFILE.source == run.SOURCE
    assert run.TRANSPORT_PROFILE.candidate_files == run.CANDIDATE_FILES
    assert run.TRANSPORT_PROFILE.expected_tests == 23
    assert run.TRANSPORT_PROFILE.base_sources == ()
    assert run.AUTHORITY_PROFILE.expected_tests == 28
    assert "semantic-dataset.json" in run.AUTHORITY_PROFILE.candidate_files
    assert {row[0] for row in run.AUTHORITY_PROFILE.base_sources} == {"engine", "app"}


def test_authority_inventory_materializes_data_without_changing_transport(tmp_path: Path) -> None:
    source = tmp_path / "source"
    names = run.AUTHORITY_PROFILE.candidate_files
    manifest = {}
    for name in run.input_files(names):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = b'{}\n' if "json" in name else b"# synthetic\n"
        path.write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    (source / "source-manifest.json").write_text(json.dumps(manifest))
    bindings, _ = run.materialize(source, tmp_path / "output", names)
    assert bindings == manifest
    assert (tmp_path / "output/runtime/candidate/semantic-dataset.json").read_bytes() == b'{}\n'
    with pytest.raises(ValueError, match="inventory"):
        run.load_inputs(source)


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


def test_receipt_counts_are_profile_specific() -> None:
    assert run.public_result(receipt(28), {"example": "1"}, 28)["runs"][0]["tests"] == 28
    with pytest.raises(ValueError, match="test run"):
        run.public_result(receipt(23), {"example": "1"}, 28)
    with pytest.raises(ValueError, match="test run"):
        run.public_result(receipt(28), {"example": "1"})


def test_authority_command_selects_only_fixed_base_sources() -> None:
    arguments = run.base_source_arguments(run.AUTHORITY_PROFILE)
    assert arguments == [
        "--engine-source", str(run.REPO / "packages/engine/src"),
        "--app-source", str(run.REPO / "packages/app/src"),
    ]
    assert run.base_source_arguments(run.TRANSPORT_PROFILE) == []


def test_base_source_projection_requires_exact_profile_pins() -> None:
    expected = {name: pin for name, _, pin in run.AUTHORITY_PROFILE.base_sources}
    raw = {"base_source_sha256": expected, "base_source_roots": {"engine": "private-path"}}
    assert run.base_source_result(raw, run.AUTHORITY_PROFILE) == {"base_source_sha256": expected}
    for invalid in ({}, {"engine": "a" * 64}, {**expected, "extra": "a" * 64}):
        with pytest.raises(ValueError, match="base source"):
            run.base_source_result({"base_source_sha256": invalid}, run.AUTHORITY_PROFILE)
    assert run.base_source_result({}, run.TRANSPORT_PROFILE) == {}


@pytest.mark.parametrize("profile", [run.TRANSPORT_PROFILE, run.AUTHORITY_PROFILE])
@pytest.mark.parametrize("wrong_base", [False, True])
def test_coordinator_uses_profile_and_rejects_wrong_base_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    profile: run.ProofProfile, wrong_base: bool,
) -> None:
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
            value["base_source_sha256"] = {
                name: pin for name, _, pin in profile.base_sources
            }
            if wrong_base:
                value["base_source_sha256"] = {"engine": "0" * 64}
            value["base_source_roots"] = {"engine": "never-publish-private-path"}
            (output / "tests").mkdir()
            (output / "tests/receipt.json").write_text(json.dumps(value))

    monkeypatch.setattr(run, "run_command", fake_command)
    monkeypatch.setattr(shutil, "which", lambda _: "/synthetic/uv")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sys, "argv", ["proof", "--output", str(output)])
    failed = bool(wrong_base and profile.base_sources)
    assert run.run_proof(selected) == int(failed)
    value = json.loads((output / "release/verification.json").read_text())
    assert value["passed"] is (not failed)
    assert value["schema"] == f"open-brain-nw0-{profile.name}-proof-v1"
    assert "never-publish-private-path" not in json.dumps(value)
    assert len(commands) == 2
    assert commands[0][commands[0].index("-r") + 1] == str(
        output / "runtime/candidate/requirements.txt"
    )
    for name, path, _ in profile.base_sources:
        assert commands[1][commands[1].index(f"--{name}-source") + 1] == str(path)
