from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools.nw0_api_probe.run import (
    CANDIDATE_FILES,
    INPUT_FILES,
    implementation_sha256,
    materialize,
    public_result,
    verify_inputs,
)


def source_tree(root: Path) -> Path:
    source = root / "source"
    manifest = {}
    for name in INPUT_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic proof input\n")
        manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (source / "source-manifest.json").write_text(json.dumps(manifest))
    return source


def test_materialization_binds_exact_bytes_and_preserves_source(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    before = verify_inputs(source)
    output = tmp_path / "output"
    bindings, manifest_sha256 = materialize(source, output)
    assert bindings == before
    manifest_bytes = (source / "source-manifest.json").read_bytes()
    assert manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
    for name in CANDIDATE_FILES:
        assert (output / "runtime/candidate" / name).read_bytes() == (
            source / "payload" / (name + ".txt")
        ).read_bytes()
    assert verify_inputs(source) == before
    with pytest.raises(FileExistsError):
        materialize(source, output)


def test_tampered_input_rejects_before_materialization(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    path = source / INPUT_FILES[0]
    path.write_text("changed")
    with pytest.raises(ValueError, match="binding"):
        materialize(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_implementation_binding_has_fixed_path_free_fields() -> None:
    result = implementation_sha256("a" * 64)
    assert set(result) == {"coordinator", "source_manifest", "command_runner"}
    assert result["source_manifest"] == "a" * 64
    assert all(len(value) == 64 and int(value, 16) >= 0 for value in result.values())


def test_materialization_does_not_reread_verified_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools.nw0_api_probe import run

    source = source_tree(tmp_path)
    original = run.load_inputs

    def changed_after_read(
        path: Path, candidate_files: tuple[str, ...] = CANDIDATE_FILES,
        extra_files: tuple[str, ...] = (),
    ) -> tuple[dict[str, str], dict[str, bytes], str]:
        value = original(path, candidate_files, extra_files)
        (path / "requirements.txt").write_text("changed")
        (path / "dependency-wheels.json").write_text("changed")
        return value

    monkeypatch.setattr(run, "load_inputs", changed_after_read)
    output = tmp_path / "output"
    bindings, _ = materialize(source, output)
    for relative, name in (
        ("runtime/candidate/requirements.txt", "requirements.txt"),
        ("runtime/dependency-wheels.json", "dependency-wheels.json"),
    ):
        assert hashlib.sha256((output / relative).read_bytes()).hexdigest() == bindings[name]


def test_linked_input_rejects_before_materialization(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    target = source / "requirements.txt"
    link = source / "extra-hardlink"
    link.hardlink_to(target)
    with pytest.raises(ValueError, match="binding"):
        materialize(source, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def passing_receipt() -> dict[str, Any]:
    return {
        "ok": True, "dependency_pins_match": True,
        "dependency_versions": {"example": "1.0"},
        "raw_error": "not for publication",
        "runs": [
            {"label": label, "tests": 23, "returncode": 0, "failure": None,
             "reaped": True, "direct_reaped": True, "group_terminated": True,
             "cleanup_known": True, "elapsed_seconds": 1.0, "command": ["private-local-path"]}
            for label in ("normal", "optimized")
        ],
    }


def test_receipt_projection_omits_raw_errors_commands_and_paths() -> None:
    value = public_result(passing_receipt(), {"example": "1.0"})
    encoded = json.dumps(value)
    assert "private-local-path" not in encoded
    assert "not for publication" not in encoded
    assert "command" not in encoded
    assert len(value["runs"]) == 2


@pytest.mark.parametrize("field,value", [
    ("tests", 22), ("reaped", False), ("returncode", 1), ("group_terminated", False),
    ("cleanup_known", False), ("elapsed_seconds", float("nan")),
    ("returncode", False),
])
def test_receipt_rejects_incomplete_run(field: str, value: Any) -> None:
    receipt = passing_receipt()
    receipt["runs"][0][field] = value
    with pytest.raises(ValueError, match="test run"):
        public_result(receipt, {"example": "1.0"})


def test_receipt_rejects_wrong_dependency_identity() -> None:
    with pytest.raises(ValueError, match="runner receipt"):
        public_result(passing_receipt(), {"example": "2.0"})
