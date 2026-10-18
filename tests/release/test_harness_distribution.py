"""Third optional resource keeps the base archive closure and old manifests intact."""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import pytest

from tests.security.test_native_artifact_audit import executable, module, package, pyz
from tools.open_brain_dev import base_native
from tools.open_brain_dev.harness_distribution import (
    ASSETS,
    EXECUTABLE,
    INVENTORY,
    write_harness_archive,
)


def resource(role: str, platform: str) -> base_native.ReleaseArtifact:
    return base_native.ReleaseArtifact(
        version="0.1.0",
        platform_tag=platform,
        filename="open-brain-"
        + ("" if role == "base" else role + "-")
        + f"0.1.0-{platform}.tar.gz",
        role=role,
        sha256="a" * 64,
        executable_sha256="b" * 64,
        destination={
            "base": "libexec/open-brain/open-brain",
            "graphify": "libexec/open-brain-graphify",
            "harness": "libexec/open-brain-harness/open-brain-harness",
        }.get(role, "bin/other"),
    )


def test_third_resource_is_canonical_and_formula_installs_persistent_paths(tmp_path: Path) -> None:
    artifacts = [
        resource(role, platform)
        for platform in ("macos-arm64", "linux-x86_64")
        for role in ("harness", "graphify", "base")
    ]
    manifest = base_native.write_release_manifest(artifacts, tmp_path / "manifest")
    parsed = base_native.read_release_manifest(manifest)
    assert [item.role for item in parsed.artifacts] == ["base", "graphify", "harness"] * 2
    formula = base_native.render_homebrew_formula(manifest, tmp_path / "formula.rb").read_text()
    assert formula.count('resource "harness" do') == 2
    assert 'libexec.install "open-brain-harness"' in formula
    assert '(share/"open-brain/harness").install Dir["harness/*"]' in formula
    assert "open-brain-harness/open-brain-harness harness --help" in formula
    assert 'depends_on "python' not in formula
    assert 'depends_on "node' not in formula


@pytest.mark.parametrize("bad", ["mixed", "missing-base", "duplicate", "unknown"])
def test_manifest_rejects_inconsistent_resource_sets(tmp_path: Path, bad: str) -> None:
    artifacts = [
        resource(role, platform)
        for platform in ("macos-arm64", "linux-x86_64")
        for role in ("base", "graphify", "harness")
    ]
    if bad == "mixed":
        artifacts = [
            item
            for item in artifacts
            if not (item.role == "harness" and item.platform_tag == "macos-arm64")
        ]
    elif bad == "missing-base":
        artifacts = [item for item in artifacts if item.role != "base"]
    elif bad == "duplicate":
        artifacts.append(artifacts[0])
    else:
        artifacts.append(resource("other", "linux-x86_64"))
    with pytest.raises(base_native.BaseNativeError):
        base_native.write_release_manifest(artifacts, tmp_path / "manifest")


def test_harness_archive_reproducible_complete_and_digest_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    internal = runtime / "_internal"
    internal.mkdir(parents=True)
    binary = executable(package([("PYZ.pyz", b"z", pyz(module()))]), "linux-x86_64")
    (runtime / "open-brain-harness").write_bytes(binary)
    (runtime / "open-brain-harness").chmod(0o755)
    (internal / "data.txt").write_text("synthetic data")
    (internal / "alias").symlink_to("data.txt")
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ASSETS:
        (assets / Path(name).name).write_text("// synthetic adapter")
    monkeypatch.setattr(
        base_native,
        "audit_base_artifact",
        lambda _path: base_native.BaseNativeAudit(
            "linux-x86_64", 1, "0" * 64, hashlib.sha256(binary).hexdigest(), "not-applicable"
        ),
    )
    first = write_harness_archive(runtime, assets, tmp_path / "first", version="0.1.0")
    second = write_harness_archive(runtime, assets, tmp_path / "second", version="0.1.0")
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        names = {member.name for member in archive}
    assert (
        names
        == {
            EXECUTABLE,
            INVENTORY,
            "harness",
            "open-brain-harness",
            "open-brain-harness/_internal",
            "open-brain-harness/_internal/data.txt",
            "open-brain-harness/_internal/alias",
        }
        | ASSETS
    )
    artifact = base_native._release_artifact(first)
    assert artifact.role == "harness"
    assert artifact.executable_sha256 == hashlib.sha256(binary).hexdigest()
    assert artifact.sha256 == hashlib.sha256(first.read_bytes()).hexdigest()


def test_harness_archive_rejects_version_path_injection(tmp_path: Path) -> None:
    with pytest.raises(base_native.BaseNativeError, match="version"):
        write_harness_archive(tmp_path, tmp_path, tmp_path, version="../../outside")
