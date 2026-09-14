from __future__ import annotations

import hashlib
import io
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
    write_base_archive,
    write_release_manifest,
)
from tools.open_brain_dev.graphify_native import write_graphify_archive

ROOT = Path(__file__).parents[2]


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _resource(role: str, platform: str, marker: str = "a") -> ReleaseArtifact:
    component = "-graphify" if role == "graphify" else ""
    destination = "libexec/open-brain-graphify" if role == "graphify" else "bin/open-brain"
    return ReleaseArtifact(
        version="0.1.0",
        platform_tag=platform,
        sha256=marker * 64,
        filename=f"open-brain{component}-0.1.0-{platform}.tar.gz",
        role=role,
        executable_sha256=("f" if role == "graphify" else "e") * 64,
        destination=destination,
    )


def _obsidian_plugin(directory: Path) -> Path:
    directory.mkdir(parents=True)
    (directory / "main.js").write_text("module.exports = {};\n", encoding="utf-8")
    (directory / "manifest.json").write_text(
        '{"id":"open-brain","isDesktopOnly":true,"version":"0.1.0"}\n',
        encoding="utf-8",
    )
    (directory / "styles.css").write_text(".open-brain {}\n", encoding="utf-8")
    return directory


def test_native_specs_keep_base_and_graphify_in_separate_executables() -> None:
    command = base_native.pyinstaller_command(ROOT, ROOT / "build/native-test")
    spec = (ROOT / "release/open-brain/open-brain.spec").read_text(encoding="utf-8")

    assert command[-1] == str(ROOT / "release/open-brain/open-brain.spec")
    assert "local_native_entrypoint.py" in spec
    assert "analysis.binaries" in spec
    assert "analysis.datas" in spec
    assert "COLLECT(" not in spec
    assert "exclude_binaries=True" not in spec
    graphify_spec = (ROOT / "release/open-brain-graphify/open-brain-graphify.spec").read_text(
        encoding="utf-8"
    )
    assert 'name="open-brain-graphify"' in graphify_spec
    assert "graphify_helper_entry.py" in graphify_spec
    assert "yaml._yaml" in graphify_spec
    for distribution in ("open-brain", "open-brain-engine", "rfc8785"):
        assert f'copy_metadata("{distribution}")' in spec
    for forbidden in (
        "open_brain.services.appliance_entrypoints",
        "open_brain_engine.portability.secure_node",
        "open_brain_engine.ledger",
        "open_brain_engine.protocol",
        "http.server",
        "docker",
        "prctl",
        "systemd",
        "cryptography",
    ):
        assert f'"{forbidden}"' in spec
    assert "open_brain_engine.engine.markdown_import" in base_native._REQUIRED_MODULES
    assert "open_brain_engine.engine.markdown_import_fs" in base_native._REQUIRED_MODULES
    assert "open_brain_engine.engine.local_schema" in base_native._REQUIRED_MODULES
    assert "open_brain_engine.engine.local_schema_catalog" in base_native._REQUIRED_MODULES
    assert "open_brain_engine.storage.migrations" in base_native._REQUIRED_MODULES
    for module in (
        "open_brain.services.graph_projection_store",
        "open_brain.services.graphify_projection",
        "open_brain.services.local_mcp",
        "open_brain.services.local_operations",
        "open_brain.services.local_runtime_session",
        "open_brain.services.mcp_protocol",
    ):
        assert module in base_native._REQUIRED_MODULES
    for module in ("open_brain.integrations", "open_brain.services.mcp_stdio"):
        assert module in base_native._FORBIDDEN_MODULE_PREFIXES
        assert f'"{module}"' in spec


@pytest.mark.parametrize(
    ("system_name", "machine_name", "expected"),
    (
        ("Darwin", "arm64", "macos-arm64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "amd64", "linux-x86_64"),
    ),
)
def test_native_platform_contract(system_name: str, machine_name: str, expected: str) -> None:
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


def test_release_archives_are_reproducible_and_manifest_the_exact_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _executable(tmp_path / "artifact/open-brain")
    helper = _executable(tmp_path / "artifact/open-brain-graphify")
    plugin = _obsidian_plugin(tmp_path / "artifact/obsidian-plugin")
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    for name in ("LICENSE", "LICENSE-MIT", "NOTICE", "OPEN-BRAIN-NOTICE"):
        (licenses / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(
        base_native,
        "audit_base_artifact",
        lambda _path: BaseNativeAudit("macos-arm64", 1, "a" * 64, "b" * 64, "adhoc"),
    )

    first = write_base_archive(
        artifact,
        tmp_path / "first",
        version="0.1.0",
        obsidian_plugin_directory=plugin,
    )
    second = write_base_archive(
        artifact,
        tmp_path / "second",
        version="0.1.0",
        obsidian_plugin_directory=plugin,
    )
    first_helper = write_graphify_archive(
        helper,
        licenses,
        tmp_path / "first/open-brain-graphify-0.1.0-macos-arm64.tar.gz",
    )
    second_helper = write_graphify_archive(
        helper,
        licenses,
        tmp_path / "second/open-brain-graphify-0.1.0-macos-arm64.tar.gz",
    )
    first_manifest = write_release_manifest(
        (
            base_native._release_artifact(first, executable=artifact),
            base_native._release_artifact(first_helper, executable=helper),
        ),
        tmp_path / "first/open-brain-component-manifest-v1.txt",
    )
    second_manifest = write_release_manifest(
        (
            base_native._release_artifact(second, executable=artifact),
            base_native._release_artifact(second_helper, executable=helper),
        ),
        tmp_path / "second/open-brain-component-manifest-v1.txt",
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_helper.read_bytes() == second_helper.read_bytes()
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "open-brain",
            "obsidian-plugin/main.js",
            "obsidian-plugin/manifest.json",
            "obsidian-plugin/styles.css",
        ]
        assert members[0].mode & 0o100
        assert all(member.mode == 0o644 for member in members[1:])
    with tarfile.open(first_helper, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "licenses/graphify/LICENSE",
            "licenses/graphify/LICENSE-MIT",
            "licenses/graphify/NOTICE",
            "licenses/graphify/OPEN-BRAIN-NOTICE",
            "open-brain",
        ]
    manifest = read_release_manifest(first_manifest)
    assert manifest.version == "0.1.0"
    assert [item.role for item in manifest.artifacts] == ["base", "graphify"]
    assert manifest.artifacts[0].sha256 == hashlib.sha256(first.read_bytes()).hexdigest()
    assert manifest.artifacts[1].sha256 == hashlib.sha256(first_helper.read_bytes()).hexdigest()


def test_two_platform_manifest_is_canonical_and_drives_the_homebrew_formula(
    tmp_path: Path,
) -> None:
    artifacts = (
        _resource("graphify", "macos-arm64", "d"),
        _resource("base", "macos-arm64", "b"),
        _resource("graphify", "linux-x86_64", "c"),
        _resource("base", "linux-x86_64", "a"),
    )
    manifest_path = write_release_manifest(artifacts, tmp_path / "manifest.txt")
    formula_path = render_homebrew_formula(manifest_path, tmp_path / "open-brain.rb")
    manifest = read_release_manifest(manifest_path)
    formula = formula_path.read_text(encoding="utf-8")

    assert [(artifact.platform_tag, artifact.role) for artifact in manifest.artifacts] == [
        ("linux-x86_64", "base"),
        ("linux-x86_64", "graphify"),
        ("macos-arm64", "base"),
        ("macos-arm64", "graphify"),
    ]
    assert [artifact.platform_tag for artifact in manifest.artifacts] == [
        "linux-x86_64",
        "linux-x86_64",
        "macos-arm64",
        "macos-arm64",
    ]
    assert 'version "0.1.0"' in formula
    assert "on_linux do" in formula
    assert "on_macos do" in formula
    assert "depends_on arch: :x86_64" in formula
    assert "depends_on arch: :arm64" in formula
    assert 'sha256 "a' in formula
    assert 'sha256 "b' in formula
    assert 'resource "graphify" do' in formula
    assert "https://github.com/cbolden15/open-brain/releases/download/v0.1.0" in formula
    assert "class OpenBrain < Formula" in formula
    assert "keg_only" not in formula
    assert 'bin.install "open-brain"' in formula
    assert '(share/"open-brain/obsidian-plugin").install' in formula
    assert 'assert_path_exists share/"open-brain/obsidian-plugin/main.js"' in formula
    assert 'libexec.install "open-brain" => "open-brain-graphify"' in formula
    assert "open-brain-graphify --capabilities" in formula


def test_manifest_combination_rejects_cross_version_archives(tmp_path: Path) -> None:
    first = tmp_path / "open-brain-0.1.0-linux-x86_64.tar.gz"
    second = tmp_path / "open-brain-graphify-0.2.0-linux-x86_64.tar.gz"
    for path in (first, second):
        with tarfile.open(path, "w:gz") as bundle:
            payload = b"synthetic executable"
            member = tarfile.TarInfo("open-brain")
            member.size = len(payload)
            member.mode = 0o755
            bundle.addfile(member, io.BytesIO(payload))

    with pytest.raises(BaseNativeError, match="versions do not match"):
        combine_release_manifest((first, second), tmp_path / "manifest.txt")


def test_ci_has_only_two_native_product_runners() -> None:
    workflows = ROOT / ".github/workflows"
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    runs_on = re.findall(r"^\s+runs-on: (\S+)$", workflow, flags=re.MULTILINE)

    assert {path.name for path in workflows.iterdir() if path.is_file()} == {"ci.yml"}
    assert runs_on == ["ubuntu-latest", "macos-latest"]
    assert workflow.count("make contributor-check") == 2
    assert "make verify" not in workflow
    assert "make homebrew-smoke" not in workflow
    assert "goal/open-brain-five-minute-install" in workflow
    assert "docker" not in workflow.lower()
    assert "attestation" not in workflow.lower()
    assert "notar" not in workflow.lower()
    assert "install.sh" not in workflow


def test_native_build_group_installs_the_base_application() -> None:
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "open-brain==0.1.0" in workspace["dependency-groups"]["dev"]
    assert all(
        "secure-node" not in dependency for dependency in workspace["dependency-groups"]["dev"]
    )
    assert "open-brain==0.1.0" in workspace["dependency-groups"]["native-build"]
    assert workspace["tool"]["uv"]["sources"]["open-brain"] == {"workspace": True}
    assert makefile.count("--no-dev --group native-build") == 4


def test_local_homebrew_smoke_uses_a_temporary_tap() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "bash tools/homebrew-smoke.sh" in makefile
    target = makefile.split("contributor-check:\n", 1)[1].split("\n\n", 1)[0]
    assert target.splitlines() == [
        "\t$(MAKE) verify",
        "\t$(MAKE) native-integration-smoke",
        "\t$(MAKE) desktop-native",
    ]
    native_smoke = makefile.split("native-integration-smoke:", 1)[1].split("\n\n", 1)[0]
    assert native_smoke == " homebrew-smoke"
    verify = makefile.split("verify:", 1)[1].split("\n\n", 1)[0]
    assert "plugin-test" in verify
    assert "desktop-test" in verify
    assert "contributor-check" in makefile.splitlines()[0]


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
