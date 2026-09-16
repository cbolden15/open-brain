"""Build and verify the native artifact for default Open Brain."""

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
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, cast

_SPEC = Path("release/open-brain/open-brain.spec")
_EXECUTABLE = "open-brain"
_MANIFEST = "open-brain-component-manifest-v1.txt"
_OBSIDIAN_PLUGIN_ASSETS: Final = ("main.js", "manifest.json", "styles.css")
_PLATFORMS: Final = ("linux-x86_64", "macos-arm64")
_ARCHIVE_PATTERN: Final = re.compile(
    r"^open-brain-(?:(?P<component>graphify)-)?"
    r"(?P<version>[0-9A-Za-z][0-9A-Za-z._-]*)-"
    r"(?P<platform>linux-x86_64|macos-arm64)\.tar\.gz$"
)
_RESOURCE_DESTINATIONS: Final = {
    "base": "bin/open-brain",
    "graphify": "libexec/open-brain-graphify",
}
_REQUIRED_MODULES: Final = frozenset(
    {
        "open_brain.local_data",
        "open_brain.profile",
        "open_brain.services.agent_setup",
        "open_brain.services.local_bootstrap",
        "open_brain.services.local_entrypoints",
        "open_brain.services.graph_projection_store",
        "open_brain.services.graphify_projection",
        "open_brain.services.local_operations",
        "open_brain.services.local_runtime_session",
        "open_brain.services.obsidian_plugin",
        "open_brain.services.plugin_bridge",
        "open_brain.services.provider_credentials",
        "open_brain.services.local_mcp",
        "open_brain.services.space_inbox",
        "open_brain.services.mcp_protocol",
        "open_brain_engine.engine.capture",
        "open_brain_engine.engine.local",
        "open_brain_engine.engine.local_schema",
        "open_brain_engine.engine.local_schema_catalog",
        "open_brain_engine.engine.markdown_import",
        "open_brain_engine.engine.markdown_import_fs",
        "open_brain_engine.engine.portability",
        "open_brain_engine.engine.retrieval",
        "open_brain_engine.storage.operational",
        "open_brain_engine.storage.sqlite",
        "open_brain_engine.storage.migrations",
    }
)
_FORBIDDEN_MODULE_PREFIXES: Final = (
    "argon2",
    "capng",
    "cryptography",
    "docker",
    "http.server",
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
    "open_brain_collector",
    "open_brain_connectors",
    "open_brain_engine.portability.secure_node",
    "open_brain_engine.engine.authority",
    "open_brain_engine.engine.backup",
    "open_brain_engine.ledger",
    "open_brain_engine.protocol",
    "open_brain_legacy",
    "podman",
    "prctl",
    "pyroute2",
    "socketserver",
    "sqlcipher3",
    "starlette",
    "systemd",
    "uvicorn",
)


class BaseNativeError(RuntimeError):
    """The default native artifact failed its release contract."""


@dataclass(frozen=True, slots=True)
class BaseNativeAudit:
    platform_tag: str
    module_count: int
    modules_sha256: str
    executable_sha256: str
    signature: str

    def to_dict(self) -> dict[str, object]:
        return {
            "executable_sha256": self.executable_sha256,
            "module_count": self.module_count,
            "modules_sha256": self.modules_sha256,
            "platform": self.platform_tag,
            "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    version: str
    platform_tag: str
    sha256: str
    filename: str
    role: str
    executable_sha256: str
    destination: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    version: str
    artifacts: tuple[ReleaseArtifact, ...]


def native_platform_tag(*, system_name: str | None = None, machine_name: str | None = None) -> str:
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


def product_version(root: Path) -> str:
    metadata = tomllib.loads(
        (root.resolve(strict=True) / "packages/app/pyproject.toml").read_text(encoding="utf-8")
    )
    project = metadata.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version) is None:
        raise BaseNativeError("Open Brain package version is invalid")
    return version


def pyinstaller_command(root: Path, output: Path) -> tuple[str, ...]:
    spec = root.resolve(strict=True) / _SPEC
    if not spec.is_file():
        raise BaseNativeError("native spec is unavailable")
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
            raise BaseNativeError("native module inventory is invalid")
        return tuple(sorted(toc))
    except BaseNativeError:
        raise
    except Exception as error:
        raise BaseNativeError("native module inventory is unavailable") from error


def audit_base_artifact(artifact: Path) -> BaseNativeAudit:
    try:
        selected = artifact.resolve(strict=True)
        metadata = artifact.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or artifact.is_symlink()
            or metadata.st_mode & stat.S_IXUSR == 0
        ):
            raise BaseNativeError("native artifact is invalid")
        modules = archive_modules(selected)
        if not set(modules) >= _REQUIRED_MODULES or any(
            module == prefix or module.startswith(prefix)
            for module in modules
            for prefix in _FORBIDDEN_MODULE_PREFIXES
        ):
            raise BaseNativeError("native artifact crosses the product boundary")
        platform_tag = native_platform_tag()
        signature = _validate_native_executable(selected, platform_tag)
        encoded_modules = "\n".join(modules).encode("utf-8")
        return BaseNativeAudit(
            platform_tag=platform_tag,
            module_count=len(modules),
            modules_sha256=hashlib.sha256(encoded_modules).hexdigest(),
            executable_sha256=_sha256(selected),
            signature=signature,
        )
    except BaseNativeError:
        raise
    except OSError as error:
        raise BaseNativeError("native artifact is unavailable") from error


def smoke_base_artifact(
    artifact: Path,
    *,
    version: str,
    repository_root: Path,
    graphify_artifact: Path | None = None,
) -> dict[str, object]:
    executable = artifact.resolve(strict=True)
    with TemporaryDirectory(prefix="open-brain-smoke-") as raw:
        home = Path(raw).resolve(strict=True)
        home.chmod(0o700)
        environment = {"HOME": os.fspath(home), "PATH": os.environ.get("PATH", "")}
        self_check = _run((os.fspath(executable), "__open-brain-self-check"), environment)
        if json.loads(self_check.stdout) != {
            "daemon_running": False,
            "frozen": True,
            "profile": "local",
            "status": "ok",
            "version": version,
        }:
            raise BaseNativeError("native self-check failed")
        reported_version = _run((os.fspath(executable), "--version"), environment)
        if reported_version.stdout.strip() != f"open-brain {version}":
            raise BaseNativeError("native version is invalid")
        journey = _smoke_local_journey(executable, home, environment)
        _smoke_markdown_import(
            executable,
            home,
            environment,
            repository_root.resolve(strict=True) / "examples/markdown-fixture",
        )
        journey["markdown_import"] = "passed"
        _smoke_local_mcp(executable, home, environment)
        journey["local_mcp"] = "passed"
        if graphify_artifact is not None:
            _smoke_graphify_projection(executable, graphify_artifact, home, environment)
            journey["graph_projection"] = "passed"
            _smoke_obsidian_plugin(executable, home, environment)
            journey["obsidian_plugin"] = "passed"
        return {"journey": journey, "self_check": "passed"}


def write_base_archive(
    artifact: Path,
    destination: Path,
    *,
    version: str,
    obsidian_plugin_directory: Path,
) -> Path:
    audit = audit_base_artifact(artifact)
    selected_destination = destination.resolve()
    selected_destination.mkdir(parents=True, exist_ok=True)
    archive = selected_destination / f"open-brain-{version}-{audit.platform_tag}.tar.gz"
    _write_reproducible_archive(
        artifact.resolve(strict=True),
        archive,
        obsidian_plugin_directory=obsidian_plugin_directory,
        version=version,
    )
    return archive


def write_release_manifest(artifacts: Sequence[ReleaseArtifact], destination: Path) -> Path:
    role_order = {"base": 0, "graphify": 1}
    ordered = tuple(
        sorted(artifacts, key=lambda item: (item.platform_tag, role_order.get(item.role, 99)))
    )
    keys = {(item.platform_tag, item.role) for item in ordered}
    platforms = {item.platform_tag for item in ordered}
    if (
        not ordered
        or len(keys) != len(ordered)
        or any({(platform, "base"), (platform, "graphify")} - keys for platform in platforms)
        or len(ordered) != len(platforms) * 2
    ):
        raise BaseNativeError("release manifest resource pairs are invalid")
    versions = {item.version for item in ordered}
    if len(versions) != 1:
        raise BaseNativeError("release manifest versions do not match")
    version = next(iter(versions))
    lines = ["open-brain-component-manifest-v1", f"version {version}"]
    for item in ordered:
        _validate_release_artifact(item)
        lines.append(
            " ".join(
                (
                    "resource",
                    item.role,
                    item.platform_tag,
                    item.sha256,
                    item.executable_sha256,
                    item.filename,
                    item.destination,
                )
            )
        )
    selected = destination.resolve()
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text("\n".join(lines) + "\n", encoding="ascii")
    return selected


def combine_release_manifest(archives: Sequence[Path], destination: Path) -> Path:
    return write_release_manifest(tuple(_release_artifact(path) for path in archives), destination)


def read_release_manifest(path: Path) -> ReleaseManifest:
    try:
        lines = path.resolve(strict=True).read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise BaseNativeError("release manifest is unavailable") from error
    if len(lines) < 4 or lines[0] != "open-brain-component-manifest-v1":
        raise BaseNativeError("release manifest header is invalid")
    version_record = lines[1].split(" ", 1)
    if len(version_record) != 2 or version_record[0] != "version":
        raise BaseNativeError("release manifest version is invalid")
    artifacts: list[ReleaseArtifact] = []
    for line in lines[2:]:
        fields = line.split(" ")
        if len(fields) != 7 or fields[0] != "resource":
            raise BaseNativeError("release manifest resource is invalid")
        artifacts.append(
            ReleaseArtifact(
                version=version_record[1],
                role=fields[1],
                platform_tag=fields[2],
                sha256=fields[3],
                executable_sha256=fields[4],
                filename=fields[5],
                destination=fields[6],
            )
        )
    manifest = ReleaseManifest(version_record[1], tuple(artifacts))
    for item in manifest.artifacts:
        _validate_release_artifact(item)
    canonical = tuple(
        sorted(
            manifest.artifacts,
            key=lambda item: (item.platform_tag, {"base": 0, "graphify": 1}.get(item.role, 99)),
        )
    )
    keys = {(item.platform_tag, item.role) for item in manifest.artifacts}
    platforms = {item.platform_tag for item in manifest.artifacts}
    if (
        len(keys) != len(manifest.artifacts)
        or len(manifest.artifacts) != len(platforms) * 2
        or any({(platform, "base"), (platform, "graphify")} - keys for platform in platforms)
    ):
        raise BaseNativeError("release manifest resource pairs are invalid")
    if manifest.artifacts != canonical:
        raise BaseNativeError("release manifest is not canonical")
    return manifest


def render_homebrew_formula(
    manifest_path: Path,
    destination: Path,
    *,
    repository: str = "cbolden15/open-brain",
    base_url: str | None = None,
    smoke: bool = False,
) -> Path:
    manifest = read_release_manifest(manifest_path)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise BaseNativeError("Homebrew repository is invalid")
    origin = (
        f"https://github.com/{repository}/releases/download/v{manifest.version}"
        if base_url is None
        else base_url.rstrip("/")
    )
    if not origin.startswith(("https://", "file://")) or any(
        character in origin for character in ('"', "\n", "\r")
    ):
        raise BaseNativeError("Homebrew artifact origin is invalid")
    platform_blocks = {
        "linux-x86_64": ("on_linux", "x86_64"),
        "macos-arm64": ("on_macos", "arm64"),
    }
    lines = [
        "class OpenBrainSmoke < Formula" if smoke else "class OpenBrain < Formula",
        '  desc "Local-first capture, search, and portable export"',
        f'  homepage "https://github.com/{repository}"',
        f'  version "{manifest.version}"',
        '  license "Apache-2.0"',
        "",
    ]
    if smoke:
        lines.extend(('  keg_only "Temporary contributor smoke fixture"', ""))
    for platform_tag in sorted({item.platform_tag for item in manifest.artifacts}):
        resources = {
            item.role: item for item in manifest.artifacts if item.platform_tag == platform_tag
        }
        base = resources["base"]
        helper = resources["graphify"]
        block, architecture = platform_blocks[platform_tag]
        lines.extend(
            (
                f"  {block} do",
                f"    depends_on arch: :{architecture}",
                f'    url "{origin}/{base.filename}"',
                f'    sha256 "{base.sha256}"',
                '    resource "graphify" do',
                f'      url "{origin}/{helper.filename}"',
                f'      sha256 "{helper.sha256}"',
                "    end",
                "  end",
                "",
            )
        )
    lines.extend(
        (
            "  def install",
            f'    bin.install "{_EXECUTABLE}"',
            '    (share/"open-brain/obsidian-plugin").install Dir["obsidian-plugin/*"]',
            '    resource("graphify").stage do',
            '      libexec.install "open-brain" => "open-brain-graphify"',
            '      (share/"open-brain/licenses/graphify").install Dir["licenses/graphify/*"]',
            "    end",
            "  end",
            "",
            "  test do",
            (
                f'    assert_match "open-brain #{{version}}", '
                f'shell_output("#{{bin}}/{_EXECUTABLE} --version")'
            ),
            '    assert_path_exists share/"open-brain/obsidian-plugin/main.js"',
            (
                '    assert_match "open-brain-graphify-helper-v1", '
                'shell_output("#{libexec}/open-brain-graphify --capabilities")'
            ),
            "  end",
            "end",
            "",
        )
    )
    selected = destination.resolve()
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text("\n".join(lines), encoding="utf-8")
    return selected


def build_base_artifact(root: Path, output: Path) -> tuple[Path, Path, Path, Path, Path]:
    if sys.version_info[:2] != (3, 14):
        raise BaseNativeError("native build requires Python 3.14")
    if (
        importlib.metadata.version("pyinstaller") != "6.22.2"
        or importlib.metadata.version("pyinstaller-hooks-contrib") != "2026.7"
    ):
        raise BaseNativeError("native build toolchain is not pinned")
    version = product_version(root)
    subprocess.run(
        pyinstaller_command(root, output),
        cwd=root.resolve(strict=True),
        check=True,
        timeout=1800,
    )
    artifact = output.resolve() / "dist/open-brain"
    audit_base_artifact(artifact)
    smoke_base_artifact(artifact, version=version, repository_root=root)
    archive = write_base_archive(
        artifact,
        output.resolve() / "release",
        version=version,
        obsidian_plugin_directory=(root.resolve(strict=True) / "build/obsidian-plugin/open-brain"),
    )
    from tools.open_brain_dev.graphify_native import build_graphify_artifact

    graphify_artifact, graphify_archive = build_graphify_artifact(root, output, version=version)
    manifest = write_release_manifest(
        (
            _release_artifact(archive, executable=artifact),
            _release_artifact(graphify_archive, executable=graphify_artifact),
        ),
        output.resolve() / "release" / _MANIFEST,
    )
    return artifact, graphify_artifact, archive, graphify_archive, manifest


def _release_artifact(archive: Path, *, executable: Path | None = None) -> ReleaseArtifact:
    selected = archive.resolve(strict=True)
    match = _ARCHIVE_PATTERN.fullmatch(selected.name)
    if match is None or not selected.is_file():
        raise BaseNativeError("release artifact filename is invalid")
    role = "graphify" if match.group("component") == "graphify" else "base"
    return ReleaseArtifact(
        version=match.group("version"),
        platform_tag=match.group("platform"),
        sha256=_sha256(selected),
        filename=selected.name,
        role=role,
        executable_sha256=(
            _sha256(executable.resolve(strict=True))
            if executable is not None
            else _archive_executable_sha256(selected)
        ),
        destination=_RESOURCE_DESTINATIONS[role],
    )


def _validate_release_artifact(artifact: ReleaseArtifact) -> None:
    match = _ARCHIVE_PATTERN.fullmatch(artifact.filename)
    if (
        artifact.platform_tag not in _PLATFORMS
        or artifact.role not in _RESOURCE_DESTINATIONS
        or artifact.destination != _RESOURCE_DESTINATIONS.get(artifact.role)
        or re.fullmatch(r"[0-9a-f]{64}", artifact.sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", artifact.executable_sha256) is None
        or match is None
        or match.group("version") != artifact.version
        or match.group("platform") != artifact.platform_tag
        or ("graphify" if match.group("component") == "graphify" else "base") != artifact.role
    ):
        raise BaseNativeError("release artifact record is invalid")


def _archive_executable_sha256(archive: Path) -> str:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            matches = [
                member
                for member in bundle.getmembers()
                if member.name == _EXECUTABLE and member.isfile() and not member.issym()
            ]
            if len(matches) != 1 or not 0 < matches[0].size <= 64 * 1024 * 1024:
                raise BaseNativeError("release archive executable is invalid")
            stream = bundle.extractfile(matches[0])
            if stream is None:
                raise BaseNativeError("release archive executable is invalid")
            payload = stream.read(matches[0].size + 1)
            if len(payload) != matches[0].size:
                raise BaseNativeError("release archive executable is invalid")
            return hashlib.sha256(payload).hexdigest()
    except BaseNativeError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise BaseNativeError("release archive is invalid") from error


def _validate_native_executable(executable: Path, platform_tag: str) -> str:
    if platform_tag == "linux-x86_64":
        header = executable.read_bytes()[:20]
        if (
            len(header) != 20
            or header[:4] != b"\x7fELF"
            or header[4] != 2
            or header[5] not in {1, 2}
            or int.from_bytes(header[18:20], "little" if header[5] == 1 else "big") != 62
        ):
            raise BaseNativeError("native Linux executable is not x86_64")
        return "not-applicable"
    architecture = _run_build_tool(("/usr/bin/lipo", "-archs", os.fspath(executable)))
    if architecture.stdout.strip() != "arm64":
        raise BaseNativeError("native macOS executable is not arm64")
    _run_build_tool(("/usr/bin/codesign", "--verify", "--strict", os.fspath(executable)))
    details = _run_build_tool(("/usr/bin/codesign", "-dv", "--verbose=4", os.fspath(executable)))
    return "adhoc" if "Signature=adhoc" in details.stderr else "identity"


def _smoke_local_journey(
    executable: Path,
    home: Path,
    environment: Mapping[str, str],
) -> dict[str, object]:
    brain_root = _brain_root(home)
    if brain_root.exists():
        raise BaseNativeError("native journey did not start clean")
    token = "open-brain-five-minute-acceptance"
    capture = json.loads(
        _run((os.fspath(executable), "capture", token, "--json"), environment).stdout
    )
    if (
        capture.get("status") != "captured"
        or not isinstance(capture.get("capture_id"), str)
        or token in json.dumps(capture)
        or not (brain_root / "brain.toml").is_file()
        or not (brain_root / ".open-brain/state/phase1.sqlite3").is_file()
    ):
        raise BaseNativeError("native first capture failed")
    diacritic = json.loads(
        _run(
            (os.fspath(executable), "capture", "Café constellation", "--json"),
            environment,
        ).stdout
    )
    title_hit = json.loads(
        _run(
            (os.fspath(executable), "capture", "Aurora field atlas\nQuiet body", "--json"),
            environment,
        ).stdout
    )
    body_hit = json.loads(
        _run(
            (
                os.fspath(executable),
                "capture",
                "Ordinary heading\nAn aurora atlas for observers",
                "--json",
            ),
            environment,
        ).stdout
    )
    search = cast(
        dict[str, object],
        json.loads(
            _run(
                (os.fspath(executable), "search", token, "--limit", "1", "--json"),
                environment,
            ).stdout
        ),
    )
    search_results = cast(list[dict[str, object]], search.get("results"))
    if not search_results or search_results[0].get("capture_id") != capture.get("capture_id"):
        raise BaseNativeError("native search failed")
    unicode_search = cast(
        dict[str, object],
        json.loads(
            _run(
                (os.fspath(executable), "search", "CAFE", "--limit", "1", "--json"),
                environment,
            ).stdout
        ),
    )
    unicode_results = cast(list[dict[str, object]], unicode_search.get("results"))
    if not unicode_results or unicode_results[0].get("capture_id") != diacritic.get("capture_id"):
        raise BaseNativeError("native FTS tokenizer failed")
    ranked_search = cast(
        dict[str, object],
        json.loads(
            _run(
                (
                    os.fspath(executable),
                    "search",
                    "aurora atlas",
                    "--limit",
                    "2",
                    "--json",
                ),
                environment,
            ).stdout
        ),
    )
    ranked_results = cast(list[dict[str, object]], ranked_search.get("results"))
    if (
        [result.get("capture_id") for result in ranked_results]
        != [title_hit.get("capture_id"), body_hit.get("capture_id")]
        or ranked_results[0].get("explanation") != "title match"
        or "[" not in str(ranked_results[0].get("excerpt"))
        or ranked_results[0].get("source_origin") != "owner_authored"
    ):
        raise BaseNativeError("native FTS ranking failed")
    export = home / "portable-export"
    exported = json.loads(
        _run(
            (
                os.fspath(executable),
                "export",
                os.fspath(export),
                "--verify",
                "--json",
            ),
            environment,
        ).stdout
    )
    if (
        exported.get("status") != "exported"
        or exported.get("verification") != "verified"
        or exported.get("schema_version") != 1
        or not (export / "portable-manifest.json").is_file()
        or not any(
            token.encode("utf-8") in path.read_bytes()
            for path in export.rglob("*")
            if path.is_file()
        )
        or any(".open-brain" in path.parts for path in export.rglob("*"))
        or any(path.suffix in {".sqlite", ".sqlite3"} for path in export.rglob("*"))
    ):
        raise BaseNativeError("native verified export failed")
    status_result = cast(
        dict[str, object],
        json.loads(_run((os.fspath(executable), "status", "--json"), environment).stdout),
    )
    if status_result != {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": False,
        "live_search": {
            "authoritative": True,
            "contents_agree": True,
            "fts_count": 4,
            "identity_count": 4,
            "projection_count": 4,
            "result_ids_agree": True,
            "state": "current",
        },
        "portable_export": "verified",
        "portable_snapshot": {
            "authoritative": False,
            "document_count": 0,
            "freshness": "potentially_stale",
            "generation": None,
            "state": "absent",
        },
        "profile": "local",
        "storage": "sqlite",
    }:
        raise BaseNativeError("native status failed")
    for check in (
        "private-data-directory",
        "foreground-runtime",
        "base-dependency-closure",
        "search-index",
    ):
        checked = _run((os.fspath(executable), "doctor", "--check", check), environment)
        if checked.stdout != f"{check}: ok\n":
            raise BaseNativeError("native doctor failed")
    run_root = brain_root / ".open-brain/run"
    if run_root.is_dir() and any(run_root.iterdir()):
        raise BaseNativeError("native journey left a background runtime artifact")
    return {
        "capture": "passed",
        "doctor": "passed",
        "export": "verified",
        "search": "passed",
        "status": "passed",
    }


def _smoke_markdown_import(
    executable: Path,
    home: Path,
    environment: Mapping[str, str],
    fixture: Path,
) -> None:
    if not fixture.is_dir():
        raise BaseNativeError("Markdown fixture is unavailable")
    vault = home / "markdown-fixture"
    shutil.copytree(fixture, vault)
    imported = cast(
        dict[str, object],
        json.loads(
            _run(
                (os.fspath(executable), "import", os.fspath(vault), "--yes", "--json"),
                environment,
            ).stdout
        ),
    )
    if (
        imported.get("status") != "completed"
        or imported.get("selected") != 3
        or imported.get("imported") != 3
        or imported.get("skipped") != 1
        or imported.get("missing_finalized") is not True
    ):
        raise BaseNativeError("native Markdown import failed")

    search = cast(
        dict[str, object],
        json.loads(
            _run(
                (
                    os.fspath(executable),
                    "search",
                    "w4-nested-markdown-fixture-token",
                    "--limit",
                    "1",
                    "--json",
                ),
                environment,
            ).stdout
        ),
    )
    results = cast(list[dict[str, object]], search.get("results"))
    if (
        len(results) != 1
        or results[0].get("title") != "Nested fixture note"
        or results[0].get("trust") != "unverified"
        or results[0].get("source_origin") != "unknown"
    ):
        raise BaseNativeError("native Markdown search failed")
    ignored = cast(
        dict[str, object],
        json.loads(
            _run(
                (
                    os.fspath(executable),
                    "search",
                    "w4-obsidian-metadata-must-not-search",
                    "--json",
                ),
                environment,
            ).stdout
        ),
    )
    if ignored.get("results") != []:
        raise BaseNativeError("native Markdown metadata exclusion failed")

    repeated = cast(
        dict[str, object],
        json.loads(
            _run(
                (os.fspath(executable), "import", os.fspath(vault), "--json"),
                environment,
            ).stdout
        ),
    )
    if (
        repeated.get("unchanged") != 3
        or repeated.get("imported") != 0
        or repeated.get("updated") != 0
    ):
        raise BaseNativeError("native Markdown import replay failed")

    export = home / "portable-export-after-import"
    exported = cast(
        dict[str, object],
        json.loads(
            _run(
                (
                    os.fspath(executable),
                    "export",
                    os.fspath(export),
                    "--verify",
                    "--json",
                ),
                environment,
            ).stdout
        ),
    )
    source_bytes = (fixture / "nested/search-note.md").read_bytes()
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    blob = export / f"sources/blobs/sha256/{source_digest[:2]}/{source_digest}"
    capture_id = results[0].get("capture_id")
    captures = tuple((export / "sources/captures").rglob(f"{capture_id}.json"))
    if (
        exported.get("status") != "exported"
        or exported.get("verification") != "verified"
        or exported.get("schema_version") != 1
        or not blob.is_file()
        or blob.read_bytes() != source_bytes
        or len(captures) != 1
    ):
        raise BaseNativeError("native Markdown export failed")
    capture = cast(dict[str, object], json.loads(captures[0].read_bytes()))
    source = cast(dict[str, object], capture.get("source"))
    provenance = cast(dict[str, object], capture.get("provenance"))
    trust = cast(dict[str, object], capture.get("trust"))
    if (
        source.get("origin") != "third_party"
        or provenance.get("content_origin") != "unknown"
        or provenance.get("owner_context") != "automation_absent"
        or trust.get("label") != "unverified"
    ):
        raise BaseNativeError("native Markdown provenance failed")


def _smoke_local_mcp(executable: Path, home: Path, environment: Mapping[str, str]) -> None:
    """Exercise installed stdio capabilities after the required CLI journey."""
    token = "w6-installed-mcp-capture-token"
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}},
    }

    def exchange(
        flag: str, name: str, arguments: dict[str, object], *, tools: set[str] | None = None
    ) -> dict[str, object]:
        requests = [
            initialize,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        ]
        try:
            process = subprocess.run(
                (os.fspath(executable), "mcp", flag),
                env=dict(environment),
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            responses = [json.loads(line) for line in process.stdout.splitlines()]
            if (
                process.stderr
                or len(responses) != 3
                or responses[0]["result"]["capabilities"] != {"tools": {}}
                or {tool["name"] for tool in responses[1]["result"]["tools"]} != (tools or {name})
                or responses[2]["result"].get("isError")
            ):
                raise BaseNativeError("native MCP exchange failed")
            return cast(dict[str, object], responses[2]["result"]["structuredContent"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            raise BaseNativeError("native MCP exchange failed") from error

    capture = exchange(
        "--allow-capture",
        "brain_capture",
        {
            "text": token,
            "idempotency_key": "installed-smoke",
        },
    )
    repeated = exchange(
        "--allow-capture",
        "brain_capture",
        {
            "text": token,
            "idempotency_key": "installed-smoke",
        },
    )
    if capture.get("status") != "captured" or repeated != {**capture, "duplicate": True}:
        raise BaseNativeError("native MCP replay failed")
    search = exchange("--allow-search", "brain_search", {"query": token})
    cli_search = json.loads(
        _run((os.fspath(executable), "search", token, "--json"), environment).stdout
    )
    results = cast(list[dict[str, object]], search.get("results"))
    if (
        search != cli_search
        or len(results) != 1
        or results[0].get("capture_id") != capture.get("capture_id")
        or results[0].get("trust") != "unverified"
        or results[0].get("source_origin") != "unknown"
    ):
        raise BaseNativeError("native MCP search failed")
    organization_tools = {"brain_space_create", "brain_space_rename", "brain_inbox_route"}
    read_tools = {"brain_space_list", "brain_inbox_list"}
    created = exchange(
        "--allow-organize", "brain_space_create",
        {"name": "Installed organization", "idempotency_key": "installed-space"},
        tools=organization_tools,
    )
    repeated_space = json.loads(_run(
        (os.fspath(executable), "space", "create", "Installed organization",
         "--idempotency-key", "installed-space", "--json"), environment,
    ).stdout)
    if repeated_space != created:
        raise BaseNativeError("native organization replay failed")
    space_id = cast(dict[str, str], created["space"])["space_id"]
    renamed = json.loads(_run(
        (os.fspath(executable), "space", "rename", space_id, "Installed renamed", "--json"),
        environment,
    ).stdout)
    spaces = exchange("--allow-inbox-read", "brain_space_list", {}, tools=read_tools)
    if renamed["space"] not in cast(list[dict[str, object]], spaces["spaces"]):
        raise BaseNativeError("native organization rename failed")
    route_arguments = {
        "capture_id": capture["capture_id"], "space_id": space_id,
        "idempotency_key": "installed-route",
    }
    routed = exchange(
        "--allow-organize", "brain_inbox_route", route_arguments, tools=organization_tools,
    )
    repeated_route = json.loads(_run(
        (os.fspath(executable), "inbox", "route", cast(str, capture["capture_id"]), space_id,
         "--idempotency-key", "installed-route", "--json"), environment,
    ).stdout)
    inbox = exchange(
        "--allow-inbox-read", "brain_inbox_list", {"unassigned_only": True}, tools=read_tools,
    )
    assigned = json.loads(_run(
        (os.fspath(executable), "inbox", "list", "--json"), environment,
    ).stdout)
    if (
        routed != repeated_route
        or any(item["capture_id"] == capture["capture_id"]
               for item in cast(list[dict[str, object]], inbox["items"]))
        or not any(item["capture_id"] == capture["capture_id"] and item["space_id"] == space_id
                   for item in assigned["items"])
        or exchange("--allow-search", "brain_search", {"query": token}) != search
    ):
        raise BaseNativeError("native organization routing failed")
    run_root = _brain_root(home) / ".open-brain/run"
    if run_root.is_dir() and any(run_root.iterdir()):
        raise BaseNativeError("native MCP left a runtime artifact")


def _smoke_graphify_projection(
    executable: Path,
    graphify_artifact: Path,
    home: Path,
    environment: Mapping[str, str],
) -> None:
    expected = executable.parent.parent / "libexec/open-brain-graphify"
    try:
        if graphify_artifact.resolve(strict=True) != expected.resolve(strict=True):
            raise BaseNativeError("installed Graphify helper is not in the base keg")
    except OSError as error:
        raise BaseNativeError("installed Graphify helper is unavailable") from error
    _seed_managed_graph_fixture(_brain_root(home))
    setup = json.loads(
        _run((os.fspath(executable), "workspace", "setup", "--json"), environment).stdout
    )
    if setup.get("status") != "setup" or not isinstance(setup.get("workspace_id"), str):
        raise BaseNativeError("native managed workspace setup failed")
    refreshed = json.loads(
        _run(
            (os.fspath(executable), "graph", "refresh-structural", "--json"),
            environment,
        ).stdout
    )
    projected = json.loads(
        _run((os.fspath(executable), "graph", "projection", "--json"), environment).stdout
    )
    canvas = json.loads(
        _run((os.fspath(executable), "graph", "canvas", "--json"), environment).stdout
    )
    canvas_body = canvas.get("canvas")
    structural_links = refreshed.get("structural_links")
    inferred_suggestions = refreshed.get("inferred_suggestions")
    if (
        refreshed != projected
        or refreshed.get("status") != "fresh"
        or not str(refreshed.get("adapter_identity", "")).startswith("graphify:")
        or not isinstance(structural_links, list)
        or len(structural_links) != 1
        or not isinstance(inferred_suggestions, list)
        or canvas.get("status") != "fresh"
        or canvas.get("generation_id") != refreshed.get("generation_id")
        or not isinstance(canvas_body, dict)
        or set(canvas_body) != {"edges", "nodes"}
        or not isinstance(canvas_body["edges"], list)
        or len(canvas_body["edges"]) != 1
        or not isinstance(canvas_body["nodes"], list)
        or len(canvas_body["nodes"]) != 3
        or any(home.rglob("*.canvas"))
    ):
        raise BaseNativeError("native Graphify projection failed")
    run_root = _brain_root(home) / ".open-brain/run"
    if run_root.is_dir() and any(run_root.iterdir()):
        raise BaseNativeError("native Graphify projection left a runtime artifact")


def _smoke_obsidian_plugin(
    executable: Path,
    home: Path,
    environment: Mapping[str, str],
) -> None:
    installed = json.loads(
        _run((os.fspath(executable), "obsidian-plugin", "install", "--json"), environment).stdout
    )
    status = json.loads(
        _run((os.fspath(executable), "obsidian-plugin", "status", "--json"), environment).stdout
    )
    workspace = _brain_root(home).parent / "Open Brain Vault"
    plugin = workspace / ".obsidian/plugins/open-brain"
    if (
        installed.get("status") != "installed"
        or status.get("status") != "current"
        or not all((plugin / name).is_file() for name in ("main.js", "manifest.json", "styles.css"))
        or (workspace / ".obsidian/community-plugins.json").exists()
    ):
        raise BaseNativeError("native Obsidian plugin install failed")
    removed = json.loads(
        _run((os.fspath(executable), "obsidian-plugin", "remove", "--json"), environment).stdout
    )
    if removed.get("status") != "removed" or any(
        (plugin / name).exists()
        for name in ("main.js", "manifest.json", "styles.css", ".open-brain-owned.json")
    ):
        raise BaseNativeError("native Obsidian plugin removal failed")
    run_root = _brain_root(home) / ".open-brain/run"
    if run_root.is_dir() and any(run_root.iterdir()):
        raise BaseNativeError("native Obsidian plugin journey left a runtime artifact")


def _seed_managed_graph_fixture(brain_root: Path) -> None:
    from open_brain_engine.engine import CaptureAction, TextPayload, open_local_engine

    from open_brain.profile import open_existing_single_user_local

    tasks = open_local_engine(open_existing_single_user_local(brain_root))
    space_id = tasks.inbox.create_space(
        "Graph smoke",
        delivery_id="native.graph.space",
    ).space_id
    target = tasks.capture.accept(
        TextPayload("# Structural target\n"),
        delivery_id="native.graph.second",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
    )
    tasks.reconciliation.reconcile()
    target_pages = [
        result.result_id
        for result in tasks.retrieval.search("Structural target", limit=4)
        if result.record_type == "canonical" and result.capture_id == target.capture_id
    ]
    if len(target_pages) != 1:
        raise BaseNativeError("native graph fixture target is invalid")
    target_id = target_pages[0]
    tasks.capture.accept(
        TextPayload(f"# Structural source\n[[{target_id}]]\n"),
        delivery_id="native.graph.first",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space_id,
    )


def _write_reproducible_archive(
    executable: Path,
    archive: Path,
    *,
    obsidian_plugin_directory: Path,
    version: str,
) -> None:
    plugin_assets = _validated_obsidian_plugin_assets(obsidian_plugin_directory, version=version)
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle,
        executable.open("rb") as stream,
    ):
        info = bundle.gettarinfo(os.fspath(executable), arcname=_EXECUTABLE)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        bundle.addfile(info, stream)
        for name, path in plugin_assets:
            with path.open("rb") as asset_stream:
                asset_info = bundle.gettarinfo(os.fspath(path), arcname=f"obsidian-plugin/{name}")
                asset_info.uid = 0
                asset_info.gid = 0
                asset_info.uname = ""
                asset_info.gname = ""
                asset_info.mode = 0o644
                asset_info.mtime = 0
                bundle.addfile(asset_info, asset_stream)


def _validated_obsidian_plugin_assets(
    directory: Path, *, version: str
) -> tuple[tuple[str, Path], ...]:
    try:
        selected = directory.resolve(strict=True)
        metadata = directory.stat(follow_symlinks=False)
        if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise BaseNativeError("Obsidian plugin build is invalid")
        result: list[tuple[str, Path]] = []
        for name in _OBSIDIAN_PLUGIN_ASSETS:
            path = selected / name
            item = path.stat(follow_symlinks=False)
            if path.is_symlink() or not stat.S_ISREG(item.st_mode) or item.st_size <= 0:
                raise BaseNativeError("Obsidian plugin build is invalid")
            result.append((name, path))
        manifest = json.loads((selected / "manifest.json").read_bytes())
        if (
            not isinstance(manifest, dict)
            or manifest.get("id") != "open-brain"
            or manifest.get("version") != version
            or manifest.get("isDesktopOnly") is not True
        ):
            raise BaseNativeError("Obsidian plugin build is invalid")
        return tuple(result)
    except BaseNativeError:
        raise
    except OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError:
        raise BaseNativeError("Obsidian plugin build is unavailable") from None


def _brain_root(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library/Application Support/open-brain/brain"
    return home / ".local/share/open-brain/brain"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_build_tool(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BaseNativeError("native executable verification failed") from error


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
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BaseNativeError("native runtime check failed") from error


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.open_brain_dev.base_native")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--artifact", type=Path, required=True)
    audit.add_argument("--graphify-artifact", type=Path, required=True)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--root", type=Path, required=True)
    smoke.add_argument("--artifact", type=Path, required=True)
    smoke.add_argument("--graphify-artifact", type=Path, required=True)
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--artifact", type=Path, action="append", required=True)
    manifest.add_argument("--output", type=Path, required=True)
    formula = subparsers.add_parser("formula")
    formula.add_argument("--manifest", type=Path, required=True)
    formula.add_argument("--output", type=Path, required=True)
    formula.add_argument("--repository", default="cbolden15/open-brain")
    formula.add_argument("--base-url")
    formula.add_argument("--smoke", action="store_true")
    namespace = parser.parse_args(argv)
    if namespace.command == "build":
        artifact, graphify_artifact, archive, graphify_archive, manifest_path = build_base_artifact(
            namespace.root, namespace.output
        )
        payload: object = {
            "archive": os.fspath(archive),
            "artifact": os.fspath(artifact),
            "graphify_archive": os.fspath(graphify_archive),
            "graphify_artifact": os.fspath(graphify_artifact),
            "manifest": os.fspath(manifest_path),
            "status": "built",
        }
    elif namespace.command == "audit":
        from tools.open_brain_dev.graphify_native import audit_graphify_artifact

        payload = {
            "base": audit_base_artifact(namespace.artifact).to_dict(),
            "graphify": audit_graphify_artifact(namespace.graphify_artifact).to_dict(),
        }
    elif namespace.command == "smoke":
        payload = smoke_base_artifact(
            namespace.artifact,
            version=product_version(namespace.root),
            repository_root=namespace.root,
            graphify_artifact=namespace.graphify_artifact,
        )
    elif namespace.command == "manifest":
        output = combine_release_manifest(namespace.artifact, namespace.output)
        payload = {"manifest": os.fspath(output), "status": "written"}
    else:
        output = render_homebrew_formula(
            namespace.manifest,
            namespace.output,
            repository=namespace.repository,
            base_url=namespace.base_url,
            smoke=namespace.smoke,
        )
        payload = {"formula": os.fspath(output), "status": "written"}
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
