"""Build and verify the minimal native artifact for default Open Brain."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Final

_SPEC = Path("release/open-brain/open-brain.spec")
_EXECUTABLE = "open-brain"
_MANIFEST = "open-brain-release-manifest-v1.txt"
_VERSION: Final = "0.1.0"
_PLATFORMS: Final = frozenset({"linux-x86_64", "macos-arm64"})
_REQUIRED_MODULES: Final = frozenset(
    {
        "open_brain.local_data",
        "open_brain.profile",
        "open_brain.services.local_bootstrap",
        "open_brain.services.local_entrypoints",
        "open_brain_engine.engine.local",
        "open_brain_engine.storage.sqlite",
    }
)
_FORBIDDEN_MODULE_PREFIXES: Final = (
    "argon2",
    "cryptography",
    "keyring",
    "open_brain.capture",
    "open_brain.cli",
    "open_brain.extensions",
    "open_brain.integrations",
    "open_brain.services.appliance_",
    "open_brain.services.composition",
    "open_brain.services.connectors",
    "open_brain.services.http_server",
    "open_brain.services.mcp_stdio",
    "open_brain.services.native_artifacts",
    "open_brain.services.native_entrypoint",
    "open_brain.services.phase1_",
    "open_brain.services.runtime",
    "open_brain.services.secure_node_entrypoints",
    "open_brain_connectors",
    "open_brain_engine.portability.secure_node",
    "open_brain_engine.protocol.custody",
    "open_brain_legacy",
    "sqlcipher3",
    "starlette",
    "uvicorn",
)


class BaseNativeError(RuntimeError):
    """The default native artifact failed its bounded build contract."""


@dataclass(frozen=True, slots=True)
class BaseNativeAudit:
    platform_tag: str
    module_count: int
    modules_sha256: str
    tree_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "module_count": self.module_count,
            "modules_sha256": self.modules_sha256,
            "platform": self.platform_tag,
            "tree_sha256": self.tree_sha256,
        }


def native_platform_tag(
    *, system_name: str | None = None, machine_name: str | None = None
) -> str:
    system = platform.system() if system_name is None else system_name
    machine = platform.machine() if machine_name is None else machine_name
    mapping = {
        ("Darwin", "arm64"): "macos-arm64",
        ("Linux", "x86_64"): "linux-x86_64",
        ("Linux", "amd64"): "linux-x86_64",
    }
    try:
        return mapping[(system, machine)]
    except KeyError as error:
        raise BaseNativeError("unsupported native build platform") from error


def pyinstaller_command(root: Path, output: Path) -> tuple[str, ...]:
    spec = root.resolve(strict=True) / _SPEC
    if not spec.is_file():
        raise BaseNativeError("base native spec is unavailable")
    return (
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        os.fspath(output.resolve() / "dist"),
        "--workpath",
        os.fspath(output.resolve() / "work"),
        os.fspath(spec),
    )


def archive_modules(executable: Path) -> tuple[str, ...]:
    try:
        readers = importlib.import_module("PyInstaller.archive.readers")
        reader_type = readers.CArchiveReader
        outer = reader_type(os.fspath(executable.resolve(strict=True)))
        embedded = outer.open_embedded_archive("PYZ.pyz")
        toc = embedded.toc
        if not isinstance(toc, Mapping) or not all(isinstance(name, str) for name in toc):
            raise BaseNativeError("base native module inventory is invalid")
        return tuple(sorted(toc))
    except BaseNativeError:
        raise
    except Exception as error:
        raise BaseNativeError("base native module inventory is unavailable") from error


def audit_base_artifact(artifact: Path) -> BaseNativeAudit:
    try:
        selected = artifact.resolve(strict=True)
        metadata = artifact.lstat()
        executable = selected / _EXECUTABLE
        executable_metadata = executable.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or artifact.is_symlink()
            or not stat.S_ISREG(executable_metadata.st_mode)
            or executable.is_symlink()
            or executable_metadata.st_mode & stat.S_IXUSR == 0
        ):
            raise BaseNativeError("base native artifact is invalid")
        modules = archive_modules(executable)
        if not set(modules) >= _REQUIRED_MODULES or any(
            module == prefix or module.startswith(prefix)
            for module in modules
            for prefix in _FORBIDDEN_MODULE_PREFIXES
        ):
            raise BaseNativeError("base native artifact crosses the product boundary")
        members = _tree_members(selected)
        if any("open_brain_engine/protocol" in member for member in members):
            raise BaseNativeError("base native artifact contains Secure Node protocol data")
        encoded_modules = "\n".join(modules).encode("utf-8")
        encoded_members = "\n".join(members).encode("utf-8")
        return BaseNativeAudit(
            platform_tag=native_platform_tag(),
            module_count=len(modules),
            modules_sha256=hashlib.sha256(encoded_modules).hexdigest(),
            tree_sha256=hashlib.sha256(encoded_members).hexdigest(),
        )
    except BaseNativeError:
        raise
    except OSError as error:
        raise BaseNativeError("base native artifact is unavailable") from error


def smoke_base_artifact(artifact: Path) -> dict[str, object]:
    executable = artifact.resolve(strict=True) / _EXECUTABLE
    with TemporaryDirectory(prefix="open-brain-base-smoke-") as raw:
        home = Path(raw).resolve(strict=True)
        home.chmod(0o700)
        environment = {"HOME": os.fspath(home), "PATH": os.environ.get("PATH", "")}
        self_check = _run((os.fspath(executable), "__open-brain-self-check"), environment)
        if json.loads(self_check.stdout) != {
            "daemon_running": False,
            "frozen": True,
            "profile": "local",
            "status": "ok",
            "version": _VERSION,
        }:
            raise BaseNativeError("base native self-check failed")
        version = _run((os.fspath(executable), "--version"), environment)
        if version.stdout.strip() != f"open-brain {_VERSION}":
            raise BaseNativeError("base native version is invalid")
        first = json.loads(
            _run((os.fspath(executable), "--json", "init"), environment).stdout
        )
        identity = _brain_root(home) / "brain.toml"
        identity_bytes = identity.read_bytes()
        second = json.loads(
            _run((os.fspath(executable), "--json", "init"), environment).stdout
        )
        if (
            first.get("status") != "initialized"
            or second.get("status") != "already_initialized"
            or identity.read_bytes() != identity_bytes
            or first.get("profile") != "local"
            or first.get("storage") != "sqlite"
            or first.get("daemon_running") is not False
            or first.get("application_encryption") is not False
        ):
            raise BaseNativeError("base native bootstrap failed")
        return {"first_init": first, "second_init": second, "self_check": "passed"}


def write_release_assets(
    artifact: Path,
    destination: Path,
    *,
    version: str = _VERSION,
) -> tuple[Path, Path]:
    if re.fullmatch(r"[0-9A-Za-z._-]+", version) is None:
        raise BaseNativeError("invalid release version")
    audit = audit_base_artifact(artifact)
    selected_destination = destination.resolve()
    selected_destination.mkdir(parents=True, exist_ok=True)
    archive = selected_destination / f"open-brain-{version}-{audit.platform_tag}.tar.gz"
    _write_reproducible_archive(artifact.resolve(strict=True), archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = selected_destination / _MANIFEST
    manifest.write_text(
        "open-brain-release-manifest-v1\n"
        f"version {version}\n"
        f"artifact {audit.platform_tag} {digest} {archive.name}\n",
        encoding="ascii",
    )
    return archive, manifest


def smoke_installer(root: Path, release_directory: Path) -> dict[str, object]:
    installer = root.resolve(strict=True) / "release/open-brain/install.sh"
    release = release_directory.resolve(strict=True)
    if not installer.is_file() or not (release / _MANIFEST).is_file():
        raise BaseNativeError("base installer fixture is unavailable")
    with TemporaryDirectory(prefix="open-brain-installer-smoke-") as raw:
        temporary = Path(raw).resolve(strict=True)
        home = temporary / "home"
        fake_bin = temporary / "bin"
        home.mkdir(mode=0o700)
        fake_bin.mkdir(mode=0o700)
        curl = fake_bin / "curl"
        curl.write_text(
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
            encoding="ascii",
        )
        curl.chmod(0o700)
        environment = {
            "FAKE_RELEASE_SOURCE": os.fspath(release),
            "HOME": os.fspath(home),
            "OPEN_BRAIN_ACCEPTANCE_VERSION": _VERSION,
            "OPEN_BRAIN_RELEASE_BASE_URL": "https://example.invalid/release",
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }
        installed = _run(("/bin/sh", os.fspath(installer)), environment)
        launcher = home / ".local/bin/open-brain"
        if (
            installed.stdout
            != '{"command":"open-brain","status":"installed"}\n'
            or not launcher.is_symlink()
            or os.readlink(launcher) != "../lib/open-brain/open-brain"
            or _brain_root(home).exists()
        ):
            raise BaseNativeError("base installer activation failed")
        version = _run((os.fspath(launcher), "--version"), environment)
        if version.stdout.strip() != f"open-brain {_VERSION}":
            raise BaseNativeError("installed base command is unavailable")
        return {"command": "open-brain", "status": "installed", "version": _VERSION}


def build_base_artifact(root: Path, output: Path) -> tuple[Path, Path, Path]:
    if sys.version_info[:2] != (3, 12):
        raise BaseNativeError("base native build requires Python 3.12")
    if (
        importlib.metadata.version("pyinstaller") != "6.22.2"
        or importlib.metadata.version("pyinstaller-hooks-contrib") != "2026.7"
    ):
        raise BaseNativeError("base native build toolchain is not pinned")
    command = pyinstaller_command(root, output)
    subprocess.run(command, cwd=root.resolve(strict=True), check=True, timeout=1800)
    artifact = output.resolve() / "dist/open-brain"
    audit_base_artifact(artifact)
    smoke_base_artifact(artifact)
    archive, manifest = write_release_assets(artifact, output.resolve() / "release")
    smoke_installer(root, archive.parent)
    return artifact, archive, manifest


def _tree_members(root: Path) -> tuple[str, ...]:
    members: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or ".." in PurePosixPath(target).parts:
                raise BaseNativeError("base native artifact has an unsafe symlink")
            kind = "symlink"
            content = target
        elif path.is_dir():
            kind = "directory"
            content = "-"
        elif path.is_file():
            kind = "file"
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            raise BaseNativeError("base native artifact has an unsupported member")
        members.append(f"{kind} {mode:o} {content} {relative}")
    return tuple(members)


def _write_reproducible_archive(root: Path, archive: Path) -> None:
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle,
    ):
        for source in (root, *sorted(root.rglob("*"))):
            relative = source.relative_to(root)
            name = PurePosixPath("open-brain", *relative.parts).as_posix()
            info = bundle.gettarinfo(os.fspath(source), arcname=name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            if info.isfile():
                with source.open("rb") as stream:
                    bundle.addfile(info, stream)
            else:
                bundle.addfile(info)


def _brain_root(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library/Application Support/open-brain/brain"
    return home / ".local/share/open-brain/brain"


def _run(
    command: Sequence[str], environment: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            env=dict(environment),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BaseNativeError("base native runtime check failed") from error


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.open_brain_dev.base_native")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--artifact", type=Path, required=True)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--artifact", type=Path, required=True)
    installer_smoke = subparsers.add_parser("installer-smoke")
    installer_smoke.add_argument("--root", type=Path, required=True)
    installer_smoke.add_argument("--release", type=Path, required=True)
    namespace = parser.parse_args(argv)
    if namespace.command == "build":
        artifact, archive, manifest = build_base_artifact(namespace.root, namespace.output)
        payload: object = {
            "archive": os.fspath(archive),
            "artifact": os.fspath(artifact),
            "manifest": os.fspath(manifest),
            "status": "built",
        }
    elif namespace.command == "audit":
        payload = audit_base_artifact(namespace.artifact).to_dict()
    elif namespace.command == "smoke":
        payload = smoke_base_artifact(namespace.artifact)
    else:
        payload = smoke_installer(namespace.root, namespace.release)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
