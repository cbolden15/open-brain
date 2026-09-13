from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools.nw0_unprivileged_transport_probe import run


def source_tree(root: Path) -> Path:
    source = root / "source"
    manifest = {}
    for name in run.INPUT_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic proof input\n")
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (source / "source-manifest.json").write_text(json.dumps(manifest))
    return source


def test_materialization_binds_exact_bytes_and_patch(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    before, _, manifest_sha256 = run.load_inputs(source)
    output = tmp_path / "output"
    bindings, actual_manifest = run.materialize(output, source)
    assert bindings == before
    assert actual_manifest == manifest_sha256
    assert (output / "runtime/pycares.patch").read_bytes() == (
        source / "patches/pycares-5.0.1-terminal-shutdown.patch"
    ).read_bytes()
    for name in run.CANDIDATE_FILES:
        assert (output / "runtime/candidate" / name).read_bytes() == (
            source / "payload" / f"{name}.txt"
        ).read_bytes()


def test_tampered_or_linked_input_rejects_before_materialization(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    (source / run.INPUT_FILES[0]).write_text("changed")
    with pytest.raises(ValueError, match="binding"):
        run.materialize(tmp_path / "tampered", source)
    source = source_tree(tmp_path / "linked")
    (source / "extra-link").hardlink_to(source / run.INPUT_FILES[0])
    with pytest.raises(ValueError, match="binding"):
        run.materialize(tmp_path / "linked-output", source)


def test_dependency_manifest_closes_both_native_targets() -> None:
    path = run.SOURCE / "dependency-wheels.json"
    for target in ("macos-arm64", "linux-x86_64"):
        manifest, rows = run.wheel_inventory(path, target)
        assert {row["name"] for row in rows} == set(run.EXPECTED_VERSIONS)
        assert manifest["pycares_source"]["module_sha256_before_patch"] == (
            "513c4097c82d7caca59e9854d2b5740f939131bbdb210e6bd26b8730a14c3d3d"
        )
        pycares = next(row for row in rows if row["name"] == "pycares")
        assert pycares["native_loader"]["embedded_c_ares"] == {
            "linkage": "static",
            "version": "1.34.6",
        }
        assert pycares["native_loader"]["native_members"]
        assert {row["name"]: row["license_expression"] for row in rows} == {
            "aiodns": "MIT",
            "cffi": "MIT",
            "cryptography": "Apache-2.0 OR BSD-3-Clause",
            "pycares": "MIT",
            "pycparser": "BSD-3-Clause",
        }


def passing_receipt(bindings: dict[str, str]) -> dict[str, Any]:
    candidate = {name: bindings[f"payload/{name}.txt"] for name in run.CANDIDATE_FILES}
    candidate["requirements.txt"] = bindings["requirements.txt"]
    return {
        "schema": "open-brain-e43-unprivileged-runner-v1",
        "passed": True,
        "provider_calls": 0,
        "dependency_versions": run.EXPECTED_VERSIONS,
        "candidate_files_sha256": candidate,
        "test_runs": [
            {
                "label": label,
                "tests": 13,
                "returncode": 0,
                "reaped": True,
                "group_terminated": True,
                "elapsed_seconds": 1.0,
            }
            for label in ("normal", "optimized")
        ],
        "runtime_run": {"returncode": 0, "reaped": True, "group_terminated": True},
        "runtime": {"passed": True, "provider_calls": 0},
    }


def test_public_receipt_requires_complete_test_and_runtime_ownership() -> None:
    bindings = json.loads((run.SOURCE / "source-manifest.json").read_text())
    receipt = passing_receipt(bindings)
    projected = run.public_receipt(receipt, bindings)
    assert projected["runtime"]["provider_calls"] == 0
    receipt["test_runs"][0]["group_terminated"] = False
    with pytest.raises(ValueError, match="test run"):
        run.public_receipt(receipt, bindings)
    receipt = passing_receipt(bindings)
    receipt["runtime"]["provider_calls"] = 1
    with pytest.raises(ValueError, match="runtime"):
        run.public_receipt(receipt, bindings)
