"""Build and verify the native artifact for default Open Brain."""

from __future__ import annotations

import argparse
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
MANIFEST_V2_FILENAME: Final = "open-brain-component-manifest-v2.json"
_OBSIDIAN_PLUGIN_ASSETS: Final = ("main.js", "manifest.json", "styles.css")
_PLATFORMS: Final = ("linux-x86_64", "macos-arm64")
_ARCHIVE_PATTERN: Final = re.compile(
    r"^open-brain-(?:(?P<component>graphify|harness)-)?"
    r"(?P<version>[0-9A-Za-z][0-9A-Za-z._-]*)-"
    r"(?P<platform>linux-x86_64|macos-arm64)\.tar\.gz$"
)
_BASE_EXECUTABLE_MEMBER: Final = "open-brain/open-brain"
_RESOURCE_DESTINATIONS: Final = {
    "base": "libexec/open-brain/open-brain",
    "graphify": "libexec/open-brain-graphify",
    "harness": "libexec/open-brain-harness/open-brain-harness",
}
_PORTABLE_V11_SIDECARS: Final = (
    "history/issuer/legacy-bindings-v1.json",
    "history/issuer/migration-v1.json",
    "history/privacy/effective-privacy-v1.json",
    "history/sources/lifecycle-v1.json",
    "history/sources/admission-v1.json",
    "history/sharing/approvals-v1.json",
    "history/historical-authority/reconciliation-v2.json",
    "history/capture-metadata/original-v2.json",
    "history/capture-custody/journal-v1.json",
    "content/facets/state-v1.json",
    "history/facets/operations-v1.json",
    "history/facets/events-v1.json",
    "history/developments/evidence-v1.json",
    "history/developments/append-v1.json",
)
_PORTABLE_V12_SIDECARS: Final = (
    *_PORTABLE_V11_SIDECARS,
    "history/sharing/slack-recall-selections-v1.json",
)
_PORTABLE_V13_SIDECARS: Final = (
    *_PORTABLE_V12_SIDECARS,
    "history/capture-lifecycle/journal-v1.json",
)
_PORTABLE_V14_SIDECARS: Final = (
    *_PORTABLE_V13_SIDECARS,
    "history/retraction/approvals-v1.json",
)
_PORTABLE_V15_SIDECARS: Final = (
    *_PORTABLE_V12_SIDECARS,
    "history/capture-lifecycle/journal-v2.json",
    "history/consumer-intake/evidence-v1.json",
    "history/retraction/approvals-v1.json",
)
_PORTABLE_V16_SIDECARS: Final = (
    *_PORTABLE_V15_SIDECARS,
    "history/embedding-policy/embedding-policy.jsonl",
)
_PORTABLE_V17_SIDECARS: Final = (
    *_PORTABLE_V16_SIDECARS,
    "history/owner-egress-policy/owner-egress-policy.jsonl",
)
_PORTABLE_V18_SIDECARS: Final = (
    *_PORTABLE_V17_SIDECARS,
    "history/followups/followups-v1.json",
)
_REQUIRED_MODULES: Final = frozenset(
    {
        "decimal",
        "open_brain.local_data",
        "open_brain.profile",
        "open_brain.services.agent_setup",
        "open_brain.services.catalog",
        "open_brain.services.component_manifest_v2",
        "open_brain.services.embedding_health",
        "open_brain.services.embedding_helper",
        "open_brain.services.embedding_pin",
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
        "open_brain_engine.engine.portable_v8_authority",
        "open_brain_engine.engine.portable_v8_restore",
        "open_brain_engine.engine.portable_v9_authority",
        "open_brain_engine.engine.portable_v9_restore",
        "open_brain_engine.engine.historical_contracts_v2",
        "open_brain_engine.engine.historical_observation_v2",
        "open_brain_engine.engine.historical_admission_v2",
        "open_brain_engine.engine.historical_transition_v2",
        "open_brain_engine.engine.historical_checkpoint",
        "open_brain_engine.portable.v8",
        "open_brain_engine.portable.v9",
        "open_brain_engine.portable.v10",
        "open_brain_engine.engine.portable_v10_evidence",
        "open_brain_engine.engine.portable_v10_restore",
        "open_brain_engine.portable.v11",
        "open_brain_engine.portable.v11_catalog",
        "open_brain_engine.portable.developments_v11",
        "open_brain_engine.portable.development_binding",
        "open_brain_engine.engine.developments",
        "open_brain_engine.engine.development_schema",
        "open_brain_engine.engine.portable_v11_evidence",
        "open_brain_engine.engine.portable_v11_restore",
        "open_brain_engine.portable.v12",
        "open_brain_engine.portable.v12_catalog",
        "open_brain_engine.portable.slack_v12",
        "open_brain_engine.portable.v14",
        "open_brain_engine.portable.v14_catalog",
        "open_brain_engine.portable.retraction_v14",
        "open_brain_engine.engine.portable_v14_evidence",
        "open_brain_engine.engine.portable_v14_restore",
        "open_brain_engine.engine.owner_retraction",
        "open_brain_engine.engine.owner_retraction_contracts",
        "open_brain_engine.engine.owner_retraction_schema",
        "open_brain_engine.portable.v13",
        "open_brain_engine.portable.v13_catalog",
        "open_brain_engine.portable.lifecycle_v13",
        "open_brain_engine.engine.capture_lifecycle",
        "open_brain_engine.engine.capture_lifecycle_contracts",
        "open_brain_engine.engine.capture_lifecycle_journal",
        "open_brain_engine.engine.capture_lifecycle_policy",
        "open_brain_engine.engine.capture_lifecycle_schema",
        "open_brain_engine.engine.consumer_contracts",
        "open_brain_engine.engine.consumer_segments",
        "open_brain_engine.engine.consumer_sources",
        "open_brain_engine.engine.consumer_profiles",
        "open_brain_engine.engine.consumer_intents",
        "open_brain_engine.engine.consumer_plans",
        "open_brain_engine.engine.consumer_operations",
        "open_brain_engine.engine.consumer_intake",
        "open_brain_engine.engine.consumer_journal",
        "open_brain_engine.engine.consumer_schema",
        "open_brain_engine.portable.v15",
        "open_brain_engine.portable.v15_catalog",
        "open_brain_engine.portable.lifecycle_v15",
        "open_brain_engine.portable.consumer_v15",
        "open_brain_engine.portable.retraction_v15",
        "open_brain_engine.engine.portable_v15_evidence",
        "open_brain_engine.engine.portable_v15_restore",
        "open_brain_engine.engine.portable_v13_evidence",
        "open_brain_engine.engine.portable_v13_restore",
        "open_brain_engine.engine.portable_v12_evidence",
        "open_brain_engine.engine.portable_v12_restore",
        "open_brain_engine.engine.slack_selection_validation",
        "open_brain_engine.engine.slack_sharing",
        "open_brain_engine.engine.slack_sharing_projection",
        "open_brain_engine.engine.slack_sharing_schema",
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

# The embedding resource runs inference in its own audited native helper; the base never
# carries a Python machine-learning stack.
_ML_MODULE_PREFIXES: Final = (
    "numpy",
    "onnxruntime",
    "sentence_transformers",
    "tokenizers",
    "torch",
    "transformers",
)
_EMBEDDING_PIN_MODULE: Final = "open_brain.services.embedding_pin"
_EMBEDDING_PIN_ENVIRONMENT: Final = "OPEN_BRAIN_EMBEDDING_PIN"


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
            for prefix in (*_FORBIDDEN_MODULE_PREFIXES, "_pydecimal")
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
    embedding_artifact: Path | None = None,
) -> dict[str, object]:
    executable = artifact.resolve(strict=True)
    with TemporaryDirectory(prefix="open-brain-smoke-") as raw:
        runtime_root = Path(raw).resolve(strict=True)
        home = runtime_root / "home"
        working_directory = runtime_root / "work"
        home.mkdir(mode=0o700)
        working_directory.mkdir(mode=0o700)
        home.chmod(0o700)
        environment = {"HOME": os.fspath(home), "PATH": os.defpath}
        _smoke_catalog(executable, environment)
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
        _smoke_owner_retraction(executable, home, environment)
        journey["owner_retraction"] = "passed"
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
        if embedding_artifact is not None:
            _smoke_embedding(executable, embedding_artifact, home, environment)
            journey["embedding"] = "passed"
        return {"journey": journey, "self_check": "passed"}


def _pinned_inventory_sha256(executable: Path, platform_tag: str) -> str:
    constants = _embedded_pin_constants(executable)
    pinned = sorted(item for item in constants if re.fullmatch(r"[0-9a-f]{64}", item))
    if platform_tag not in constants or len(pinned) != 1:
        raise BaseNativeError("native embedding pin is unavailable")
    return pinned[0]


def _verify_installed_embedding(executable: Path, helper: Path) -> tuple[str, str]:
    """Installed embedding metadata and bytes match the base pin; return the component ID."""
    from open_brain_engine.engine.embedding_backend import EmbeddingFailure

    from open_brain.services import embedding_helper
    from open_brain.services.component_manifest_v2 import EXECUTABLES
    from open_brain.services.installed_layout import base_prefix

    prefix = base_prefix(executable.resolve(strict=True))
    if prefix is None or helper.resolve(strict=True) != prefix / EXECUTABLES["embedding"]:
        raise BaseNativeError("native embedding helper is not in the base prefix")
    platform_tag = native_platform_tag()
    pinned = _pinned_inventory_sha256(executable, platform_tag)
    try:
        verified = embedding_helper.verify_embedding_metadata(
            embedding_helper.InstalledEmbedding(prefix), expected_inventory_sha256=pinned
        )
    except EmbeddingFailure as error:
        raise BaseNativeError(f"native embedding metadata failed: {error.category}") from error
    for member in verified.inventory.members:
        path = prefix / member.path
        metadata = path.lstat()
        if member.kind == "directory":
            exact = stat.S_ISDIR(metadata.st_mode)
        else:
            exact = (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_size == member.bytes
                and _sha256(path) == member.sha256
            )
        if not exact or stat.S_IMODE(metadata.st_mode) != member.mode:
            raise BaseNativeError("native embedding install differs from its inventory")
    return verified.component_id, pinned


def _smoke_embedding(
    executable: Path, helper: Path, home: Path, environment: Mapping[str, str]
) -> None:
    """One synthetic hybrid query through the installed base and embedding resource."""
    component_id, _ = _verify_installed_embedding(executable, helper)
    data_dir = home / "embedding-brain"
    command = (os.fspath(executable),)
    location = ("--data-dir", os.fspath(data_dir), "--json")
    notes = (
        "Courier delays\nThe parcel courier arrived three days late again.",
        "Garden plan\nTomatoes and basil go in the south bed this spring.",
        "Reading list\nFinish the history of navigation before the trip.",
    )
    captured = [
        json.loads(_run((*command, "capture", note, *location), environment).stdout)
        for note in notes
    ]
    enabled = json.loads(
        _run(
            (*command, "embedding", "enable", "--component-id", component_id, *location),
            environment,
        ).stdout
    )
    rebuilt = json.loads(
        _run((*command, "embedding", "rebuild", *location), environment, timeout=300).stdout
    )
    search = json.loads(
        _run(
            (
                *command,
                "search-page",
                "when did the shipment come in late",
                "--mode",
                "hybrid_required",
                *location,
            ),
            environment,
        ).stdout
    )
    results = search.get("results")
    if (
        any(item.get("status") != "captured" for item in captured)
        or enabled.get("enabled") is not True
        or enabled.get("component_id") != component_id
        or rebuilt.get("records") != len(notes)
        or rebuilt.get("excluded_oversize") != 0
        or search.get("status") != "ok"
        or search.get("mode_used") != "hybrid"
        or not isinstance(results, list)
        or not results
        or "Courier delays" not in json.dumps(results[0])
    ):
        raise BaseNativeError("native hybrid search failed")


def render_embedding_pin(pins: Mapping[str, str]) -> str:
    """Source of the build-generated ``open_brain.services.embedding_pin`` (D8)."""
    if any(
        platform_tag not in _PLATFORMS or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for platform_tag, digest in pins.items()
    ):
        raise BaseNativeError("embedding pin is invalid")
    entries = "".join(
        f'    "{platform_tag}": "{pins[platform_tag]}",\n' for platform_tag in sorted(pins)
    )
    mapping = "{\n" + entries + "}" if entries else "{}"
    return (
        '"""Generated by the native base build from the audited embedding archive."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "from collections.abc import Mapping\n"
        "\n"
        f"EXPECTED_EMBEDDING_INVENTORY_SHA256: Mapping[str, str] = {mapping}\n"
    )


def embedding_pin_environment(output: Path, pins: Mapping[str, str]) -> dict[str, str]:
    """Write the generated pin below the build output, never into the source tree."""
    generated = output.resolve() / "generated" / "embedding_pin.py"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(render_embedding_pin(pins), encoding="utf-8")
    return {_EMBEDDING_PIN_ENVIRONMENT: os.fspath(generated)}


def _string_constants(code: object) -> frozenset[str]:
    found: set[str] = set()
    pending = [code]
    while pending:
        current = pending.pop()
        for constant in getattr(current, "co_consts", ()):
            if isinstance(constant, str):
                found.add(constant)
            elif hasattr(constant, "co_consts"):
                pending.append(constant)
    return frozenset(found)


def _embedded_pin_constants(executable: Path) -> frozenset[str]:
    try:
        readers = importlib.import_module("PyInstaller.archive.readers")
        outer = readers.CArchiveReader(os.fspath(executable.resolve(strict=True)))
        embedded = outer.open_embedded_archive("PYZ.pyz")
        return _string_constants(embedded.extract(_EMBEDDING_PIN_MODULE))
    except Exception as error:
        raise BaseNativeError("native embedding pin is unavailable") from error


def audit_base_embedding(artifact: Path, *, platform: str, inventory_sha256: str) -> None:
    """The frozen base pins exactly this inventory and carries no Python ML stack."""
    modules = archive_modules(artifact)
    if any(
        module == prefix or module.startswith(prefix + ".")
        for module in modules
        for prefix in _ML_MODULE_PREFIXES
    ):
        raise BaseNativeError("native artifact contains a machine-learning package")
    constants = _embedded_pin_constants(artifact)
    pinned = {item for item in constants if re.fullmatch(r"[0-9a-f]{64}", item)}
    if platform not in constants or pinned != {inventory_sha256}:
        raise BaseNativeError("native embedding pin disagrees with the embedding archive")


def write_base_archive(
    artifact: Path,
    destination: Path,
    *,
    version: str,
    obsidian_plugin_directory: Path,
) -> Path:
    from tools.open_brain_dev.artifact_audit import inspect_artifact

    audit = audit_base_artifact(artifact)
    selected_destination = destination.resolve()
    selected_destination.mkdir(parents=True, exist_ok=True)
    archive = selected_destination / f"open-brain-{version}-{audit.platform_tag}.tar.gz"
    _write_reproducible_archive(
        artifact,
        archive,
        obsidian_plugin_directory=obsidian_plugin_directory,
        version=version,
    )
    if inspect_artifact(archive, ()):
        raise BaseNativeError("base archive failed the strict content audit")
    return archive


def _validate_resource_sets(artifacts: Sequence[ReleaseArtifact]) -> None:
    keys = {(item.platform_tag, item.role) for item in artifacts}
    platforms = {item.platform_tag for item in artifacts}
    role_sets = {
        frozenset(item.role for item in artifacts if item.platform_tag == platform)
        for platform in platforms
    }
    if (
        not artifacts
        or len(keys) != len(artifacts)
        or len(role_sets) != 1
        or next(iter(role_sets))
        not in {frozenset({"base", "graphify"}), frozenset({"base", "graphify", "harness"})}
    ):
        raise BaseNativeError("release manifest resource pairs are invalid")


def write_release_manifest(artifacts: Sequence[ReleaseArtifact], destination: Path) -> Path:
    role_order = {"base": 0, "graphify": 1, "harness": 2}
    ordered = tuple(
        sorted(artifacts, key=lambda item: (item.platform_tag, role_order.get(item.role, 99)))
    )
    _validate_resource_sets(ordered)
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
            key=lambda item: (
                item.platform_tag,
                {"base": 0, "graphify": 1, "harness": 2}.get(item.role, 99),
            ),
        )
    )
    _validate_resource_sets(manifest.artifacts)
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
    if manifest_path.name == MANIFEST_V2_FILENAME:
        return _render_homebrew_formula_v2(
            manifest_path, destination, repository=repository, base_url=base_url, smoke=smoke
        )
    manifest = read_release_manifest(manifest_path)
    origin = _formula_origin(repository, base_url, manifest.version)
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
        harness = resources.get("harness")
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
            )
        )
        if harness is not None:
            lines.extend(
                (
                    '    resource "harness" do',
                    f'      url "{origin}/{harness.filename}"',
                    f'      sha256 "{harness.sha256}"',
                    "    end",
                )
            )
        lines.extend(("  end", ""))
    lines.extend(
        (
            "  def install",
            # One-folder base: the folder moves intact; bin holds a relative link to it.
            f'    libexec.install "{_EXECUTABLE}"',
            f'    bin.install_symlink libexec/"{_BASE_EXECUTABLE_MEMBER}"',
            '    (share/"open-brain/obsidian-plugin").install Dir["obsidian-plugin/*"]',
            '    resource("graphify").stage do',
            '      libexec.install "open-brain" => "open-brain-graphify"',
            '      (share/"open-brain/licenses/graphify").install Dir["licenses/graphify/*"]',
            "    end",
        )
    )
    if any(item.role == "harness" for item in manifest.artifacts):
        lines.extend(
            (
                '    resource("harness").stage do',
                '      libexec.install "open-brain-harness"',
                '      (share/"open-brain/harness").install Dir["harness/*"]',
                "    end",
            )
        )
    lines.extend(
        (
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
        )
    )
    if any(item.role == "harness" for item in manifest.artifacts):
        lines.extend(
            (
                '    assert_path_exists share/"open-brain/harness/harness-hook.mjs"',
                '    assert_match "harness", '
                'shell_output("#{libexec}/open-brain-harness/open-brain-harness harness --help")',
            )
        )
    lines.extend(("  end", "end", ""))
    selected = destination.resolve()
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text("\n".join(lines), encoding="utf-8")
    return selected


def _formula_origin(repository: str, base_url: str | None, version: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise BaseNativeError("Homebrew repository is invalid")
    origin = (
        f"https://github.com/{repository}/releases/download/v{version}"
        if base_url is None
        else base_url.rstrip("/")
    )
    if not origin.startswith(("https://", "file://")) or any(
        character in origin for character in ('"', "\n", "\r")
    ):
        raise BaseNativeError("Homebrew artifact origin is invalid")
    return origin


def _render_homebrew_formula_v2(
    manifest_path: Path,
    destination: Path,
    *,
    repository: str,
    base_url: str | None,
    smoke: bool,
) -> Path:
    from tools.open_brain_dev.component_manifest_v2 import (
        ComponentManifestError,
        parse_manifest_v2,
    )
    from tools.open_brain_dev.release_v2 import render_homebrew_formula_v2

    try:
        manifest = parse_manifest_v2(manifest_path.resolve(strict=True).read_bytes())
    except OSError as error:
        raise BaseNativeError("release manifest is unavailable") from error
    except ComponentManifestError as error:
        raise BaseNativeError("release manifest v2 is invalid") from error
    text = render_homebrew_formula_v2(
        manifest,
        origin=_formula_origin(repository, base_url, manifest.version),
        repository=repository,
        smoke=smoke,
    )
    selected = destination.resolve()
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(text, encoding="utf-8")
    return selected


def build_base_artifact(
    root: Path, output: Path, *, embedding_archive: Path | None = None
) -> tuple[Path, Path, Path, Path, Path]:
    if sys.version_info[:2] != (3, 14):
        raise BaseNativeError("native build requires Python 3.14")
    if (
        importlib.metadata.version("pyinstaller") != "6.22.3"
        or importlib.metadata.version("pyinstaller-hooks-contrib") != "2026.7"
    ):
        raise BaseNativeError("native build toolchain is not pinned")
    version = product_version(root)
    # Build order: embedding archive, then its inventory pin, then the base freeze.
    # Without an audited archive a stale shell pin must never reach the freeze.
    environment = {
        key: value for key, value in os.environ.items() if key != _EMBEDDING_PIN_ENVIRONMENT
    }
    embedding_pin: tuple[str, str] | None = None
    if embedding_archive is not None:
        from tools.open_brain_dev.embedding_native import verify_embedding_archive

        embedding_version, resource, _ = verify_embedding_archive(embedding_archive)
        if embedding_version != version or resource.platform != native_platform_tag():
            raise BaseNativeError("embedding archive does not match this base build")
        embedding_pin = (resource.platform, resource.inventory_sha256)
        environment = {
            **environment,
            **embedding_pin_environment(output, {resource.platform: resource.inventory_sha256}),
        }
    subprocess.run(
        pyinstaller_command(root, output),
        cwd=root.resolve(strict=True),
        env=environment,
        check=True,
        timeout=1800,
    )
    artifact = output.resolve() / "dist/open-brain/open-brain"
    audit_base_artifact(artifact)
    if embedding_pin is not None:
        audit_base_embedding(artifact, platform=embedding_pin[0], inventory_sha256=embedding_pin[1])
    smoke_base_artifact(artifact, version=version, repository_root=root)
    archive = write_base_archive(
        artifact,
        output.resolve() / "release",
        version=version,
        obsidian_plugin_directory=(root.resolve(strict=True) / "build/obsidian-plugin/open-brain"),
    )
    from tools.open_brain_dev.graphify_native import build_graphify_artifact

    graphify_artifact, graphify_archive = build_graphify_artifact(root, output, version=version)
    from tools.open_brain_dev.harness_distribution import write_harness_archive

    harness_archive = write_harness_archive(
        output.resolve() / "dist/open-brain-harness",
        root.resolve(strict=True) / "packages/app/src/open_brain/assets",
        output.resolve() / "release",
        version=version,
    )
    manifest = write_release_manifest(
        (
            _release_artifact(archive, executable=artifact),
            _release_artifact(graphify_archive, executable=graphify_artifact),
            _release_artifact(harness_archive),
        ),
        output.resolve() / "release" / _MANIFEST,
    )
    return artifact, graphify_artifact, archive, graphify_archive, manifest


def _release_artifact(archive: Path, *, executable: Path | None = None) -> ReleaseArtifact:
    selected = archive.resolve(strict=True)
    match = _ARCHIVE_PATTERN.fullmatch(selected.name)
    if match is None or not selected.is_file():
        raise BaseNativeError("release artifact filename is invalid")
    role = match.group("component") or "base"
    if role == "harness":
        from tools.open_brain_dev.artifact_audit import inspect_artifact

        if inspect_artifact(selected, ()):
            raise BaseNativeError("harness archive failed the strict content audit")
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
        or (match.group("component") or "base") != artifact.role
    ):
        raise BaseNativeError("release artifact record is invalid")


def _archive_executable_sha256(archive: Path) -> str:
    try:
        name = (
            "open-brain-harness/open-brain-harness"
            if "-harness-" in archive.name
            else _EXECUTABLE
            if "-graphify-" in archive.name
            else _BASE_EXECUTABLE_MEMBER
        )
        with tarfile.open(archive, "r:gz") as bundle:
            matches = [
                member
                for member in bundle.getmembers()
                if member.name == name and member.isfile() and not member.issym()
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


def _smoke_owner_retraction(
    executable: Path,
    home: Path,
    environment: Mapping[str, str],
) -> None:
    """Prove packaged owner commands against a separate synthetic Brain."""
    root = home / "owner-retraction-brain"

    def invoke(*arguments: str) -> dict[str, object]:
        return cast(
            dict[str, object],
            json.loads(
                _run(
                    (os.fspath(executable), *arguments, "--data-dir", os.fspath(root), "--json"),
                    environment,
                ).stdout
            ),
        )

    capture = invoke("capture", "Synthetic native retraction evidence")
    preview = invoke("retract", "preview", "--capture", str(capture["capture_id"]))
    identity = str(preview["preview_id"])
    if invoke("retract", "preview", "--preview-id", identity, "--item-page", "0") != preview:
        raise BaseNativeError("native retraction preview paging failed")
    operation = "00000000-0000-4000-8000-000000000119"
    receipt = invoke("retract", "commit", "--preview-id", identity, "--operation-id", operation)
    if (
        receipt.get("status") != "committed"
        or receipt.get("counts") != preview.get("counts")
        or invoke("retract", "status", "--operation-id", operation) != receipt
        or invoke("retract", "commit", "--preview-id", identity, "--operation-id", operation)
        != receipt
        or invoke("search", "Synthetic native retraction evidence").get("results") != []
    ):
        raise BaseNativeError("native retraction receipt or exclusion failed")
    archive = home / "owner-retraction-export"
    if invoke("export", os.fspath(archive), "--verify").get("verification") != "verified":
        raise BaseNativeError("native retraction export failed")
    evidence = json.loads((archive / "history/retraction/approvals-v1.json").read_bytes())
    if len(evidence["records"]) != 1 or identity in json.dumps(evidence):
        raise BaseNativeError("native retraction evidence failed")


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
        or type(exported.get("schema_version")) is not int
        or exported.get("schema_version") != 18
        or not (export / "portable-manifest.json").is_file()
        or not (export / "sources/logical-sources.json").is_file()
        or not all((export / relative).is_file() for relative in _PORTABLE_V18_SIDECARS)
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
    # A base without an installed embedding resource reports it missing; an installed one
    # reports it ready. Either way nothing is consented or cached in this Brain yet.
    embedding_health = status_result.pop("embedding_health", None)
    component = embedding_health.get("component_id") if isinstance(embedding_health, dict) else None
    ready = isinstance(component, str) and re.fullmatch(r"[0-9a-f]{64}", component) is not None
    if embedding_health != {
        "cache_state": "absent",
        "component_id": component if ready else None,
        "consent_state": "required",
        "last_error": None if ready else "resource_missing",
        "resource_state": "ready" if ready else "missing",
        "schema": "ob.embedding-health.v1",
    }:
        raise BaseNativeError("native embedding health failed")
    if status_result != {
        "application_encryption": False,
        "brain_count": 1,
        "daemon_running": False,
        "ingestion_journal": {
            "last_failure_code": None,
            "oldest_age_seconds": None,
            "pending_count": 0,
            "quarantined_count": 0,
            "retained_bytes": 0,
        },
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
        if checked.stdout != f"{check}: ok. Journal pending: 0; quarantined: 0.\n":
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
        or type(exported.get("schema_version")) is not int
        or exported.get("schema_version") != 18
        or not (export / "sources/logical-sources.json").is_file()
        or not all((export / relative).is_file() for relative in _PORTABLE_V18_SIDECARS)
        or not blob.is_file()
        or blob.read_bytes() != source_bytes
        or len(captures) != 1
        or any(".open-brain" in path.parts for path in export.rglob("*"))
        or any(path.suffix in {".sqlite", ".sqlite3"} for path in export.rglob("*"))
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
        flag: str,
        name: str,
        arguments: dict[str, object],
        *,
        tools: set[str] | None = None,
        inspect_catalog: bool = False,
    ) -> dict[str, object]:
        expected_tools = _expected_mcp_tools(flag, name, tools)
        denied_name = "brain_search_page" if name == "brain_capture" else "brain_capture"
        requests = _mcp_exchange_requests(
            initialize,
            name,
            arguments,
            denied_name,
            inspect_catalog=inspect_catalog,
        )
        try:
            process = subprocess.run(
                (os.fspath(executable), "mcp", flag),
                cwd=_runtime_working_directory(environment),
                env=dict(environment),
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return _validate_mcp_exchange(
                process,
                expected_tools,
                inspect_catalog=inspect_catalog,
            )
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            raise BaseNativeError("native MCP exchange failed") from error

    capture = exchange(
        "--allow-capture",
        "brain_capture",
        {
            "text": token,
            "idempotency_key": "installed-smoke",
        },
        inspect_catalog=True,
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
    search = exchange(
        "--allow-search",
        "brain_search_page",
        {"dto_version": 1, "query": token},
    )
    results = cast(list[dict[str, object]], search.get("results"))
    if (
        len(results) != 1
        or results[0].get("record_id") != capture.get("capture_id")
        or results[0].get("trust") != "unverified"
        or cast(dict[str, object], results[0].get("provenance")).get("source_origin") != "unknown"
    ):
        raise BaseNativeError("native MCP search failed")
    paged = search
    read = exchange(
        "--allow-content-read",
        "brain_read",
        {
            "dto_version": 1,
            "record_id": capture["capture_id"],
            "expected_revision_id": capture["capture_id"],
        },
        tools={"brain_read", "brain_contract_describe"},
    )
    history = exchange(
        "--allow-history-read",
        "brain_history_list",
        {"dto_version": 1, "record_id": capture["capture_id"]},
        tools={"brain_history_list", "brain_history_show", "brain_contract_describe"},
    )
    paged_results = cast(list[dict[str, object]], paged["results"])
    history_entries = cast(list[dict[str, object]], history["entries"])
    if (
        paged.get("complete") is not True
        or [item["record_id"] for item in paged_results] != [capture["capture_id"]]
        or read.get("complete") is not True
        or read.get("content") != {"kind": "untrusted_text", "text": token}
        or history.get("complete") is not True
        or [item["revision_id"] for item in history_entries] != [capture["capture_id"]]
    ):
        raise BaseNativeError("native negotiated retrieval failed")
    organization_tools = {"brain_space_create", "brain_space_rename", "brain_inbox_route"}
    read_tools = {"brain_space_list", "brain_inbox_list"}
    created = exchange(
        "--allow-organize",
        "brain_space_create",
        {"name": "Installed organization", "idempotency_key": "installed-space"},
        tools=organization_tools,
    )
    repeated_space = json.loads(
        _run(
            (
                os.fspath(executable),
                "space",
                "create",
                "Installed organization",
                "--idempotency-key",
                "installed-space",
                "--json",
            ),
            environment,
        ).stdout
    )
    if repeated_space != created:
        raise BaseNativeError("native organization replay failed")
    space_id = cast(dict[str, str], created["space"])["space_id"]
    renamed = json.loads(
        _run(
            (os.fspath(executable), "space", "rename", space_id, "Installed renamed", "--json"),
            environment,
        ).stdout
    )
    spaces = exchange("--allow-inbox-read", "brain_space_list", {}, tools=read_tools)
    if renamed["space"] not in cast(list[dict[str, object]], spaces["spaces"]):
        raise BaseNativeError("native organization rename failed")
    route_arguments = {
        "capture_id": capture["capture_id"],
        "space_id": space_id,
        "idempotency_key": "installed-route",
    }
    routed = exchange(
        "--allow-organize",
        "brain_inbox_route",
        route_arguments,
        tools=organization_tools,
    )
    repeated_route = json.loads(
        _run(
            (
                os.fspath(executable),
                "inbox",
                "route",
                cast(str, capture["capture_id"]),
                space_id,
                "--idempotency-key",
                "installed-route",
                "--json",
            ),
            environment,
        ).stdout
    )
    inbox = exchange(
        "--allow-inbox-read",
        "brain_inbox_list",
        {"unassigned_only": True},
        tools=read_tools,
    )
    assigned = json.loads(
        _run(
            (os.fspath(executable), "inbox", "list", "--json"),
            environment,
        ).stdout
    )
    post_route_search = exchange(
        "--allow-search",
        "brain_search_page",
        {"dto_version": 1, "query": token},
    )
    post_route_results = cast(list[dict[str, object]], post_route_search["results"])
    if (
        routed != repeated_route
        or any(
            item["capture_id"] == capture["capture_id"]
            for item in cast(list[dict[str, object]], inbox["items"])
        )
        or not any(
            item["capture_id"] == capture["capture_id"] and item["space_id"] == space_id
            for item in assigned["items"]
        )
        or len(post_route_results) != 1
        or post_route_results[0].get("record_id") != capture["capture_id"]
        or post_route_results[0].get("space_id") != space_id
        or post_route_results[0].get("trust") != "unverified"
    ):
        raise BaseNativeError("native organization routing failed")
    run_root = _brain_root(home) / ".open-brain/run"
    if run_root.is_dir() and any(run_root.iterdir()):
        raise BaseNativeError("native MCP left a runtime artifact")


def _expected_mcp_tools(flag: str, name: str, tools: set[str] | None) -> set[str]:
    expected = set(tools if tools is not None else {name})
    expected.add("brain_catalog")
    if flag == "--allow-search":
        expected.update({"brain_contract_describe", "brain_search_page"})
    elif flag == "--allow-organize":
        expected.update({"brain_contract_describe", "brain_source_route"})
    return expected


def _mcp_exchange_requests(
    initialize: dict[str, object],
    name: str,
    arguments: dict[str, object],
    denied_name: str,
    *,
    inspect_catalog: bool,
) -> list[dict[str, object]]:
    requests = [
        initialize,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": denied_name, "arguments": {}},
        },
    ]
    if inspect_catalog:
        requests.append(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "brain_catalog", "arguments": {"schema_version": 2}},
            }
        )
    return requests


def _validate_mcp_exchange(
    process: subprocess.CompletedProcess[str],
    expected_tools: set[str],
    *,
    inspect_catalog: bool = False,
) -> dict[str, object]:
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    if (
        process.stderr
        or len(responses) != 4 + int(inspect_catalog)
        or responses[0]["result"]["capabilities"] != {"tools": {}}
        or {tool["name"] for tool in responses[1]["result"]["tools"]} != expected_tools
        or responses[2]["result"].get("isError")
        or responses[3]["result"].get("isError") is not True
        or responses[3]["result"]["content"] != [{"type": "text", "text": "unknown tool"}]
    ):
        raise BaseNativeError("native MCP exchange failed")
    if inspect_catalog:
        catalog = responses[4]["result"]
        structured = catalog.get("structuredContent")
        surfaces = structured.get("surfaces") if isinstance(structured, dict) else None
        mcp = surfaces.get("mcp") if isinstance(surfaces, dict) else None
        if (
            catalog.get("isError")
            or not isinstance(mcp, dict)
            or mcp.get("authorized_tools") != sorted(expected_tools)
        ):
            raise BaseNativeError("native MCP catalog authorization failed")
    return cast(dict[str, object], responses[2]["result"]["structuredContent"])


def _smoke_catalog(executable: Path, environment: Mapping[str, str]) -> None:
    """Inspect grantless public metadata before any Brain is created."""
    catalog = cast(
        dict[str, object],
        json.loads(
            _run(
                (os.fspath(executable), "catalog", "--schema-version", "2", "--json"),
                environment,
            ).stdout
        ),
    )
    compatibility = catalog.get("compatibility")
    product = catalog.get("product")
    packages = catalog.get("packages")
    acceptance = catalog.get("acceptance")
    if (
        catalog.get("schema_version") != 2
        or type(catalog.get("schema_version")) is not int
        or not isinstance(compatibility, dict)
        or any(
            type(compatibility.get(key)) is not int
            for key in (
                "bridge_protocol",
                "catalog_schema",
                "portable_metadata",
                "runtime_session",
                "state_schema",
            )
        )
        or compatibility
        != {
            "bridge_protocol": 1,
            "catalog_schema": 2,
            "portable_metadata": 18,
            "runtime_session": 16,
            "state_schema": 23,
            "task_contract": "t03.v1",
        }
        or not isinstance(product, dict)
        or product.get("name") != "open-brain"
        or product.get("public_acceptance") != "not_certified"
        or not isinstance(packages, list)
        or {
            package.get("name"): package.get("installed")
            for package in packages
            if isinstance(package, dict)
        }
        != {
            "open-brain": True,
            "open-brain-collector": False,
            "open-brain-connectors": False,
            "open-brain-engine": True,
        }
        or not isinstance(acceptance, dict)
        or acceptance.get("trusted_certifications") != []
    ):
        raise BaseNativeError("native catalog discovery failed")


def _smoke_graphify_projection(
    executable: Path,
    graphify_artifact: Path,
    home: Path,
    environment: Mapping[str, str],
) -> None:
    from open_brain.services.installed_layout import base_prefix

    prefix = base_prefix(executable.resolve(strict=True))
    if prefix is None:
        raise BaseNativeError("installed Graphify helper is not in the base keg")
    expected = prefix / "libexec/open-brain-graphify"
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
    from open_brain_engine.engine import (
        CaptureAction,
        CaptureReceipt,
        TextPayload,
        open_local_engine,
    )

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
    if not isinstance(target, CaptureReceipt):
        raise BaseNativeError("native graph fixture remained queued")
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
    """Archive the one-folder base, keeping its relative links, plus the plugin assets."""
    from tools.open_brain_dev.harness_distribution import _record, write_records_archive

    plugin_assets = _validated_obsidian_plugin_assets(obsidian_plugin_directory, version=version)
    folder = executable.parent
    if (
        executable.name != _EXECUTABLE
        or folder.name != _EXECUTABLE
        or folder.is_symlink()
        or not folder.is_dir()
    ):
        raise BaseNativeError("native base folder is invalid")
    root = folder.resolve(strict=True)
    paths = [(root, _EXECUTABLE)]
    paths.extend(
        (path, f"{_EXECUTABLE}/{path.relative_to(root).as_posix()}")
        for path in sorted(root.rglob("*"))
    )
    records: list[dict[str, object]] = [
        {"path": "obsidian-plugin", "kind": "directory", "mode": 0o755}
    ]
    payloads: dict[str, bytes] = {}
    try:
        for path, name in paths:
            record, payload = _record(path, name, label="base")
            records.append(record)
            if payload is not None:
                payloads[name] = payload
        for name, path in plugin_assets:
            member = f"obsidian-plugin/{name}"
            records.append(
                {"path": member, "kind": "file", "mode": 0o644, "size": path.stat().st_size}
            )
            payloads[member] = path.read_bytes()
    except OSError:
        raise BaseNativeError("native base folder is unavailable") from None
    write_records_archive(archive, records, payloads)


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
    command: Sequence[str], environment: Mapping[str, str], *, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=_runtime_working_directory(environment),
            env=dict(environment),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BaseNativeError("native runtime check failed") from error


def _runtime_working_directory(environment: Mapping[str, str]) -> Path:
    try:
        home = Path(environment["HOME"]).resolve(strict=True)
        working_directory = (home.parent / "work").resolve(strict=True)
    except (KeyError, OSError) as error:
        raise BaseNativeError("native runtime isolation is unavailable") from error
    if home.parent != working_directory.parent or working_directory == home:
        raise BaseNativeError("native runtime isolation is unavailable")
    return working_directory


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.open_brain_dev.base_native")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--embedding-archive", type=Path)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--artifact", type=Path, required=True)
    audit.add_argument("--graphify-artifact", type=Path, required=True)
    audit.add_argument("--embedding-archive", type=Path)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--root", type=Path, required=True)
    smoke.add_argument("--artifact", type=Path, required=True)
    smoke.add_argument("--graphify-artifact", type=Path, required=True)
    smoke.add_argument("--embedding-artifact", type=Path)
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
            namespace.root, namespace.output, embedding_archive=namespace.embedding_archive
        )
        payload: object = {
            "archive": os.fspath(archive),
            "artifact": os.fspath(artifact),
            "graphify_archive": os.fspath(graphify_archive),
            "graphify_artifact": os.fspath(graphify_artifact),
            "harness_artifact": os.fspath(
                namespace.output.resolve() / "dist/open-brain-harness/open-brain-harness"
            ),
            "harness_archive": os.fspath(
                namespace.output.resolve()
                / "release"
                / (
                    f"open-brain-harness-{product_version(namespace.root)}-"
                    f"{native_platform_tag()}.tar.gz"
                )
            ),
            "manifest": os.fspath(manifest_path),
            "status": "built",
        }
    elif namespace.command == "audit":
        from tools.open_brain_dev.graphify_native import audit_graphify_artifact

        audits: dict[str, object] = {
            "base": audit_base_artifact(namespace.artifact).to_dict(),
            "graphify": audit_graphify_artifact(namespace.graphify_artifact).to_dict(),
        }
        if namespace.embedding_archive is not None:
            from tools.open_brain_dev.embedding_native import audit_embedding_archive

            embedding = audit_embedding_archive(namespace.embedding_archive)
            audit_base_embedding(
                namespace.artifact,
                platform=str(embedding["platform"]),
                inventory_sha256=str(embedding["inventory_sha256"]),
            )
            audits["embedding"] = embedding
        payload = audits
    elif namespace.command == "smoke":
        payload = smoke_base_artifact(
            namespace.artifact,
            version=product_version(namespace.root),
            repository_root=namespace.root,
            graphify_artifact=namespace.graphify_artifact,
            embedding_artifact=namespace.embedding_artifact,
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
