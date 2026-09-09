from __future__ import annotations

import hashlib
import re
import tarfile
import tomllib
from pathlib import Path

import pytest

import tools.open_brain_dev.base_native as base_native
from tools.open_brain_dev.base_native import (
    BaseNativeAudit,
    BaseNativeError,
    ReleaseArtifact,
    audit_base_artifact,
    combine_release_manifest,
    native_platform_tag,
    read_release_manifest,
    render_homebrew_formula,
    write_release_assets,
    write_release_manifest,
)

ROOT = Path(__file__).parents[2]


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_native_spec_builds_one_default_product_executable() -> None:
    command = base_native.pyinstaller_command(ROOT, ROOT / "build/native-test")
    spec = (ROOT / "release/open-brain/open-brain.spec").read_text(encoding="utf-8")

    assert command[-1] == str(ROOT / "release/open-brain/open-brain.spec")
    assert "local_native_entrypoint.py" in spec
    assert "analysis.binaries" in spec
    assert "analysis.datas" in spec
    assert "COLLECT(" not in spec
    assert "exclude_binaries=True" not in spec
    for distribution in ("open-brain", "open-brain-engine", "rfc8785"):
        assert f'copy_metadata("{distribution}")' in spec
    for forbidden in (
        "open_brain.services.appliance_entrypoints",
        "open_brain_engine.portability.secure_node",
        "cryptography",
    ):
        assert f'"{forbidden}"' in spec


@pytest.mark.parametrize(
    ("system_name", "machine_name", "expected"),
    (
        ("Darwin", "arm64", "macos-arm64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "amd64", "linux-x86_64"),
    ),
)
def test_native_platform_contract(
    system_name: str, machine_name: str, expected: str
) -> None:
    assert native_platform_tag(system_name=system_name, machine_name=machine_name) == expected


def test_native_platform_rejects_unsupported_hosts() -> None:
    with pytest.raises(BaseNativeError, match="unsupported native build platform"):
        native_platform_tag(system_name="Windows", machine_name="AMD64")


def test_native_audit_rejects_secure_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _executable(tmp_path / "open-brain")
    modules = set(base_native._REQUIRED_MODULES)
    monkeypatch.setattr(base_native, "archive_modules", lambda _path: tuple(sorted(modules)))
    monkeypatch.setattr(base_native, "native_platform_tag", lambda: "macos-arm64")
    monkeypatch.setattr(base_native, "_validate_native_executable", lambda *_: "adhoc")

    assert audit_base_artifact(artifact).signature == "adhoc"

    modules.add("open_brain.services.appliance_daemon")
    with pytest.raises(BaseNativeError, match="crosses the product boundary"):
        audit_base_artifact(artifact)


def test_release_archive_contains_only_the_executable_and_has_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _executable(tmp_path / "artifact/open-brain")
    monkeypatch.setattr(
        base_native,
        "audit_base_artifact",
        lambda _path: BaseNativeAudit("macos-arm64", 1, "a" * 64, "b" * 64, "adhoc"),
    )

    first, first_manifest = write_release_assets(
        artifact, tmp_path / "first", version="0.1.0"
    )
    second, second_manifest = write_release_assets(
        artifact, tmp_path / "second", version="0.1.0"
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == ["open-brain"]
        assert members[0].mode & 0o100
    manifest = read_release_manifest(first_manifest)
    assert manifest.version == "0.1.0"
    assert manifest.artifacts[0].sha256 == hashlib.sha256(first.read_bytes()).hexdigest()


def test_two_platform_manifest_is_canonical_and_drives_the_homebrew_formula(
    tmp_path: Path,
) -> None:
    artifacts = (
        ReleaseArtifact(
            "0.1.0",
            "macos-arm64",
            "b" * 64,
            "open-brain-0.1.0-macos-arm64.tar.gz",
        ),
        ReleaseArtifact(
            "0.1.0",
            "linux-x86_64",
            "a" * 64,
            "open-brain-0.1.0-linux-x86_64.tar.gz",
        ),
    )
    manifest_path = write_release_manifest(artifacts, tmp_path / "manifest.txt")
    formula_path = render_homebrew_formula(manifest_path, tmp_path / "open-brain.rb")
    manifest = read_release_manifest(manifest_path)
    formula = formula_path.read_text(encoding="utf-8")

    assert [artifact.platform_tag for artifact in manifest.artifacts] == [
        "linux-x86_64",
        "macos-arm64",
    ]
    assert 'version "0.1.0"' in formula
    assert "on_linux do" in formula
    assert "on_macos do" in formula
    assert "depends_on arch: :x86_64" in formula
    assert "depends_on arch: :arm64" in formula
    assert 'sha256 "a' in formula
    assert 'sha256 "b' in formula
    assert "releases/download/v0.1.0" in formula
    assert 'bin.install "open-brain"' in formula


def test_manifest_combination_rejects_cross_version_archives(tmp_path: Path) -> None:
    first = tmp_path / "open-brain-0.1.0-linux-x86_64.tar.gz"
    second = tmp_path / "open-brain-0.2.0-macos-arm64.tar.gz"
    first.write_bytes(b"linux")
    second.write_bytes(b"macos")

    with pytest.raises(BaseNativeError, match="versions do not match"):
        combine_release_manifest((first, second), tmp_path / "manifest.txt")


def test_ci_has_only_two_native_product_runners() -> None:
    workflows = ROOT / ".github/workflows"
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    runs_on = re.findall(r"^\s+runs-on: (\S+)$", workflow, flags=re.MULTILINE)

    assert {path.name for path in workflows.iterdir() if path.is_file()} == {"ci.yml"}
    assert runs_on == ["ubuntu-latest", "macos-latest"]
    assert workflow.count("make verify") == 2
    assert workflow.count("make homebrew-smoke") == 2
    for job in ("linux-x86-64", "macos-arm64"):
        job_body = workflow.split(f"  {job}:", maxsplit=1)[1]
        if job == "linux-x86-64":
            job_body = job_body.split("  macos-arm64:", maxsplit=1)[0]
        assert job_body.index("make verify") < job_body.index("make homebrew-smoke")
    assert "docker" not in workflow.lower()
    assert "attestation" not in workflow.lower()
    assert "notar" not in workflow.lower()
    assert "install.sh" not in workflow


def test_native_build_group_installs_the_base_application() -> None:
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "open-brain[secure-node]==0.1.0" in workspace["dependency-groups"]["dev"]
    assert "open-brain==0.1.0" in workspace["dependency-groups"]["native-build"]
    assert workspace["tool"]["uv"]["sources"]["open-brain"] == {"workspace": True}
    assert makefile.count("--no-dev --group native-build") == 4


def test_local_homebrew_smoke_uses_a_temporary_tap() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "HOMEBREW_SMOKE_TAP = open-brain-local/smoke" in makefile
    assert 'brew tap "$$smoke_tap"' in makefile
    assert 'brew install "$$smoke_tap/open-brain"' in makefile
    assert 'brew untap --force "$$smoke_tap"' in makefile
    assert "brew install --formula" not in makefile


def test_removed_release_stack_does_not_regrow() -> None:
    removed_files = (
        ROOT / "release/open-brain/install.sh",
        ROOT / "release/v0-artifact-policy.json",
        ROOT / "tools/open_brain_dev/artifact_policy.py",
    )
    removed_source_trees = (ROOT / "release/native", ROOT / "tools/phase4")

    assert not any(path.exists() for path in removed_files)
    assert not any(
        path.is_file() and "__pycache__" not in path.parts
        for tree in removed_source_trees
        for path in tree.rglob("*")
    )
