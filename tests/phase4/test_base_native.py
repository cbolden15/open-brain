from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import tools.open_brain_dev.base_native as base_native
from tools.open_brain_dev.base_native import (
    BaseNativeAudit,
    BaseNativeError,
    audit_base_artifact,
    native_platform_tag,
    pyinstaller_command,
    write_release_assets,
)

ROOT = Path(__file__).parents[2]


def _executable(path: Path, source: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _release_fixture(root: Path, *, damage: str = "none") -> Path:
    artifact = root / "artifact/open-brain"
    _executable(
        artifact / "open-brain",
        "#!/bin/sh\n"
        "case \"${1:-}\" in\n"
        "  __open-brain-self-check) exit 0 ;;\n"
        "  --version) printf '%s\\n' 'open-brain 0.1.0'; exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
    )
    release = root / "release"
    release.mkdir(parents=True)
    archive = release / "open-brain-0.1.0-macos-arm64.tar.gz"
    base_native._write_reproducible_archive(artifact, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = release / "open-brain-release-manifest-v1.txt"
    manifest.write_text(
        "open-brain-release-manifest-v1\n"
        "version 0.1.0\n"
        f"artifact macos-arm64 {digest} {archive.name}\n",
        encoding="ascii",
    )
    if damage == "checksum":
        with archive.open("ab") as stream:
            stream.write(b"tampered")
    elif damage == "manifest":
        with manifest.open("a", encoding="ascii") as stream:
            stream.write("unexpected record\n")
    return release


def _fake_download_tools(root: Path) -> Path:
    tools = root / "bin"
    _executable(
        tools / "uname",
        "#!/bin/sh\n"
        "case \"${1:-}\" in\n"
        "  -s) printf '%s\\n' Darwin ;;\n"
        "  -m) printf '%s\\n' arm64 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    _executable(
        tools / "curl",
        "#!/bin/sh\n"
        "set -eu\n"
        "url=\n"
        "output=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    https://*) url=$1 ;;\n"
        "    -o) shift; output=$1 ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        "[ -n \"$url\" ] && [ -n \"$output\" ]\n"
        "cp \"$FAKE_RELEASE_SOURCE/${url##*/}\" \"$output\"\n",
    )
    return tools


def test_base_native_spec_is_separate_and_excludes_secure_node() -> None:
    command = pyinstaller_command(ROOT, ROOT / "build/base-native-test")
    spec = (ROOT / "release/open-brain/open-brain.spec").read_text(encoding="utf-8")

    assert command[-1] == str(ROOT / "release/open-brain/open-brain.spec")
    assert "local_native_entrypoint.py" in spec
    assert "collect_submodules" not in spec
    for distribution in ("open-brain", "open-brain-engine", "rfc8785"):
        assert f'copy_metadata("{distribution}")' in spec
    assert '"open_brain.services.appliance_entrypoints"' in spec
    assert '"open_brain_engine.portability.secure_node"' in spec
    assert '"cryptography"' in spec
    assert "release/native/open-brain.spec" not in spec


@pytest.mark.parametrize(
    ("system_name", "machine_name", "expected"),
    (
        ("Darwin", "arm64", "macos-arm64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "amd64", "linux-x86_64"),
    ),
)
def test_base_native_platform_contract(
    system_name: str, machine_name: str, expected: str
) -> None:
    assert native_platform_tag(system_name=system_name, machine_name=machine_name) == expected


def test_base_native_platform_rejects_unsupported_hosts() -> None:
    with pytest.raises(BaseNativeError, match="unsupported native build platform"):
        native_platform_tag(system_name="Windows", machine_name="AMD64")


def test_base_native_audit_rejects_secure_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "open-brain"
    _executable(artifact / "open-brain")
    modules = set(base_native._REQUIRED_MODULES)
    monkeypatch.setattr(base_native, "archive_modules", lambda _path: tuple(sorted(modules)))
    monkeypatch.setattr(base_native, "native_platform_tag", lambda: "macos-arm64")

    assert audit_base_artifact(artifact).platform_tag == "macos-arm64"

    modules.add("open_brain.services.appliance_daemon")
    with pytest.raises(BaseNativeError, match="crosses the product boundary"):
        audit_base_artifact(artifact)


def test_release_archive_and_manifest_are_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "artifact/open-brain"
    _executable(artifact / "open-brain")
    (artifact / "_internal").mkdir()
    (artifact / "_internal/data.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        base_native,
        "audit_base_artifact",
        lambda _path: BaseNativeAudit("macos-arm64", 1, "a" * 64, "b" * 64),
    )

    first, first_manifest = write_release_assets(artifact, tmp_path / "first")
    second, second_manifest = write_release_assets(artifact, tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() in first_manifest.read_text(
        encoding="ascii"
    )


@pytest.mark.parametrize("damage", ("none", "checksum", "manifest"))
def test_installer_verifies_manifest_and_checksum_before_activation(
    tmp_path: Path, damage: str
) -> None:
    release = _release_fixture(tmp_path, damage=damage)
    fake_tools = _fake_download_tools(tmp_path)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    environment = {
        "FAKE_RELEASE_SOURCE": str(release),
        "HOME": str(home),
        "OPEN_BRAIN_ACCEPTANCE_VERSION": "0.1.0",
        "OPEN_BRAIN_RELEASE_BASE_URL": "https://example.invalid/release",
        "PATH": f"{fake_tools}:/usr/bin:/bin",
    }

    completed = subprocess.run(
        ("/bin/sh", str(ROOT / "release/open-brain/install.sh")),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    launcher = home / ".local/bin/open-brain"
    if damage != "none":
        assert completed.returncode == 1
        assert not launcher.exists()
        assert not (home / ".local/lib/open-brain").exists()
    else:
        assert completed.returncode == 0
        assert completed.stdout == '{"command":"open-brain","status":"installed"}\n'
        assert launcher.is_symlink()
        assert os.readlink(launcher) == "../lib/open-brain/open-brain"
        assert not (home / "Library/Application Support/open-brain").exists()
